#!/usr/bin/env python3
"""Validate the 2026-10-26 production browser finding eval catalog.

This command checks offline contracts and synthetic controls only. It never
contacts production and never turns a blocked live finding into acceptance.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "docs/evals/production-browser-findings-2026-10-26.jsonl"
REQUIRED_IDS = [f"pb-{index:03d}" for index in range(1, 9)]
EXPECTED_HISTORICAL_OUTCOMES = {
    "pb-001": "pass",
    "pb-002": "blocked",
    "pb-003": "pass_with_failures_preserved",
    "pb-004": "fail",
    "pb-005": "fail",
    "pb-006": "blocked_unconnected",
    "pb-007": "blocked_gpu_pool",
    "pb-008": "pass",
}
SECRET_LITERAL = re.compile(r"(?:eyJ[a-zA-Z0-9_-]{20,}|Bearer\s+\S+|sk-[a-zA-Z0-9]{12,}|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})")


def finite_number(value: Any, *, positive: bool = False) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)


def nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def at(value: Any, pointer: str) -> Any:
    if not pointer.startswith("/"):
        raise KeyError(pointer)
    for raw in pointer[1:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def auth_session_logout(sample: dict[str, Any]) -> bool:
    keys = ("callback_success", "fragment_cleared", "library_visible", "reload_authenticated",
            "logout_cleared", "protected_redirect", "redirect_preserved")
    return all(sample.get(key) is True for key in keys)


def preflight_gpu_quota(sample: dict[str, Any]) -> bool:
    preflight = sample.get("preflight")
    quota = sample.get("quota")
    if not isinstance(preflight, dict) or preflight.get("ok") is not True:
        return False
    checks = preflight.get("checks")
    gpu = checks.get("gpu_pool") if isinstance(checks, dict) else None
    if not isinstance(gpu, dict) or not (gpu.get("ok") is True or sample.get("cpu_fallback") is True):
        return False
    if not isinstance(quota, dict) or not all(finite_number(quota.get(k), positive=True)
                                               for k in ("conversion_remaining_h", "storage_remaining_gb")):
        return False
    raw_quota_keys = ("ai_hours_used", "ai_hours_total", "storage_gb_used", "storage_gb_total")
    if not all(finite_number(quota.get(key)) for key in raw_quota_keys):
        return False
    if quota["ai_hours_total"] < quota["ai_hours_used"] \
            or quota["storage_gb_total"] < quota["storage_gb_used"] \
            or not nonempty_text(quota.get("profile_quota_text")):
        return False
    return sample.get("quota_consistent") is True


def task_history_reconciliation(sample: dict[str, Any]) -> bool:
    counts = tuple(sample.get(key) for key in ("total", "active", "done", "failed"))
    if not all(type(value) is int and value >= 0 for value in counts):
        return False
    rows = sample.get("rows")
    if not isinstance(rows, list) or len(rows) != sample["total"]:
        return False
    if sample["total"] != sample["active"] + sample["done"] + sample["failed"]:
        return False
    actual = {status: sum(row.get("status") == status for row in rows) for status in ("active", "done", "failed")}
    if actual != {"active": sample["active"], "done": sample["done"], "failed": sample["failed"]}:
        return False
    for row in rows:
        if not isinstance(row, dict) or row.get("status") not in actual:
            return False
        if row["status"] == "failed" and not (nonempty_text(row.get("stage")) and nonempty_text(row.get("reason"))
                                                and row.get("retry") is True):
            return False
    return sample.get("filters_reconcile") is True and sample.get("history_preserved") is True


def reader_media_ready_playback(sample: dict[str, Any]) -> bool:
    images = sample.get("expected_images")
    videos = sample.get("expected_videos")
    assets = sample.get("assets")
    if not isinstance(images, list) or not images or not isinstance(videos, list) or not videos:
        return False
    if not isinstance(assets, list) or {row.get("name") for row in assets} != set(images + videos):
        return False
    for row in assets:
        if not row.get("exists") or not finite_number(row.get("bytes"), positive=True):
            return False
        if row["name"] in videos and not (
            row.get("ffprobe_exit_code") == 0 and finite_number(row.get("duration_s"), positive=True)
        ):
            return False
    samples = sample.get("samples")
    if not isinstance(samples, list) or len(samples) < 2:
        return False
    ready = False
    for point in samples:
        if not isinstance(point, dict) or point.get("visibility") != "visible":
            return False
        if type(point.get("elapsed_ms")) is not int or not 0 <= point["elapsed_ms"] <= 60000:
            return False
        if point.get("elapsed_ms") > 0 and point.get("decoded_images") == len(images) and point.get("loading_count") == 0:
            rows = point.get("videos")
            ready = isinstance(rows, list) and {row.get("label") for row in rows} == set(videos) and all(
                type(row.get("ready_state")) is int and 2 <= row["ready_state"] <= 4 and row.get("error") in (0, None)
                for row in rows
            )
    played = sample.get("playback")
    return ready and isinstance(played, dict) and played.get("action") == "play" and played.get("paused") is False \
        and played.get("label") in videos and finite_number(played.get("before_s")) \
        and finite_number(played.get("after_s")) and finite_number(played.get("duration_s"), positive=True) \
        and played["before_s"] < played["after_s"] <= played["duration_s"] and played.get("error") in (0, None)


def callout_render(sample: dict[str, Any]) -> bool:
    expected = set(sample.get("source_markers", []))
    rendered = sample.get("rendered_callouts")
    return expected == {"info", "tip", "warning", "example"} and isinstance(rendered, list) \
        and {row.get("type") for row in rendered} == expected \
        and all(nonempty_text(row.get("text")) for row in rendered) \
        and sample.get("raw_marker_tokens") is False and sample.get("callout_dom_semantics") is True \
        and sample.get("content_preserved") is True \
        and sample.get("sanitized") is True


def colab_onboarding(sample: dict[str, Any]) -> bool:
    before = sample.get("status_before")
    after = sample.get("status_after")
    redirect = sample.get("redirect")
    return sample.get("login_succeeded") is True and sample.get("connect_button_visible") is True \
        and sample.get("skip_available") is True and isinstance(before, dict) and before.get("can_authorize") is True \
        and before.get("has_credentials") is False and isinstance(after, dict) and after.get("has_credentials") is True \
        and sample.get("explicit_action") is True and sample.get("same_google_account") is True \
        and sample.get("next_is_same_site") is True and isinstance(redirect, str) \
        and redirect.startswith("/profile/colab?next=/")


def minimal_real_run(sample: dict[str, Any]) -> bool:
    if sample.get("preflight_ok") is not True or not nonempty_text(sample.get("input_url")) \
            or not sample["input_url"].startswith("https://ferosse.tech/test-assets/"):
        return False
    if not (nonempty_text(sample.get("run_id")) and sample.get("persisted") is True
            and sample.get("sse_connected") is True and sample.get("book_visible") is True
            and sample.get("markdown_nonempty") is True and sample.get("terminal") in ("done", "partial")):
        return False
    stages = sample.get("stages")
    if not isinstance(stages, list) or not stages or not all(row.get("status") == "completed" for row in stages):
        return False
    usage = sample.get("usage")
    return isinstance(usage, dict) and usage.get("run_id") == sample["run_id"] \
        and all(type(usage.get(key)) is int and usage[key] > 0 for key in ("llm_in_tok", "llm_out_tok")) \
        and finite_number(usage.get("gpu_s"), positive=True) and finite_number(sample.get("cost_usd"), positive=True) \
        and sample.get("cost_run_id") == sample["run_id"]


def anonymous_security_surface(sample: dict[str, Any]) -> bool:
    return (sample.get("healthz_status") == 200 and sample.get("healthz_redacted") is True
            and sample.get("me_status") == 401 and sample.get("runs_status") == 401
            and sample.get("docs_status") == 403 and sample.get("openapi_status") == 403
            and sample.get("protocol_guard") is True and sample.get("terms_status") == 200
            and sample.get("privacy_status") == 200)


CHECKS = {
    "auth_session_logout": auth_session_logout,
    "preflight_gpu_quota": preflight_gpu_quota,
    "task_history_reconciliation": task_history_reconciliation,
    "reader_media_ready_playback": reader_media_ready_playback,
    "callout_render": callout_render,
    "colab_onboarding": colab_onboarding,
    "minimal_real_run": minimal_real_run,
    "anonymous_security_surface": anonymous_security_surface,
}


def cases(path: Path = DATASET) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def validate(rows: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return ["catalog: rows must be objects"]
    if [row.get("id") for row in rows] != REQUIRED_IDS:
        errors.append("catalog: ids must be ordered pb-001..pb-008")
    for row in rows:
        try:
            if row["scope"] == "":
                errors.append(f"{row.get('id')}: scope is empty")
            meta = row["metadata"]
            if meta["offline_can_close_production"] is not False:
                errors.append(f"{row['id']}: offline closure must be false")
            if not meta.get("production_evidence_required"):
                errors.append(f"{row['id']}: production evidence requirement missing")
            if meta.get("historical_outcome") != EXPECTED_HISTORICAL_OUTCOMES.get(row["id"]):
                errors.append(f"{row['id']}: historical outcome changed from recorded production result")
            expected = row["expectedOutput"]
            checks = expected["checks"]
            if len(checks) != 1 or checks[0] not in CHECKS:
                errors.append(f"{row['id']}: unknown or ambiguous check")
            sample = row["input"]["sample"]
            if sample.get("kind") != "synthetic":
                errors.append(f"{row['id']}: controls must be synthetic")
            negatives = row["counterexamples"]
            if len(negatives) < 3 or len({n.get("name") for n in negatives}) != len(negatives):
                errors.append(f"{row['id']}: at least three unique counterexamples required")
            for negative in negatives:
                if negative.get("check") != checks[0]:
                    errors.append(f"{row['id']}: counterexample check mismatch")
                at(sample, negative["path"])
            blob = json.dumps(row, ensure_ascii=False)
            if SECRET_LITERAL.search(blob):
                errors.append(f"{row['id']}: literal secret or personal identifier detected")
        except (KeyError, TypeError, ValueError, IndexError):
            errors.append(f"{row.get('id', 'unknown')}: malformed case")
    return errors


def replay(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for row in rows:
        check_name = row["expectedOutput"]["checks"][0]
        passed = CHECKS[check_name](row["input"]["sample"])
        results.append({"id": row["id"], "check": check_name, "synthetic_control": passed,
                        "historical_outcome": row["metadata"].get("historical_outcome")})
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--require-closed", action="store_true")
    args = parser.parse_args()
    try:
        rows = cases(args.dataset)
        failures = validate(rows)
        if not failures:
            failures.extend(f"{row['id']}: synthetic control does not satisfy its contract"
                            for row in rows if not CHECKS[row["expectedOutput"]["checks"][0]](row["input"]["sample"]))
        if args.require_closed:
            failures.extend(f"OPEN {row['id']}: {row['metadata'].get('historical_outcome', 'unproven')}"
                            for row in rows if row["metadata"].get("historical_outcome") != "pass")
    except (OSError, json.JSONDecodeError, TypeError, KeyError, ValueError):
        failures = ["catalog: unreadable or invalid; source values suppressed"]
    if failures:
        print("FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("PASS: 8 offline production browser finding contracts; no new production acceptance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

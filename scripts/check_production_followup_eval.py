#!/usr/bin/env python3
"""Validate the 2026-10-26 production follow-up evaluation set.

The positive controls intentionally describe the observed production outcome.
They prove that a blocked or unresolved finding is not silently promoted to
pass. This checker is offline and never contacts production or a model.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "docs/evals/production-followup-eval-set-2026-10-26.jsonl"
REQUIRED_IDS = [f"pf-{index:03d}" for index in range(1, 9)]
SECRET_LITERAL = re.compile(
    r"(?:Bearer\s+\S+|sk-[A-Za-z0-9]{12,}|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|"
    r"(?:api[_-]?key|access[_-]?token|client[_-]?secret|cookie)\s*[:=])",
    re.IGNORECASE,
)


def _finite_nonnegative(value: Any) -> bool:
    return type(value) in (int, float) and value >= 0


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def preflight_blocked(sample: dict[str, Any]) -> bool:
    preflight = sample.get("preflight") or {}
    quota = sample.get("quota") or {}
    return (
        preflight.get("ok") is True
        and preflight.get("gpu_pool") is False
        and preflight.get("cpu_fallback") is False
        and quota.get("conversion_remaining_h") == 0
        and quota.get("storage_remaining_gb") == 0
        and sample.get("submission_allowed") is False
        and sample.get("billing_started") is False
        and sample.get("status") == "blocked"
    )


def reader_media_failure(sample: dict[str, Any]) -> bool:
    return (
        sample.get("status") == "fail"
        and isinstance(sample.get("loading_count"), int)
        and sample["loading_count"] > 0
        and sample.get("video_elements") == 0
        and sample.get("playback_verified") is False
        and sample.get("production_acceptance") is False
    )


def callout_raw_failure(sample: dict[str, Any]) -> bool:
    return (
        sample.get("status") == "fail"
        and sample.get("raw_marker_tokens") is True
        and sample.get("rendered_callouts") is False
        and sample.get("production_acceptance") is False
    )


def markdown_export_inconclusive(sample: dict[str, Any]) -> bool:
    return (
        sample.get("status") == "inconclusive"
        and sample.get("modal_visible") is True
        and sample.get("markdown_option_visible") is True
        and sample.get("download_observed") is False
        and sample.get("production_acceptance") is False
    )


def integration_environment_blocked(sample: dict[str, Any]) -> bool:
    return (
        sample.get("status") == "blocked_environment"
        and sample.get("error_code") == "EPERM"
        and sample.get("local_listener_allowed") is False
        and sample.get("app_assertion_failed") is False
        and sample.get("must_not_count_as_production_failure") is True
    )


def model_billing_blocked(sample: dict[str, Any]) -> bool:
    usage = sample.get("usage") or {}
    return (
        sample.get("status") == "blocked"
        and sample.get("model_http_status") == 403
        and usage.get("llm_in_tok") == 0
        and usage.get("llm_out_tok") == 0
        and sample.get("cost_usd") == 0
        and _text(sample.get("failure_reason"))
        and sample.get("production_acceptance") is False
    )


def bearer_sse_live(sample: dict[str, Any]) -> bool:
    """Require an authenticated live SSE subscription to reach a real terminal event."""
    stages = sample.get("stage_events") or []
    return (
        sample.get("status") == "done"
        and sample.get("sse_no_auth_http") == 401
        and sample.get("sse_bearer_http") == 200
        and sample.get("connected") is True
        and isinstance(stages, list)
        and any(isinstance(event, str) and event.startswith("stage:") for event in stages)
        and sample.get("terminal_event") == "graph:end"
        and sample.get("terminal_status") == "done"
        and sample.get("production_acceptance") is True
    )


def source_download_blocked(sample: dict[str, Any]) -> bool:
    """Require Bilibili source denial to fail before any downstream spend."""
    usage = sample.get("usage") or {}
    return (
        sample.get("status") == "error"
        and sample.get("source") == "bilibili"
        and sample.get("failure_stage") == "download"
        and sample.get("failure_reason_code") == "source_access_unavailable"
        and sample.get("failure_reason_recorded") is True
        and sample.get("downstream_stages_started") is False
        and sample.get("model_called") is False
        and usage.get("llm_in_tok") == 0
        and usage.get("llm_out_tok") == 0
        and sample.get("cost_usd") == 0
        and sample.get("production_acceptance") is False
    )


CHECKS = {
    "preflight_blocked": preflight_blocked,
    "reader_media_failure": reader_media_failure,
    "callout_raw_failure": callout_raw_failure,
    "markdown_export_inconclusive": markdown_export_inconclusive,
    "integration_environment_blocked": integration_environment_blocked,
    "model_billing_blocked": model_billing_blocked,
    "bearer_sse_live": bearer_sse_live,
    "source_download_blocked": source_download_blocked,
}


def load_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _contains_secret(value: Any) -> bool:
    return bool(SECRET_LITERAL.search(json.dumps(value, ensure_ascii=False)))


def validate(rows: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    if [row.get("id") for row in rows] != REQUIRED_IDS:
        failures.append("catalog: ids must be ordered pf-001..pf-008")
    for row in rows:
        row_id = row.get("id")
        if row.get("scope") != "production-followup":
            failures.append(f"{row_id}: invalid scope")
        metadata = row.get("metadata") or {}
        if metadata.get("offline_can_close_production") is not False:
            failures.append(f"{row_id}: offline closure must remain false")
        if not metadata.get("production_evidence_required"):
            failures.append(f"{row_id}: production evidence requirement missing")
        expected = row.get("expectedOutput") or {}
        checks = expected.get("checks") or []
        if len(checks) != 1 or checks[0] not in CHECKS:
            failures.append(f"{row_id}: unknown check")
        counterexamples = row.get("counterexamples")
        if not isinstance(counterexamples, list) or len(counterexamples) < 3:
            failures.append(f"{row_id}: at least three counterexamples required")
        for negative in counterexamples or []:
            if negative.get("check") != checks[0] or not negative.get("name") or "value" not in negative:
                failures.append(f"{row_id}: malformed counterexample")
        if _contains_secret(row):
            failures.append(f"{row_id}: secret-like literal found")
    if len({row.get("id") for row in rows}) != len(rows):
        failures.append("catalog: duplicate ids")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()
    try:
        rows = load_rows(args.dataset)
        failures = validate(rows)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL\n- input: {exc}")
        return 1
    if failures:
        print("FAIL")
        print("\n".join(f"- {failure}" for failure in failures))
        return 1
    print(f"PASS: {len(rows)} offline production follow-up contracts; no new production acceptance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

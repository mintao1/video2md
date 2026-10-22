#!/usr/bin/env python3
"""Validate each production stage from measurements, never from a pass label alone."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

REQUIRED_IDS = [f"pps-{index:03d}" for index in range(1, 25)]
USAGE_KEYS = ("llm_in_tok", "llm_out_tok", "gpu_s")
PRICE_KEYS = ("llm_in_mtok", "llm_out_mtok", "gpu_min")


def number(value: object, *, positive: bool = False) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)
    except OverflowError:
        return False


def count(value: object, *, positive: bool = False) -> bool:
    return type(value) is int and (value > 0 if positive else value >= 0)


def text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def equal(actual: object, expected: object, tolerance: float = 0.0000005) -> bool:
    return number(actual) and number(expected) and abs(actual - expected) <= tolerance + 1e-12


def flags(metrics: dict, *keys: str) -> bool:
    return all(metrics.get(key) is True for key in keys)


def zeros(metrics: dict, *keys: str) -> bool:
    return all(number(metrics.get(key)) and metrics[key] == 0 for key in keys)


def price_cost(usage: dict, prices: dict) -> float | None:
    if not all(number(usage.get(key), positive=True) for key in USAGE_KEYS):
        return None
    if not valid_prices(prices):
        return None
    cached = usage.get("llm_cached_in_tok", 0)
    if not count(cached) or cached > usage["llm_in_tok"]:
        return None
    cache_price = prices.get("llm_cached_in_mtok", prices["llm_in_mtok"])
    if cached and not number(cache_price, positive=True):
        return None
    value = ((usage["llm_in_tok"] - cached) / 1e6 * prices["llm_in_mtok"]
             + cached / 1e6 * cache_price
             + usage["llm_out_tok"] / 1e6 * prices["llm_out_mtok"]
             + usage["gpu_s"] / 60 * prices["gpu_min"])
    return round(value, 6) if number(value, positive=True) else None


def valid_prices(prices: dict) -> bool:
    llm = all(number(prices.get(key), positive=True) for key in PRICE_KEYS[:2])
    gpu = number(prices.get("gpu_min"), positive=True) or (
        prices.get("gpu_min") == 0 and number(prices.get("gpu_min"))
        and prices.get("gpu_policy") == "free" and text(prices.get("gpu_free_reason")))
    return llm and gpu


def validate_stage(index: int, m: dict, stages: dict) -> bool:
    def other(i: int) -> dict:
        return stages.get(f"pps-{i:03d}", {}).get("metrics", {})

    if index == 1:
        return flags(m, "logout_observed", "oauth_login_succeeded", "authenticated_page_visible", "session_survives_reload")
    if index == 2:
        return (flags(m, "cleared", "entered_explicitly", "submitted") and text(m.get("approved_url"))
                and m.get("visible_url") == m["approved_url"])
    if index == 3:
        return (count(m.get("bytes"), positive=True) and number(m.get("duration_s"), positive=True)
                and flags(m, "completed_before_model"))
    if index == 4:
        return (m.get("provider") == "colab" and count(m.get("segments"), positive=True)
                and flags(m, "timestamped_segments", "transcript_nonempty")
                and number(m.get("duration_s"), positive=True))
    if index == 5:
        return (m.get("provider") == "colab" and count(m.get("frames_requested"), positive=True)
                and count(m.get("frames_completed"), positive=True)
                and m["frames_completed"] == m["frames_requested"] and number(m.get("duration_s"), positive=True))
    if index == 6:
        complete = (count(m.get("requested"), positive=True) and count(m.get("completed"), positive=True)
                    and m["completed"] == m["requested"] and number(m.get("duration_s"), positive=True))
        return complete and ((m.get("device") == "cpu" and zeros(m, "gpu_seconds"))
                             or (m.get("device") == "gpu" and equal(m.get("gpu_seconds"), m["duration_s"], 0.001)))
    if index == 7:
        times = [other(4).get("duration_s"), other(5).get("duration_s")]
        if other(6).get("device") == "gpu":
            times.append(other(6).get("duration_s"))
        return (all(number(v, positive=True) for v in times) and number(m.get("gpu_s"), positive=True)
                and abs(m["gpu_s"] - sum(times)) <= max(1.0, sum(times) * 0.05)
                and equal(m.get("gpu_s"), other(10).get("gpu_s"), 0.001))
    if index == 8:
        return (flags(m, "queue_idle", "closed") and number(m.get("idle_elapsed_s"))
                and m.get("warm_window_s") == 30 and m.get("poll_interval_s") == 15
                and m["idle_elapsed_s"] <= m["warm_window_s"] + m["poll_interval_s"])
    if index == 9:
        return (flags(m, "model_called", "inference_succeeded", "content_nonempty")
                and m.get("http_status") == 200 and m.get("refusal_detected") is False)
    if index == 10:
        return (count(m.get("llm_in_tok"), positive=True) and count(m.get("llm_out_tok"), positive=True)
                and flags(m, "owner_matches"))
    if index == 11:
        return valid_prices(m) and flags(m, "approved")
    if index == 12:
        expected = price_cost(other(10), other(11))
        return (expected is not None and expected > 0 and flags(m, "settled")
                and equal(m.get("cost_usd"), expected) and type(m.get("settlement_count")) is int
                and m["settlement_count"] == 1)
    if index == 13:
        return (flags(m, "book_visible", "reader_loads", "media_references_safe")
                and count(m.get("markdown_chars"), positive=True) and m.get("bundle_state") == "available"
                and count(m.get("book_count_before")) and count(m.get("book_count_after"))
                and m["book_count_after"] == m["book_count_before"] + 1)
    if index == 14:
        return (m.get("backend_status") in ("done", "partial") and m.get("ui_status") == m["backend_status"]
                and flags(m, "feedback_visible"))
    if index == 15:
        return (count(m.get("task_count_before")) and count(m.get("task_count_after"))
                and m["task_count_after"] == m["task_count_before"] + 1 and m.get("historical_retry") is False
                and flags(m, "feedback_visible", "run_persisted", "owner_matches", "submit_log_present", "running_stage_observed"))
    if index == 16:
        return (flags(m, "resolved") and text(m.get("url")) and m["url"] == other(2).get("approved_url")
                and number(m.get("elapsed_s")) and number(m.get("timeout_s"), positive=True)
                and m["elapsed_s"] <= m["timeout_s"])
    if index == 17:
        return (count(m.get("candidate_count"), positive=True) and count(m.get("max_frames"), positive=True)
                and m["candidate_count"] <= m["max_frames"] and flags(m, "files_nonempty", "timestamps_valid"))
    if index == 18:
        return (count(m.get("selected_count"), positive=True) and count(other(17).get("candidate_count"), positive=True)
                and m["selected_count"] <= other(17)["candidate_count"] and flags(m, "all_in_manifest")
                and (m.get("mode") == "llm" or (m.get("mode") == "heuristic_fallback" and flags(m, "fallback_reason_visible"))))
    if index == 19:
        before, after = m.get("before"), m.get("after")
        if not isinstance(before, dict) or not isinstance(after, dict):
            return False
        return (flags(m, "owner_matches", "isolated_window") and count(before.get("runs_n"))
                and count(after.get("runs_n")) and after["runs_n"] == before["runs_n"] + 1
                and all(number(before.get(k)) and number(after.get(k))
                        and equal(after[k] - before[k], other(10).get(k), 0.001 if k == "gpu_s" else 0)
                        for k in USAGE_KEYS)
                and number(before.get("spend_usd")) and number(after.get("spend_usd"))
                and equal(after["spend_usd"] - before["spend_usd"], other(12).get("cost_usd")))
    if index == 20:
        return flags(m, "details_opens", "usage_visible", "cost_visible", "values_match_ledger")
    if index == 21:
        return flags(m, "failure_observed", "run_id_visible", "failure_stage_visible", "failure_reason_visible", "retry_available", "secrets_redacted")
    if index == 22:
        return flags(m, "retry_observed", "same_run") and zeros(m, "task_count_delta", "token_delta", "gpu_s_delta", "cost_delta")
    if index == 23:
        return flags(m, "over_budget_attempted", "rejected", "error_visible") and zeros(m, "model_calls_delta", "cost_delta")
    if index == 24:
        return (flags(m, "rejected_submission_observed", "error_visible", "modal_remains_open", "url_preserved")
                and m.get("success_toast_visible") is False and zeros(m, "new_runs"))
    return False


def evaluate(evidence: dict, *, allow_synthetic: bool = False) -> list[str]:
    if not isinstance(evidence, dict):
        return ["matrix: evidence must be an object"]
    failures: list[str] = []
    if evidence.get("kind") != "production" and not (allow_synthetic and evidence.get("kind") == "synthetic"):
        failures.append("matrix: synthetic or unspecified evidence cannot pass production acceptance")
    run_id = evidence.get("run_id")
    if not text(run_id):
        failures.append("matrix: run_id is required")
    results = evidence.get("results")
    by_id: dict = {}
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict) or result.get("id") not in REQUIRED_IDS:
            failures.append("matrix: invalid or unknown stage id")
            continue
        case_id = result["id"]
        if case_id in by_id:
            failures.append("matrix: duplicate stage id")
        by_id[case_id] = result
    # Normalize untrusted metric containers before any cross-stage lookup.
    stages = {key: {"metrics": row["metrics"] if isinstance(row.get("metrics"), dict) else {}}
              for key, row in by_id.items()}
    for index, case_id in enumerate(REQUIRED_IDS, 1):
        row = by_id.get(case_id)
        if row is None:
            failures.append(f"{case_id}: missing evidence")
            continue
        refs = row.get("evidence_refs")
        if not text(row.get("observed")) or not isinstance(refs, list) or not refs or not all(map(text, refs)):
            failures.append(f"{case_id}: observation and source references are required")
        metrics = stages[case_id]["metrics"]
        if metrics.get("run_id") != run_id or not text(run_id):
            failures.append(f"{case_id}: metrics must belong to the evaluated run")
        if row.get("status") != "pass":
            failures.append(f"{case_id}: stage is {row.get('status', 'unproven')}")
        try:
            passed = validate_stage(index, metrics, stages)
        except (ValueError, TypeError, OverflowError):
            passed = False
        if not passed:
            failures.append(f"{case_id}: measured values do not satisfy the stage contract")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--allow-synthetic", action="store_true", help="offline self-test only, not production acceptance")
    args = parser.parse_args()
    try:
        failures = evaluate(json.loads(args.evidence.read_text(encoding="utf-8")), allow_synthetic=args.allow_synthetic)
    except (OSError, ValueError) as exc:
        failures = [f"matrix: unreadable or invalid evidence ({type(exc).__name__})"]
    if failures:
        print("FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    label = "synthetic self-test" if args.allow_synthetic else "production evidence"
    print(f"PASS: all {len(REQUIRED_IDS)} pipeline stages passed ({label})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

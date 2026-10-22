#!/usr/bin/env python3
"""Evaluate redacted evidence for the billable production video pipeline."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

TERMINAL = {"done", "partial", "error"}
SUCCESS_TERMINAL = {"done", "partial"}


def _positive(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


def evaluate(evidence: dict, *, allow_synthetic: bool = False) -> list[str]:
    failures: list[str] = []
    if evidence.get("kind") != "production" and not (allow_synthetic and evidence.get("kind") == "synthetic"):
        failures.append("prod-billing-evidence-kind: synthetic or unspecified evidence is not production acceptance")
    authentication = evidence.get("authentication", {})
    if not authentication.get("logout_observed"):
        failures.append("prod-billing-logout: test did not start by ending the existing session")
    if not authentication.get("oauth_login_succeeded"):
        failures.append("prod-billing-login: Google OAuth login did not complete")

    source = evidence.get("source_resolution", {})
    if not source.get("resolved"):
        failures.append("prod-billing-source: approved video URL did not resolve")

    submission = evidence.get("submission", {})
    if not submission.get("feedback_visible"):
        failures.append("prod-billing-feedback: submission result was not visible")
    if not submission.get("run_id"):
        failures.append("prod-billing-run: no persisted run_id")
    if not submission.get("submit_log_present"):
        failures.append("prod-billing-submit-log: submission audit row missing")
    if not submission.get("url_entered_explicitly"):
        failures.append("prod-billing-explicit-url: video URL was not explicitly entered in the UI")
    before = submission.get("task_count_before")
    after = submission.get("task_count_after")
    if type(before) is not int or type(after) is not int or after != before + 1:
        failures.append("prod-billing-new-task: submission did not create exactly one new task")

    execution = evidence.get("execution", {})
    if execution.get("status") not in TERMINAL:
        failures.append("prod-billing-terminal: run did not reach a terminal state")
    elif execution.get("status") not in SUCCESS_TERMINAL:
        failures.append("prod-billing-success-terminal: run ended in error")
    if not execution.get("running_stage_observed"):
        failures.append("prod-billing-stage-progress: no active processing stage was observed")
    if not execution.get("model_called"):
        failures.append("prod-billing-model: no real model call observed")
    if execution.get("model_inference_succeeded") is not True:
        failures.append("prod-billing-inference: attempted or rejected model calls are not successful inference")
    if execution.get("status") == "error" and not execution.get("failure_reason_visible"):
        failures.append("prod-billing-failure-reason: failure reason is not visible to the user")
    if execution.get("status") == "error" and not execution.get("failure_stage_visible"):
        failures.append("prod-billing-failure-stage: failure stage is not visible to the user")
    if not execution.get("run_id_visible"):
        failures.append("prod-billing-run-visible: run_id is not visible to the user")

    billing = evidence.get("billing", {})
    usage = billing.get("usage") or {}
    usage_present = any(_positive(usage.get(key)) for key in ("llm_in_tok", "llm_out_tok", "gpu_s"))
    if not usage_present:
        failures.append("prod-billing-usage: no positive LLM/GPU usage")
    if not all(type(usage.get(key)) is int and usage[key] > 0 for key in ("llm_in_tok", "llm_out_tok")):
        failures.append("prod-billing-tokens: both LLM token counters must be positive integers")
    if not _positive(usage.get("gpu_s")):
        failures.append("prod-billing-gpu: real GPU work must have positive measured usage")
    if billing.get("cost_usd") is None:
        failures.append("prod-billing-cost: terminal cost was not settled")
    if not billing.get("monthly_rollup_includes_run"):
        failures.append("prod-billing-rollup: run absent from monthly cost rollup")
    if not billing.get("usage_visible"):
        failures.append("prod-billing-usage-visible: run usage is not visible")
    if not billing.get("cost_visible"):
        failures.append("prod-billing-cost-visible: settled cost is not visible")
    prices = billing.get("prices") or {}
    free_gpu = (type(prices.get("gpu_min")) in (int, float) and prices["gpu_min"] == 0
                and billing.get("gpu_policy") == "free" and bool(billing.get("gpu_free_reason")))
    positive_prices = all(_positive(prices.get(key)) for key in ("llm_in_mtok", "llm_out_mtok")) \
        and (_positive(prices.get("gpu_min")) or free_gpu)
    if not positive_prices:
        failures.append("prod-billing-prices: production prices are missing or zero")
    if positive_prices and all(_positive(usage.get(key)) for key in ("llm_in_tok", "llm_out_tok", "gpu_s")):
        cached = usage.get("llm_cached_in_tok", 0)
        cache_price = prices.get("llm_cached_in_mtok", prices["llm_in_mtok"])
        if type(cached) is not int or not 0 <= cached <= usage["llm_in_tok"] or (cached and not _positive(cache_price)):
            failures.append("prod-billing-cache: invalid cached token accounting or missing cache price")
            cached = 0
        expected = round((usage["llm_in_tok"] - cached) / 1e6 * prices["llm_in_mtok"]
                         + cached / 1e6 * cache_price
                         + usage["llm_out_tok"] / 1e6 * prices["llm_out_mtok"]
                         + usage["gpu_s"] / 60 * prices["gpu_min"], 6)
        actual = billing.get("cost_usd")
        if not _positive(actual) or not math.isfinite(expected) or abs(actual - expected) > 0.0000005 + 1e-12:
            failures.append("prod-billing-cost-formula: positive cost must equal usage times actual prices")

    artifact = evidence.get("artifact", {})
    if not artifact.get("book_visible") or not artifact.get("markdown_nonempty"):
        failures.append("prod-billing-artifact: generated textbook is unavailable or empty")
    if type(artifact.get("book_count_delta")) is not int or artifact["book_count_delta"] != 1:
        failures.append("prod-billing-book-delta: library did not gain exactly one textbook")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--allow-synthetic", action="store_true", help="offline self-test only")
    args = parser.parse_args()
    evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    failures = evaluate(evidence, allow_synthetic=args.allow_synthetic)
    if failures:
        print("FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("PASS: synthetic billing self-test" if args.allow_synthetic else "PASS: application cost attribution checked; supplier debit still requires check_production_failure_cost.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

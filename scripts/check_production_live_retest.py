#!/usr/bin/env python3
"""Validate the fresh 4c login retest evidence without contacting production."""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "docs/evals/production-live-retest-regression-eval-set-2026-10-26.jsonl"
EVIDENCE = ROOT / "docs/evals/production-live-retest-evidence-2026-10-26.json"
SECRET = re.compile(r"(?i)(?:bearer\s+\S+|api[_-]?key\s*[:=]|access[_-]?token\s*[:=]|cookie\s*[:=]|[?&](?:token|sig|key)=)")


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _positive(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def evaluate(data: dict, catalog: list[dict]) -> list[str]:
    failures: list[str] = []
    if data.get("kind") != "production":
        failures.append("evidence-kind")
    if data.get("environment", {}).get("host_alias") != "lcode-4c":
        failures.append("environment-host")
    auth = data.get("authentication", {})
    if not all(auth.get(key) is True for key in ("logout_observed", "oauth_login_succeeded", "session_refresh_kept_login")):
        failures.append("authentication-session")
    if auth.get("protocol_checkbox_checked") is not False or auth.get("protocol_guard_observed") is not False:
        failures.append("protocol-bypass-observation")
    if auth.get("oauth_login_initiated_before_protocol_acceptance") is not True:
        failures.append("protocol-bypass-evidence")
    history = data.get("task_history", {})
    if (history.get("count_after_failure") != history.get("count_before", -1) + 1
            or history.get("count_after_success") != history.get("count_after_failure", -1) + 1
            or history.get("book_count_after_success") != history.get("book_count_before", -1) + 1):
        failures.append("history-deltas")
    failure = data.get("runs", {}).get("d4797d57", {})
    execution = failure.get("execution", {})
    usage = failure.get("billing", {}).get("usage", {})
    if (execution.get("status") != "error" or execution.get("failure_stage") != "note"
            or execution.get("model_called") is not False or execution.get("failure_reason_visible") is not True
            or usage.get("llm_in_tok") != 0 or usage.get("llm_out_tok") != 0
            or failure.get("billing", {}).get("cost_usd") != 0.0):
        failures.append("short-input-cutoff")
    success = data.get("runs", {}).get("cad0dc5a", {})
    execution = success.get("execution", {})
    billing = success.get("billing", {})
    usage = billing.get("usage", {})
    prices = billing.get("prices", {})
    expected = usage.get("llm_in_tok", 0) * prices.get("llm_in_mtok", 0) * 1e-6 + usage.get("llm_out_tok", 0) * prices.get("llm_out_mtok", 0) * 1e-6
    artifact = success.get("artifact", {})
    if (execution.get("status") != "done" or execution.get("model_called") is not True
            or not _positive(usage.get("llm_in_tok")) or not _positive(usage.get("llm_out_tok"))
            or abs(billing.get("cost_usd", -1) - round(expected, 6)) > 1e-9
            or artifact.get("book_visible") is not True or artifact.get("markdown_nonempty") is not True
            or artifact.get("book_count_delta") != 1 or artifact.get("frames_kept") != 8
            or artifact.get("frames_ocr") != 80):
        failures.append("success-artifact-billing")
    if SECRET.search(json.dumps(data, ensure_ascii=False)) or SECRET.search(json.dumps(catalog, ensure_ascii=False)):
        failures.append("secret-like-literal")
    if [row.get("id") for row in catalog] != ["plr-retest-001", "plr-retest-002", "plr-retest-003"]:
        failures.append("catalog-ids")
    for row in catalog:
        if row.get("metadata", {}).get("offline_can_close_production") is not False or len(row.get("counterexamples", [])) < 3:
            failures.append(f"catalog-contract:{row.get('id')}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--evidence", type=Path, default=EVIDENCE)
    args = parser.parse_args()
    failures = evaluate(json.loads(args.evidence.read_text(encoding="utf-8")), _rows(args.dataset))
    if failures:
        print("FAIL")
        print("\n".join(f"- {item}" for item in failures))
        return 1
    print("PASS: fresh 4c production login retest evidence and regression catalog")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

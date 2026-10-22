#!/usr/bin/env python3
"""Replay the eleven review findings against preserved, redacted evidence.

This is an offline regression gate, not a new production certification. Stage
contracts remain owned by check_production_pipeline_stages.py.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE = ROOT / "docs/testing/2026-10-21/opencode-glm-retest"
DATASET = ROOT / "docs/evals/glm-production-review-eval-set.jsonl"
SPEC = importlib.util.spec_from_file_location("glm_review_stages", ROOT / "scripts/check_production_pipeline_stages.py")
assert SPEC and SPEC.loader
stages = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stages)


def load_evidence(directory: Path) -> dict:
    names = ("stage-evidence", "lesson-result", "production-final-readonly",
             "billing-reconciliation", "reuse-reconciliation", "budget-previous",
             "budget-restored", "temporary-cli-rollback", "playback-proof")
    return {name: json.loads((directory / f"{name}.json").read_text()) for name in names}


def classify_transfer(*, current: bool, returned: bool, terminal: bool) -> str:
    """A stale progress response cannot prove a current download fault.

    Even a fresh pending response proves only pending; diagnosing a timeout
    requires a separate measured timeout, not an arbitrary elapsed impression.
    """
    if returned:
        return "completed" if terminal else "returned"
    return "pending" if current else "unconfirmed"


def finite(value: object, *, positive: bool = False) -> bool:
    return (type(value) in (int, float) and math.isfinite(value)
            and (value > 0 if positive else value >= 0))


def zero_deltas(values: dict) -> bool:
    required = {"llm_in_tok", "llm_out_tok", "gpu_s", "spend_usd", "runs_n"}
    return set(values) == required and all(finite(value) and value == 0 for value in values.values())


def check_case(index: int, evidence: dict) -> bool:
    matrix = evidence["stage-evidence"]
    by_id = {r["id"]: r for r in matrix["results"]}
    normalized = {key: {"metrics": r["metrics"]} for key, r in by_id.items()}

    def valid(*indices: int) -> bool:
        return all(by_id[f"pps-{i:03d}"]["status"] == "pass"
                   and stages.validate_stage(i, normalized[f"pps-{i:03d}"]["metrics"], normalized)
                   for i in indices)

    run = evidence["lesson-result"]
    final = evidence["production-final-readonly"]
    reconciliation = evidence["billing-reconciliation"]
    run_id = run["run_id"]
    if (matrix["kind"] != "production" or not run_id
            or any(obj["run_id"] != run_id for obj in (matrix, final, reconciliation))
            or any(row["metrics"]["run_id"] != run_id for row in by_id.values())):
        return False
    artifact = run["artifacts"][0]
    clip_time = datetime.fromisoformat(run["stages"]["clips"]["ts"])
    # Persisted run timestamps are naive China time, DB updated is UTC-aware.
    terminal_time = datetime.fromisoformat(final["record_updated"]).timestamp()
    clip_epoch = datetime.fromisoformat(clip_time.isoformat() + "+08:00").timestamp()
    returned = artifact["clips_done"] is True and clip_epoch <= terminal_time
    terminal = run["status"] == "done" and artifact["note_exists"] is True
    if index == 1:
        return valid(4, 17, 24)
    if index == 2:
        return valid(5, 18, 24) and artifact["selected_count"] == 8
    if index == 3:
        clips = artifact["stage_metrics"]["clips"]
        audit = final["submit_audit"]
        return (valid(6, 15) and clips["requested"] == clips["completed"] == 8
                and returned and len(audit) == 1 and audit[0]["dedup"] is False)
    if index == 4:
        return (classify_transfer(current=False, returned=returned, terminal=terminal) == "completed"
                and classify_transfer(current=False, returned=False, terminal=False) == "unconfirmed")
    if index == 5:
        rollback = evidence["temporary-cli-rollback"]
        # This hash is the independently saved original CLI, not the tar checksum.
        return (rollback["original_cli_restored"] is True and rollback["temporary_helper_removed"] is True
                and rollback["sha256"] == "3b502bf5fc07fc8fe2e04000b80bb024f29538f654b0f94c4e60f6abcf0bf6f8")
    if index == 6:
        return (valid(9, 10, 11, 12) and returned and terminal
                and final["markdown_chars"] == artifact["markdown_chars"] == 2910
                and final["model"] == "glm-5.3-flash"
                and run["cost_usd"] == reconciliation["expected_cost_usd"] == reconciliation["actual_cost_usd"])
    if index == 7:
        previous = evidence["budget-previous"]["env_entry"]
        original = 0.0 if previous is None else float(previous["value"])
        restored = evidence["budget-restored"]
        return (valid(1) and finite(original) and type(restored["budget_usd"]) in (int, float)
                and restored["budget_usd"] == original == final["budget_usd"]
                and type(restored["active"]) is int and restored["active"] == 0)
    if index == 8:
        reuse = evidence["reuse-reconciliation"]
        return (valid(22, 23, 24) and reuse["same_run"] is True
                and zero_deltas(reuse["deltas"]) and zero_deltas(reconciliation["budget_attempt_deltas"]))
    if index == 9:
        playback = evidence["playback-proof"]
        return (valid(19, 20) and finite(playback["currentTime"], positive=True)
                and finite(playback["duration"], positive=True)
                and playback["currentTime"] <= playback["duration"]
                and playback["readyState"] == 4 and playback["paused"] is False
                and all(finite(item["bytes"], positive=True) for item in final["media"])
                and len(final["media"]) == 10 and evidence["budget-restored"]["active"] == 0
                and reconciliation["month_total"] == final["cost"]["usage"]
                == by_id["pps-019"]["metrics"]["after"])
    if index == 10:
        usage = run["usage"]
        return (valid(9, 10, 11, 12, 22, 23) and final["model"] == "glm-5.3-flash"
                and usage["llm_in_tok"] == 6498 and usage["llm_out_tok"] == 6919
                and run["cost_usd"] == 0.004434)
    if index == 11:
        # Guard the historical verdict: summary success must not erase blockers.
        failures = {item.split(":", 1)[0] for item in stages.evaluate(matrix)}
        return (valid(2, 9, 10, 11, 12, 14, 15, 19, 20, 22, 23, 24)
                and failures == {"pps-008", "pps-013", "pps-016"}
                and by_id["pps-008"]["status"] == "fail"
                and by_id["pps-013"]["status"] == by_id["pps-016"]["status"] == "blocked")
    return False


def evaluate(evidence: dict) -> list[str]:
    failures = []
    for index in range(1, 12):
        try:
            passed = check_case(index, evidence)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError, OverflowError):
            passed = False
        if not passed:
            failures.append(f"glm-review-{index:03d}: evidence violates review regression contract")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE)
    args = parser.parse_args()
    try:
        failures = evaluate(load_evidence(args.evidence_dir))
    except (OSError, ValueError) as exc:
        failures = [f"matrix: unreadable evidence ({type(exc).__name__})"]
    if failures:
        print("FAIL\n" + "\n".join(failures))
        return 1
    print("PASS: 11 offline GLM review regressions; full production acceptance remains FAIL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

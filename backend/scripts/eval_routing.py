#!/usr/bin/env python3
"""Deterministic route-contract evaluator.

Generates the v1 route_contract matrix (60 cases) and evaluates routing/failure
semantics without network, GPU, or model credentials.  This is intentionally a
contract harness; replace ``simulate`` with captured transport events when the
real adapter is available.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

TASKS = {
    "transcribe": ("whisper-a", False),
    "ocr": ("vision-a", False),
    "select_frames": ("model-a", True),
    "organize": ("model-a", False),
    "review": ("model-a", False),
    "metadata": ("model-a", False),
    "clips": ("gpu-http", True),
    "judge": ("judge-a", False),
}
FAULTS = ("none", "429", "500", "empty", "timeout")


def cases() -> list[dict]:
    rows = []
    # 8 tasks × 5 transport conditions, plus 20 provider capability cases.
    for task, (model, can_fallback) in TASKS.items():
        for fault in FAULTS:
            rows.append({
                "id": f"route-{task}-{fault}", "task": task,
                "config": {"model": model, "provider": "primary"},
                "fault": fault, "fallback_allowed": can_fallback and fault != "timeout",
            })
    providers = ("http", "colab-cli", "kaggle", "zerogpu")
    for i in range(20):
        task = ("transcribe", "ocr", "clips")[i % 3]
        provider = providers[i % len(providers)]
        rows.append({
            "id": f"route-provider-{i+1:02d}", "task": task,
            "config": {"model": TASKS[task][0], "provider": provider},
            "fault": "none", "fallback_allowed": False,
        })
    return rows


def simulate(row: dict) -> dict:
    fault = row["fault"]
    task = row["task"]
    fallback = bool(row["fallback_allowed"] and fault in {"429", "500", "empty"})
    if fault == "none":
        status, failure = "ok", "none"
    elif fallback:
        status, failure = "ok", "fallback"
    elif fault in {"429", "500", "empty", "timeout"}:
        status, failure = "failed", fault
    else:
        status, failure = "failed", "4xx"
    return {"task": task, "model": row["config"]["model"],
            "provider": row["config"]["provider"], "fallback": fallback,
            "attempts": 2 if fault in {"429", "500", "empty", "timeout"} else 1,
            "status": status, "failure_class": failure}


def evaluate(rows: list[dict]) -> dict:
    route_ok = fallback_ok = failure_ok = log_ok = 0
    failures = []
    for row in rows:
        got = simulate(row)
        route = got["task"] == row["task"] and got["model"] == row["config"]["model"]
        expected_fallback = bool(row["fallback_allowed"] and row["fault"] in {"429", "500", "empty"})
        fb = got["fallback"] == expected_fallback
        expected_failure = "none" if row["fault"] == "none" else ("fallback" if expected_fallback else row["fault"])
        fail = got["failure_class"] == expected_failure
        log = all(got.get(k) is not None for k in ("task", "model", "provider", "attempts", "status"))
        route_ok += route
        fallback_ok += fb
        failure_ok += fail
        log_ok += log
        if not all((route, fb, fail, log)):
            failures.append({"id": row["id"], "expected": row, "observed": got})
    n = len(rows)
    return {"n": n, "route_accuracy": round(route_ok / n, 4),
            "fallback_correctness": round(fallback_ok / n, 4),
            "failure_taxonomy_accuracy": round(failure_ok / n, 4),
            "observability_completeness": round(log_ok / n, 4),
            "failure_cases": failures,
            "failure_classes": dict(Counter(simulate(r)["failure_class"] for r in rows))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/evals/route-contract-results.json")
    args = ap.parse_args()
    rows = cases()
    result = evaluate(rows)
    result["dataset"] = {"name": "route_contract", "version": "v1", "n": len(rows)}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "failure_cases"}, ensure_ascii=False, indent=2))
    return 0 if not result["failure_cases"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

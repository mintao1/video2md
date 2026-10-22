#!/usr/bin/env python3
"""Validate evidence for the Colab source-locality and production-topology contract."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "docs/evals/colab-source-routing-eval-set.jsonl"


def load_cases() -> list[dict]:
    cases = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    ids = [case.get("id") for case in cases]
    if len(ids) != len(set(ids)) or not cases:
        raise ValueError("eval set must contain unique, non-empty ids")
    for case in cases:
        missing = {"id", "name", "scope", "probe", "pass", "forbidden"} - case.keys()
        if missing or not isinstance(case["forbidden"], list) or not case["forbidden"]:
            raise ValueError(f"invalid case {case.get('id')}: missing={sorted(missing)}")
    return cases


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    cases = load_cases()
    payload = json.loads(args.evidence.read_text(encoding="utf-8"))
    results = {item.get("id"): item for item in payload.get("results", [])}
    failures: list[str] = []
    forbidden_routes = sorted({route for case in cases for route in case["forbidden"]})
    for case in cases:
        result = results.get(case["id"])
        if not result:
            failures.append(f"{case['id']}: missing evidence")
            continue
        observed = str(result.get("observed") or "")
        if not result.get("passed") or not observed:
            failures.append(f"{case['id']}: not passed or observed evidence is empty")
        folded = observed.casefold()
        # A rejected topology is globally invalid even when it is mentioned under
        # the wrong case id; otherwise evidence could evade the gate by moving text.
        for forbidden in forbidden_routes:
            if forbidden.casefold() in folded:
                failures.append(f"{case['id']}: forbidden route: {forbidden}")
    extra = sorted(set(results) - {case["id"] for case in cases})
    if extra:
        failures.append(f"unknown evidence ids: {', '.join(extra)}")
    if failures:
        print("FAIL")
        print("\n".join(f"- {failure}" for failure in failures))
        return 1
    print(f"PASS {len(cases)}/{len(cases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

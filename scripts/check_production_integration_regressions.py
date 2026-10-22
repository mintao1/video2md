#!/usr/bin/env python3
"""Strict gate for the one-finding/one-evaluation production matrix."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

REQUIRED_IDS = {f"pir-{index:03d}" for index in range(1, 23)}

def evaluate(evidence: dict) -> list[str]:
    results = evidence.get("results", [])
    failures = [f"{r.get('id', 'missing-id')}: {r.get('observed', 'missing evidence')}"
                for r in results if r.get("status") != "pass"]
    result_ids = [result.get("id") for result in results]
    for missing in sorted(REQUIRED_IDS - set(result_ids)):
        failures.append(f"{missing}: missing evidence")
    if len(result_ids) != len(set(result_ids)):
        failures.append("matrix: duplicate evidence id")
    return failures

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    failures = evaluate(json.loads(args.evidence.read_text(encoding="utf-8")))
    if failures:
        print("FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("PASS: every production integration regression case passed")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

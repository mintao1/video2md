#!/usr/bin/env python3
"""Validate production evidence for video resolver executability and run creation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "docs/evals/video-resolver-runtime-eval-set.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    cases = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    results = {item.get("id"): item for item in
               json.loads(args.evidence.read_text(encoding="utf-8")).get("results", [])}
    forbidden = sorted({text for case in cases for text in case["forbidden"]})
    failures: list[str] = []
    for case in cases:
        result = results.get(case["id"])
        if not result:
            failures.append(f"{case['id']}: missing evidence")
            continue
        observed = str(result.get("observed") or "")
        if not result.get("passed") or not observed:
            failures.append(f"{case['id']}: failed or empty evidence")
        for text in forbidden:
            if text.casefold() in observed.casefold():
                failures.append(f"{case['id']}: forbidden state: {text}")
    if set(results) - {case["id"] for case in cases}:
        failures.append("unknown evidence id")
    if failures:
        print("FAIL")
        print("\n".join(f"- {failure}" for failure in failures))
        return 1
    print(f"PASS {len(cases)}/{len(cases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Gate production evidence for bounded Colab warm-up and shutdown behavior."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "docs/evals/colab-warm-lifecycle-eval-set.jsonl"


def load_cases() -> list[dict]:
    cases = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    ids = [case.get("id") for case in cases]
    if not cases or len(ids) != len(set(ids)):
        raise ValueError("eval ids must be unique and non-empty")
    return cases


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    cases = load_cases()
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
            failures.append(f"{case['id']}: not passed or empty observation")
        for rejected in forbidden:
            if rejected.casefold() in observed.casefold():
                failures.append(f"{case['id']}: forbidden strategy: {rejected}")
    extras = sorted(set(results) - {case["id"] for case in cases})
    if extras:
        failures.append("unknown evidence ids: " + ", ".join(extras))
    if failures:
        print("FAIL")
        print("\n".join(f"- {failure}" for failure in failures))
        return 1
    print(f"PASS {len(cases)}/{len(cases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Validate the production quality eval set and redacted release evidence."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any

REQUIRED_STAGES = ["resolve", "download", "transcribe", "frames", "ocr", "organize", "review", "publish"]
REQUIRED_CRITERIA = {"faithfulness", "coverage", "structure", "non_hallucination", "media_reference_valid"}
REQUIRED_OBSERVATIONS = {"run_id", "stage_events", "usage", "cost"}
SECRET_PATTERNS = (
    re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|client[_-]?secret|cookie)\s*[:=]"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{16,}"),
    re.compile(r"(?i)(?:postgres(?:ql)?|mysql)://[^\s]+"),
    re.compile(r"(?i)[?&](?:token|sig|signature|key)=[^\s&]+"),
)


def _finite_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {line_no}: invalid JSON: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"line {line_no}: row must be an object")
        rows.append(row)
    return rows


def validate_eval_set(rows: list[dict[str, Any]], *, minimum_rows: int = 30) -> list[str]:
    failures: list[str] = []
    ids: list[str] = []
    if len(rows) < minimum_rows:
        failures.append(f"eval-set-count: expected at least {minimum_rows} real replay rows, got {len(rows)}")
    for index, row in enumerate(rows, 1):
        prefix = f"eval-set-{index}"
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id.startswith("pq-"):
            failures.append(f"{prefix}-id: id must use pq- prefix")
        else:
            ids.append(row_id)
        input_data = row.get("input")
        expected = row.get("expectedOutput")
        metadata = row.get("metadata")
        counterexamples = row.get("counterexamples")
        if not isinstance(input_data, dict) or not input_data.get("source") or not input_data.get("owner"):
            failures.append(f"{prefix}-input: source and owner are required")
        if not isinstance(input_data, dict) or input_data.get("expectedStages") != REQUIRED_STAGES:
            failures.append(f"{prefix}-stages: expectedStages must cover the canonical pipeline")
        if not isinstance(expected, dict) or set((expected.get("criteria") or {})) != REQUIRED_CRITERIA:
            failures.append(f"{prefix}-criteria: all quality criteria are required")
        if not isinstance(expected, dict) or set(expected.get("requiredObservations") or ()) != REQUIRED_OBSERVATIONS:
            failures.append(f"{prefix}-observations: run, stages, usage and cost observations are required")
        if not isinstance(counterexamples, list) or len(counterexamples) < 3:
            failures.append(f"{prefix}-counterexamples: at least three executable counterexamples are required")
        if not isinstance(metadata, dict) or not metadata.get("source_run_id") or not metadata.get("evidence"):
            failures.append(f"{prefix}-metadata: source_run_id and evidence are required")
        if _contains_secret(row):
            failures.append(f"{prefix}-secrets: possible credential or signed URL found")
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    if duplicates:
        failures.append(f"eval-set-ids: duplicate ids: {', '.join(duplicates)}")
    return failures


def _contains_secret(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False)
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def evaluate_evidence(evidence: dict[str, Any], expected_ids: set[str], *, allow_example: bool = False) -> list[str]:
    failures: list[str] = []
    kind = evidence.get("kind")
    if kind != "production" and not (allow_example and kind == "example"):
        failures.append("evidence-kind: evidence must be kind=production")
    release = evidence.get("release") or {}
    for key in ("version", "commit", "image", "schema"):
        if not isinstance(release.get(key), str) or not release[key] or release[key].startswith("fill-"):
            failures.append(f"evidence-release-{key}: tested release metadata is required")
    cases = evidence.get("cases")
    if not isinstance(cases, list):
        failures.append("evidence-cases: cases must be a list")
        cases = []
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            failures.append("evidence-case-id: every case needs an id")
            continue
        case_id = case["id"]
        seen.add(case_id)
        if case_id not in expected_ids:
            failures.append(f"evidence-case-unknown: {case_id}")
        if case.get("status") != "pass":
            failures.append(f"evidence-case-status: {case_id} is not pass")
        if not case.get("run_id") or not case.get("evidence_ref"):
            failures.append(f"evidence-case-trace: {case_id} needs run_id and evidence_ref")
    missing = sorted(expected_ids - seen)
    if missing:
        failures.append(f"evidence-case-missing: {', '.join(missing)}")
    aggregate = evidence.get("aggregate") or {}
    checks = {
        "run_success_rate": (0.99, ">="),
        "submission_acceptance_rate": (0.999, ">="),
        "sse_first_event_p95_seconds": (3.0, "<"),
        "bundle_delivery_rate": (0.99, ">="),
        "faithfulness_mean": (0.90, ">="),
        "coverage_mean": (0.85, ">="),
    }
    for key, (threshold, operator) in checks.items():
        value = aggregate.get(key)
        if not _finite_number(value) or (operator == ">=" and value < threshold) or (operator == "<" and value >= threshold):
            failures.append(f"evidence-metric-{key}: value does not meet {operator} {threshold}")
    for key in ("security_high_risk_failures", "forbidden_claim_hits", "credential_leaks"):
        if aggregate.get(key) != 0:
            failures.append(f"evidence-zero-{key}: expected zero")
    if aggregate.get("blocked_cases", 0) != 0:
        failures.append("evidence-blocked: blocked cases require explicit review before release")
    if evidence.get("decision") != "pass":
        failures.append("evidence-decision: production quality gate decision must be pass")
    if _contains_secret(evidence):
        failures.append("evidence-secrets: possible credential or signed URL found")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--eval-set", type=Path, default=Path("docs/evals/production-quality-eval-set.jsonl"))
    parser.add_argument("--allow-example", action="store_true", help="allow the checked-in redacted template")
    args = parser.parse_args()
    try:
        rows = load_rows(args.eval_set)
        failures = validate_eval_set(rows)
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        if not isinstance(evidence, dict):
            failures.append("evidence-json: root must be an object")
        else:
            failures.extend(evaluate_evidence(evidence, {row.get("id") for row in rows}, allow_example=args.allow_example))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL\n- input: {exc}")
        return 1
    if failures:
        print("FAIL")
        print("\n".join(f"- {failure}" for failure in failures))
        return 1
    print("PASS: production quality evidence gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

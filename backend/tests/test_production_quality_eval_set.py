"""Offline contract tests for the full-chain production quality gate."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVAL_SET = ROOT / "docs/evals/production-quality-eval-set.jsonl"
EVIDENCE = ROOT / "docs/evals/production-quality-evidence.example.json"
SPEC = importlib.util.spec_from_file_location("production_quality_gate", ROOT / "scripts/check_production_quality_eval.py")
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def rows() -> list[dict]:
    return gate.load_rows(EVAL_SET)


def evidence() -> dict:
    return json.loads(EVIDENCE.read_text(encoding="utf-8"))


def test_eval_set_has_thirty_unique_full_chain_cases() -> None:
    data = rows()
    assert len(data) >= 30
    assert len({row["id"] for row in data}) == len(data)
    assert gate.validate_eval_set(data) == []


def test_each_case_has_three_counterexamples_and_stable_observation_contract() -> None:
    for row in rows():
        assert len(row["counterexamples"]) >= 3
        assert set(row["expectedOutput"]["requiredObservations"]) == gate.REQUIRED_OBSERVATIONS
        assert row["input"]["expectedStages"] == gate.REQUIRED_STAGES


def test_redacted_example_evidence_passes_only_with_explicit_example_flag() -> None:
    data = evidence()
    ids = {row["id"] for row in rows()}
    assert gate.evaluate_evidence(data, ids, allow_example=True) == []
    assert any(item.startswith("evidence-kind:") for item in gate.evaluate_evidence(data, ids))


def test_missing_case_and_failed_status_block_release() -> None:
    data = evidence()
    data["cases"] = data["cases"][:-1]
    data["cases"][0]["status"] = "blocked"
    failures = gate.evaluate_evidence(data, {row["id"] for row in rows()}, allow_example=True)
    assert any(item.startswith("evidence-case-status:") for item in failures)
    assert any(item.startswith("evidence-case-missing:") for item in failures)


def test_metrics_and_zero_tolerance_controls_are_hard_gates() -> None:
    data = evidence()
    data["aggregate"]["faithfulness_mean"] = 0.89
    data["aggregate"]["credential_leaks"] = 1
    data["aggregate"]["sse_first_event_p95_seconds"] = 3.0
    failures = gate.evaluate_evidence(data, {row["id"] for row in rows()}, allow_example=True)
    assert any(item.startswith("evidence-metric-faithfulness_mean:") for item in failures)
    assert any(item.startswith("evidence-zero-credential_leaks:") for item in failures)
    assert any(item.startswith("evidence-metric-sse_first_event_p95_seconds:") for item in failures)


def test_secrets_in_eval_rows_are_rejected() -> None:
    data = rows()
    data[0]["input"]["source"] = "https://example.test/video?token=secret-value"
    assert any(item.endswith("-secrets: possible credential or signed URL found") for item in gate.validate_eval_set(data))


def test_unknown_case_id_and_non_pass_decision_fail() -> None:
    data = evidence()
    data["cases"][0]["id"] = "pq-999"
    data["decision"] = "hold"
    failures = gate.evaluate_evidence(data, {row["id"] for row in rows()}, allow_example=True)
    assert any(item.startswith("evidence-case-unknown:") for item in failures)
    assert any(item.startswith("evidence-decision:") for item in failures)

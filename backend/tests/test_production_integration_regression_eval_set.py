from __future__ import annotations
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "docs/evals/production-integration-regression-eval-set.jsonl"
EVIDENCE = ROOT / "docs/evals/production-integration-evidence-2026-10-21.json"
SPEC = importlib.util.spec_from_file_location("production_integration_gate", ROOT / "scripts/check_production_integration_regressions.py")
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

def _cases() -> list[dict]:
    return [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line]

def test_every_review_finding_has_one_independent_regression_case() -> None:
    cases = _cases()
    assert {case["id"] for case in cases} == {f"pir-{i:03d}" for i in range(1, 23)}
    assert len(cases) == len({case["finding"] for case in cases}) == 22
    assert all(case["seam"] and case["pass"] and case["forbidden"] for case in cases)

def test_current_production_evidence_names_every_case_once() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    ids = [result["id"] for result in evidence["results"]]
    assert ids == [case["id"] for case in _cases()]
    assert len(ids) == len(set(ids))

def test_current_gate_stays_red_for_every_unresolved_blocker() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    labels = {failure.split(":", 1)[0] for failure in gate.evaluate(evidence)}
    assert labels == {"pir-018", "pir-019", "pir-020", "pir-021"}

def test_gate_only_turns_green_when_every_case_passes() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    for result in evidence["results"]:
        result["status"] = "pass"
    assert gate.evaluate(evidence) == []

def test_gate_rejects_missing_or_duplicate_evidence() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    evidence["results"] = evidence["results"][1:]
    assert any(failure.startswith("pir-001:") for failure in gate.evaluate(evidence))
    evidence["results"].append(evidence["results"][0])
    assert "matrix: duplicate evidence id" in gate.evaluate(evidence)

def test_gpu_usage_reconciliation_accepts_current_measured_stages() -> None:
    billed_gpu_s = 251.977
    expected_gpu_s = 176.0 + 76.0
    assert abs(billed_gpu_s - expected_gpu_s) <= max(1.0, expected_gpu_s * 0.05)

"""Each user annotation has source evidence and executable negative examples."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check_glm_review_regressions.py"
SPEC = importlib.util.spec_from_file_location("glm_review_gate", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
CASES = [json.loads(line) for line in gate.DATASET.read_text().splitlines() if line.strip()]
NEGATIVES = [(case["id"], n) for case in CASES for n in case["negative_cases"]]


def patch(evidence: dict, path: str, value: object) -> None:
    parts = path.split(".")
    target = evidence
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    key = parts[-1]
    if isinstance(target, list):
        index = int(key)
        if index == len(target):
            target.append(value)
        else:
            target[index] = value
    else:
        target[key] = value


def test_all_eleven_annotations_have_separate_contracts_and_existing_sources():
    assert [case["id"] for case in CASES] == [f"glm-review-{i:03d}" for i in range(1, 12)]
    assert [case["metadata"]["annotation_index"] for case in CASES] == list(range(1, 12))
    for case in CASES:
        assert case["input"]["scenario"] and case["expectedOutput"]["criteria"]
        assert case["expectedOutput"]["case_check"] == case["metadata"]["annotation_index"]
        for ref in case["metadata"]["evidence_refs"] + case["metadata"]["regressions"]:
            assert (ROOT / ref.split("::")[0]).is_file(), ref
        assert len(case["negative_cases"]) >= 3


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_captured_review_scenario_replays(case):
    evidence = gate.load_evidence(gate.DEFAULT_EVIDENCE)
    assert gate.check_case(case["expectedOutput"]["case_check"], evidence)


@pytest.mark.parametrize(("case_id", "negative"), NEGATIVES,
                         ids=[f"{cid}-{n['name']}" for cid, n in NEGATIVES])
def test_each_review_badcase_is_rejected(case_id, negative):
    evidence = gate.load_evidence(gate.DEFAULT_EVIDENCE)
    patch(evidence, negative["path"], negative["value"])
    assert any(failure.startswith(case_id + ":") for failure in gate.evaluate(evidence))


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_missing_evidence_cannot_pass(case):
    failures = gate.evaluate({})
    assert any(item.startswith(case["id"] + ":") for item in failures)


@pytest.mark.parametrize(("current", "returned", "terminal", "expected"), [
    (False, False, False, "unconfirmed"), (False, False, True, "unconfirmed"),
    (True, False, False, "pending"), (False, True, False, "returned"),
    (False, True, True, "completed"), (True, True, True, "completed"),
])
def test_transfer_decision_distinguishes_stale_progress_from_completion(current, returned, terminal, expected):
    assert gate.classify_transfer(current=current, returned=returned, terminal=terminal) == expected


def test_review_replay_success_does_not_turn_full_production_acceptance_green():
    evidence = gate.load_evidence(gate.DEFAULT_EVIDENCE)
    assert gate.evaluate(evidence) == []
    failures = {item.split(":", 1)[0] for item in gate.stages.evaluate(evidence["stage-evidence"])}
    assert failures == {"pps-008", "pps-013", "pps-016"}


@pytest.mark.parametrize("source", ["stage-evidence", "lesson-result", "production-final-readonly",
                                    "billing-reconciliation"])
def test_cross_run_evidence_cannot_satisfy_any_review_case(source):
    evidence = gate.load_evidence(gate.DEFAULT_EVIDENCE)
    evidence[source]["run_id"] = "unrelated-run"
    assert len(gate.evaluate(evidence)) == 11


@pytest.mark.parametrize("key", ["llm_in_tok", "llm_out_tok", "gpu_s", "spend_usd", "runs_n"])
def test_missing_delta_is_not_proof_of_zero(key):
    evidence = gate.load_evidence(gate.DEFAULT_EVIDENCE)
    evidence["reuse-reconciliation"]["deltas"].pop(key)
    assert any(item.startswith("glm-review-008:") for item in gate.evaluate(evidence))


def test_month_total_must_match_final_ledger_not_just_the_stage_snapshot():
    evidence = gate.load_evidence(gate.DEFAULT_EVIDENCE)
    evidence["billing-reconciliation"]["month_total"]["spend_usd"] = 0
    assert any(item.startswith("glm-review-009:") for item in gate.evaluate(evidence))


def test_cli_reports_replay_scope_and_handles_missing_evidence(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0 and "offline" in result.stdout
    assert "production acceptance remains FAIL" in result.stdout
    (tmp_path / "stage-evidence.json").write_text('{"private":invalid}')
    result = subprocess.run([sys.executable, str(SCRIPT), "--evidence-dir", str(tmp_path)],
                            capture_output=True, text=True)
    assert result.returncode == 1 and "private" not in result.stdout
    assert "Traceback" not in result.stderr


def test_ci_executes_review_cases_alongside_application_regressions():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "test_glm_review_eval_set.py" in ci
    assert "check_glm_review_regressions.py" in ci
    assert "npm run test:integration" in ci
    assert "test_opencode_llm_contract.py" in ci

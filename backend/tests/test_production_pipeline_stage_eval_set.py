"""Executable per-stage regressions for production evidence, with no paid calls."""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "docs/evals/production-pipeline-stage-eval-set.jsonl"
EXAMPLE = ROOT / "docs/evals/production-pipeline-stage-evidence.example.json"
LATEST = ROOT / "docs/testing/2026-10-21/billing-retest/stage-evidence.json"
SCRIPT = ROOT / "scripts/check_production_pipeline_stages.py"
SPEC = importlib.util.spec_from_file_location("production_stage_gate", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
CASES = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
IDS = [f"pps-{index:03d}" for index in range(1, 25)]
NEGATIVES = [(row["id"], negative) for row in CASES for negative in row["negative_cases"]]


def example() -> dict:
    return json.loads(EXAMPLE.read_text())


def row(evidence: dict, case_id: str) -> dict:
    return next(result for result in evidence["results"] if result["id"] == case_id)


def labels(evidence: dict) -> set[str]:
    return {failure.split(":", 1)[0] for failure in gate.evaluate(evidence, allow_synthetic=True)}


def test_every_step_has_inputs_expected_measurements_and_executable_badcases() -> None:
    assert [case["id"] for case in CASES] == IDS == gate.REQUIRED_IDS
    assert len({case["step"] for case in CASES}) == 24
    assert all(case["input"] and case["expectedOutput"] and case["seam"] and case["regression"] for case in CASES)
    assert all(case["evidence_fields"] and case["negative_cases"] and case["secrets"] for case in CASES)
    for case in CASES:
        assert {n["expected_failure"] for n in case["negative_cases"]} == {case["id"]}
        assert len({n["name"] for n in case["negative_cases"]}) == len(case["negative_cases"])


def test_synthetic_example_is_consistent_but_cannot_be_production_acceptance() -> None:
    evidence = example()
    assert labels(evidence) == set()
    assert gate.evaluate(evidence) == ["matrix: synthetic or unspecified evidence cannot pass production acceptance"]


@pytest.mark.parametrize(("case_id", "negative"), NEGATIVES,
                         ids=[f"{case_id}-{n['name']}" for case_id, n in NEGATIVES])
def test_each_badcase_rejects_the_affected_step_even_with_a_pass_label(case_id: str, negative: dict) -> None:
    evidence = example()
    row(evidence, case_id)["metrics"].update(copy.deepcopy(negative["metrics_patch"]))
    assert row(evidence, case_id)["status"] == "pass"
    assert case_id in labels(evidence)


@pytest.mark.parametrize("case_id", IDS)
@pytest.mark.parametrize("defect", ["missing-stage", "missing-metrics", "wrong-run", "missing-source", "blocked", "not-tested"])
def test_every_step_fails_closed_on_missing_or_unproven_evidence(case_id: str, defect: str) -> None:
    evidence = example()
    result = row(evidence, case_id)
    if defect == "missing-stage":
        evidence["results"].remove(result)
    elif defect == "missing-metrics":
        result.pop("metrics")
    elif defect == "wrong-run":
        result["metrics"]["run_id"] = "other-run"
    elif defect == "missing-source":
        result["evidence_refs"] = []
    else:
        result["status"] = defect
    assert case_id in labels(evidence)


@pytest.mark.parametrize(("case_id", "field"), [
    ("pps-003", "bytes"), ("pps-004", "duration_s"), ("pps-005", "duration_s"),
    ("pps-006", "duration_s"), ("pps-007", "gpu_s"), ("pps-008", "idle_elapsed_s"),
    ("pps-010", "llm_in_tok"), ("pps-010", "llm_out_tok"),
    ("pps-011", "llm_in_mtok"), ("pps-011", "llm_out_mtok"), ("pps-011", "gpu_min"),
    ("pps-012", "cost_usd"), ("pps-013", "markdown_chars"),
])
@pytest.mark.parametrize("invalid", [None, True, "1", -1, float("nan"), float("inf"), float("-inf")])
def test_invalid_measurements_cannot_be_coerced_to_success(case_id: str, field: str, invalid: object) -> None:
    evidence = example()
    row(evidence, case_id)["metrics"][field] = invalid
    assert case_id in labels(evidence)


def test_gpu_accounting_uses_actual_execution_device_and_stage_durations() -> None:
    evidence = example()
    # CPU clips must not inflate GPU seconds; the run ledger must also agree.
    row(evidence, "pps-007")["metrics"]["gpu_s"] = 181
    assert "pps-007" in labels(evidence)
    # The same clip on a GPU must now be included in both totals.
    row(evidence, "pps-006")["metrics"].update(device="gpu", gpu_seconds=1)
    row(evidence, "pps-010")["metrics"]["gpu_s"] = 181
    assert "pps-006" not in labels(evidence)
    assert "pps-007" not in labels(evidence)
    assert "pps-012" in labels(evidence)  # old cost is stale after usage changes


def test_shutdown_boundary_and_explicit_fallback_are_valid() -> None:
    evidence = example()
    row(evidence, "pps-008")["metrics"]["idle_elapsed_s"] = 45
    row(evidence, "pps-018")["metrics"].update(mode="heuristic_fallback", fallback_reason_visible=True)
    assert labels(evidence) == set()


def test_duplicate_unknown_or_malformed_stage_never_passes() -> None:
    evidence = example()
    evidence["results"].append(copy.deepcopy(evidence["results"][0]))
    assert "matrix" in labels(evidence)
    for bad in ({"id": "pps-999"}, {"id": []}, None):
        evidence = example()
        evidence["results"].append(bad)
        assert "matrix" in labels(evidence)
    for bad in (None, [], "bad", {}):
        assert gate.evaluate(bad)


def test_changing_historical_red_labels_to_pass_cannot_forge_production_success() -> None:
    evidence = json.loads((ROOT / "docs/evals/production-pipeline-stage-evidence-2026-10-21.json").read_text())
    for result in evidence["results"]:
        result["status"] = "pass"
    assert set(IDS) <= labels(evidence)


def test_real_312e97ae_evidence_stays_red_with_precise_unmeasured_steps() -> None:
    evidence = json.loads(LATEST.read_text())
    assert evidence["kind"] == "production" and evidence["run_id"] == "312e97ae"
    passed = {"pps-001", "pps-002", "pps-005", "pps-007", "pps-015", "pps-019"}
    assert labels(evidence) == set(IDS) - passed
    assert row(evidence, "pps-009")["metrics"]["http_status"] == 403
    assert row(evidence, "pps-010")["metrics"]["gpu_s"] == 204.669
    assert all(row(evidence, case)["status"] == "not_tested" for case in IDS[-3:])


def test_cli_success_is_explicitly_synthetic_and_real_failure_has_nonzero_exit() -> None:
    synthetic = subprocess.run([sys.executable, str(SCRIPT), str(EXAMPLE), "--allow-synthetic"], capture_output=True, text=True)
    assert synthetic.returncode == 0 and "synthetic self-test" in synthetic.stdout
    refused = subprocess.run([sys.executable, str(SCRIPT), str(EXAMPLE)], capture_output=True, text=True)
    assert refused.returncode == 1
    actual = subprocess.run([sys.executable, str(SCRIPT), str(LATEST)], capture_output=True, text=True)
    assert actual.returncode == 1 and "pps-009:" in actual.stdout


def test_invalid_evidence_file_has_nonzero_exit_without_traceback_or_raw_content(tmp_path: Path) -> None:
    source = tmp_path / "invalid.json"
    source.write_text('{"secret": invalid}')
    result = subprocess.run([sys.executable, str(SCRIPT), str(source)], capture_output=True, text=True)
    assert result.returncode == 1 and "JSONDecodeError" in result.stdout
    assert "secret" not in result.stdout and "Traceback" not in result.stderr


def test_ci_runs_both_stage_regressions_and_frontend_failure_guards() -> None:
    ci = (ROOT / '.github/workflows/ci.yml').read_text()
    production = (ROOT / '.github/workflows/production-pipeline-gate.yml').read_text()
    package = json.loads((ROOT / 'frontend/web/package.json').read_text())
    for filename in ('test_production_pipeline_stage_eval_set.py', 'test_production_billing_eval_set.py',
                     'test_video_resolver_runtime_eval_set.py', 'test_colab_warm_lifecycle_eval_set.py'):
        assert filename in ci
    assert 'npm run test:regression' in ci
    assert 'uv run pytest tests/test_llm.py tests/test_opencode_llm_contract.py -q' in ci
    assert package['scripts']['test:regression'] == 'node --test tests/*.regression.test.mjs'
    assert 'check_production_pipeline_stages.py "$EVIDENCE_FILE"' in production
    assert '--allow-synthetic' not in production


def test_resolver_test_collection_does_not_load_app_configuration_or_local_secrets() -> None:
    source = ROOT / 'backend/tests/test_video_resolver_runtime_eval_set.py'
    code = '''
import importlib.util, sys
spec = importlib.util.spec_from_file_location('resolver_test_collection', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert 'video2pdf.config' not in sys.modules
assert 'video2pdf.api' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-c', code, str(source)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_user_approved_free_gpu_policy_keeps_usage_without_inventing_a_charge() -> None:
    evidence = example()
    row(evidence, 'pps-011')['metrics'].update(gpu_min=0, gpu_policy='free', gpu_free_reason='User-approved Colab free tier')
    row(evidence, 'pps-012')['metrics']['cost_usd'] = 0.002
    row(evidence, 'pps-019')['metrics']['after']['spend_usd'] = 1.002
    assert labels(evidence) == set()
    row(evidence, 'pps-011')['metrics'].pop('gpu_free_reason')
    assert 'pps-011' in labels(evidence)


def test_cached_input_discount_is_part_of_the_cost_formula() -> None:
    evidence = example()
    row(evidence, 'pps-010')['metrics']['llm_cached_in_tok'] = 900
    row(evidence, 'pps-011')['metrics']['llm_cached_in_mtok'] = 0.01
    assert 'pps-012' in labels(evidence)
    row(evidence, 'pps-012')['metrics']['cost_usd'] = 0.301109
    row(evidence, 'pps-019')['metrics']['after']['spend_usd'] = 1.301109
    assert labels(evidence) == set()

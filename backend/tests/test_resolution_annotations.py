"""43 annotations: historical replay, mutations, operational and honesty guards."""

from __future__ import annotations

import importlib.util
import json
import shlex
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check_resolution_annotations.py"
spec = importlib.util.spec_from_file_location("resolution_annotations", SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
CASES = gate.cases()
NEGATIVES = [(c, n) for c in CASES for n in c["counterexamples"]]


def mutate(data, pointer, value, *, delete=False):
    keys = [s.replace("~1", "/").replace("~0", "~") for s in pointer[1:].split("/")]
    for key in keys[:-1]:
        data = data[int(key)] if isinstance(data, list) else data[key]
    key = int(keys[-1]) if isinstance(data, list) else keys[-1]
    if delete:
        del data[key]
    else:
        data[key] = value


def test_43_annotations_each_have_real_evidence_tests_and_counterexamples():
    assert gate.validate(CASES) == []
    assert len({c["metadata"]["source_message_id"] for c in CASES}) == 43
    assert sum(len(c["counterexamples"]) for c in CASES) == 185
    assert CASES[18]["metadata"]["annotation_index"] == 19  # selected text without a comment is covered too


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_each_annotation_replays_hashed_original_observations(case):
    assert gate.check_case(case, gate.load_case(case))


@pytest.mark.parametrize("case,negative", NEGATIVES,
                         ids=[c["id"] + ":" + n["name"] for c, n in NEGATIVES])
def test_each_annotation_counterexample_is_actually_rejected(case, negative):
    data = gate.load_case(case)
    mutate(data, negative["path"], negative["value"])
    assert not gate.check_case(case, data)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_missing_measurement_cannot_replay_as_success(case):
    data = gate.load_case(case)
    mutate(data, case["expectedOutput"]["assertions"][0]["path"], None, delete=True)
    assert not gate.check_case(case, data)


@pytest.mark.parametrize("defect", ["missing", "duplicate", "order", "selector", "private-path", "no-negatives"])
def test_coverage_contract_cannot_silently_lose_items_or_executable_tests(defect):
    rows = deepcopy(CASES)
    if defect == "missing":
        rows.pop()
    elif defect == "duplicate":
        rows[-1] = rows[0]
    elif defect == "order":
        rows.reverse()
    elif defect == "selector":
        rows[0]["metadata"]["regressions"][0] += "_missing"
    elif defect == "private-path":
        next(iter(rows[0]["input"]["sources"].values()))["path"] = "../private.json"
    else:
        rows[0]["counterexamples"] = []
    assert gate.validate(rows)


def test_source_digest_and_public_checkout_boundary_enforced(tmp_path):
    path = tmp_path / "docs/testing/observation.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"status":"changed"}')
    case = deepcopy(CASES[0])
    case["input"]["sources"] = {"source": {"path": str(path.relative_to(tmp_path)), "sha256": "0" * 64}}
    with pytest.raises(ValueError, match="digest"):
        gate.load_case(case, tmp_path)
    outside = tmp_path.parent / "private-fixture"
    outside.mkdir(exist_ok=True)
    (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="non-local"):
        gate.local_path(tmp_path, "escape/secret.json")
    with pytest.raises(ValueError, match="non-public"):
        gate.evidence_path(tmp_path, ".env")


@pytest.mark.parametrize("defect", ["timeout", "redirect-only", "refresh-expired", "missing-ui", "bad-clock", "duplicate", "other-release"])
def test_browser_timeout_and_status_alone_never_prove_login(defect):
    data = gate.load_case(CASES[27])
    assert gate.login(data)
    row = data["login"]["observations"][0]
    if defect == "timeout":
        row["outcome"] = "click-timeout-unverified"
    elif defect == "redirect-only":
        row["me_status"] = 302
    elif defect == "refresh-expired":
        row["refresh_status"] = 401
    elif defect == "missing-ui":
        row.pop("screenshot")
    elif defect == "bad-clock":
        row["elapsed_s"] = 1
    elif defect == "duplicate":
        data["login"]["observations"][1]["attempt_id"] = row["attempt_id"]
    else:
        data["login"]["release"] = "old"
    if defect == "other-release":
        assert not gate.check_case(CASES[24], data)
    else:
        assert not gate.login(data)


@pytest.mark.parametrize("field,value", [
    ("control_id", "old-job"), ("cuda_available", False), ("cuda_devices", 0),
    ("upload_file_count", 59), ("kernel_status", "queued"), ("error_count", 1),
    ("provider_source_sha256", "0" * 64), ("onnxruntime_version", ""),
])
def test_gpu_packet_probe_is_bound_to_input_runtime_and_source(field, value):
    data = gate.load_case(CASES[22])
    assert gate.gpu_packet(data)
    data["probe"][field] = value
    assert not gate.gpu_packet(data)


@pytest.mark.parametrize("defect", ["input", "output", "cache", "cost", "duplicate", "count"])
def test_request_rows_are_recalculated_instead_of_trusting_match_flags(defect):
    data = gate.load_case(CASES[29])
    assert gate.reconcile(data)
    record = data["balance"]
    if defect in ("input", "output", "cache"):
        record["requests"][0][{"input": "input_tokens", "output": "output_tokens", "cache": "cached_tokens"}[defect]] += 1
    elif defect == "cost":
        record["requests"][0]["attribution_usd"] = "9.99"
    elif defect == "duplicate":
        record["requests"][1]["request_id"] = record["requests"][0]["request_id"]
    else:
        record["provider_request_count"] = 4
    assert record["usage_matches"] and record["attribution_matches"]
    assert not gate.reconcile(data)


def test_successful_delivery_does_not_close_failure_or_performance_gaps():
    data = gate.load_case(CASES[42])
    assert gate.delivered(data) and gate.reconcile(data)
    failures = gate.production_gaps()
    assert len(failures) == 3
    assert any("failure-cost-001:" in f for f in failures)
    assert any("failure-cost-002:" in f for f in failures)
    assert any("media-readiness:" in f for f in failures)
    matrix = deepcopy(data["strict"])
    for row in matrix["results"]:
        row["status"] = "pass"
    errors = gate.module("check_production_failure_cost").evaluate(matrix, expected_release=matrix["release"])
    assert len(errors) == 2  # cannot close by editing labels


def test_collection_has_no_project_configuration_side_effects():
    code = 'import runpy,sys; runpy.run_path(sys.argv[1]); assert "video2md.config" not in sys.modules'
    for filename in ("test_resolution_annotations.py", "test_resolution_operations.py"):
        result = subprocess.run([sys.executable, "-c", code, str(Path(__file__).with_name(filename))],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr


def test_cli_replay_cannot_be_mistaken_for_closed_production_and_suppresses_private_input(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0 and "offline" in result.stdout and "no new production acceptance" in result.stdout
    result = subprocess.run([sys.executable, str(SCRIPT), "--require-closed"], capture_output=True, text=True)
    assert result.returncode == 1 and result.stdout.count("OPEN ") == 3
    invalid = tmp_path / "invalid.jsonl"
    invalid.write_text('{"SECRET_CANARY":broken}')
    result = subprocess.run([sys.executable, str(SCRIPT), "--dataset", str(invalid)], capture_output=True, text=True)
    assert result.returncode == 1 and "SECRET_CANARY" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("row", [None, [], "SECRET_CANARY", 7, True])
def test_malformed_jsonl_rows_fail_closed_without_disclosing_input(tmp_path, row):
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(row) + "\n")
    result = subprocess.run([sys.executable, str(SCRIPT), "--dataset", str(path)],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert "SECRET_CANARY" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_required_ci_executes_replay_and_operational_regressions():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    for filename in ("tests/test_resolution_annotations.py", "tests/test_resolution_operations.py"):
        job = workflow["jobs"]["test"]
        steps = [s for s in job["steps"] if filename in shlex.split(s.get("run", ""))]
        assert len(steps) == 1 and steps[0]["run"].startswith("uv run pytest ")
        assert "if" not in steps[0] and not steps[0].get("continue-on-error", False)
        assert "if" not in job and not job.get("continue-on-error", False)
    lint = "\n".join(s.get("run", "") for s in workflow["jobs"]["lint"]["steps"])
    assert "../scripts/check_resolution_annotations.py" in lint


@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"), "37018"])
def test_untyped_numeric_claims_fail_closed(value):
    assert not gate.matches({"v": value}, {"path": "/v", "op": "eq", "value": 37018})

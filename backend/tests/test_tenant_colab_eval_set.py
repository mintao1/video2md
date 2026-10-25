"""Keep the tenant login catalog linked to real application tests, offline only."""
from __future__ import annotations

import copy
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check_tenant_colab_eval_set.py"
spec = importlib.util.spec_from_file_location("tenant_colab_catalog", SCRIPT)
assert spec and spec.loader
catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog)


def test_current_catalog_covers_all_requirements_and_retires_superseded_sets():
    rows = catalog.load()
    assert catalog.validate(rows) == []
    assert len(rows) == 13
    assert sum(len(row["counterexamples"]) for row in rows) == 31


@pytest.mark.parametrize("defect", ["missing", "duplicate", "unknown", "no-input", "no-criteria",
                                  "no-negatives", "positive-negative", "missing-test", "missing-function",
                                  "wrong-annotation", "production-claim", "no-privacy"])
def test_incomplete_or_unlinked_catalog_cannot_pass(defect):
    rows = copy.deepcopy(catalog.load())
    row = rows[0]
    if defect == "missing":
        rows.pop()
    elif defect == "duplicate":
        rows.append(copy.deepcopy(row))
    elif defect == "unknown":
        row["id"] = "tenant-colab-999"
    elif defect == "no-input":
        row.pop("input")
    elif defect == "no-criteria":
        row["expectedOutput"] = {}
    elif defect == "no-negatives":
        row["counterexamples"] = []
    elif defect == "positive-negative":
        row["counterexamples"][0]["expectedOutput"]["accept"] = True
    elif defect == "missing-test":
        row["metadata"]["regressions"] = ["backend/tests/not-present.py::test_missing"]
    elif defect == "missing-function":
        row["metadata"]["regressions"] = ["backend/tests/test_colab_auth.py::test_not_present"]
    elif defect == "wrong-annotation":
        row["metadata"]["annotation_index"] = 2
    elif defect == "production-claim":
        row["metadata"]["offline_can_close_production"] = True
    elif defect == "no-privacy":
        row["metadata"].pop("privacy")
    assert catalog.validate(rows)


def test_catalog_cli_checks_definitions_without_claiming_production_or_echoing_bad_input(tmp_path):
    passed = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert passed.returncode == 0 and "13 cases, 31 counterexamples" in passed.stdout
    assert "No production acceptance" in passed.stdout
    source = tmp_path / "bad.jsonl"
    source.write_text("private-invalid-input")
    rejected = subprocess.run([sys.executable, str(SCRIPT), "--dataset", str(source)], capture_output=True, text=True)
    assert rejected.returncode == 1
    assert "private-invalid-input" not in rejected.stdout + rejected.stderr


def test_ci_executes_application_boundaries_as_well_as_the_catalog():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    for filename in ("test_tenant_colab_eval_set.py", "test_colab_auth.py", "test_colab_cli.py"):
        assert filename in ci
    assert "npm run test:integration" in ci


def test_application_runner_uses_disposable_stores_and_does_not_inherit_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("V2MD_SECRETS_FILE", str(tmp_path / "real-secrets"))
    monkeypatch.setenv("LLM_API_KEY", "private-sentinel")
    monkeypatch.setenv("V2MD_RUNS_FILE", str(tmp_path / "real-runs.json"))
    calls = []

    def run(command, *, cwd, env, check):
        calls.append((command, cwd))
        assert env["V2MD_SECRETS_FILE"] != str(tmp_path / "real-secrets")
        assert not Path(env["V2MD_SECRETS_FILE"]).exists()
        assert env["LLM_API_KEY"] == ""
        assert env["V2MD_RUNS_FILE"] != str(tmp_path / "real-runs.json")
        assert env["V2MD_AUTH"] == "off" and env["V2MD_FAKE_LLM"] == "1"
        assert env.get("V2MD_REMOTE", "") == ""
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(catalog.subprocess, "run", run)
    assert catalog.run_regressions() == 0
    assert len(calls) == 2
    assert "test_colab_auth.py" in " ".join(calls[0][0])
    assert "colab-tenant.integration.test.mjs" in " ".join(calls[1][0])

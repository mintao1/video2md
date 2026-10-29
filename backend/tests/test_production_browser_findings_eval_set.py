"""Offline contract tests for the 2026-10-26 production browser finding set."""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check_production_browser_findings.py"
spec = importlib.util.spec_from_file_location("production_browser_findings", SCRIPT)
gate = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(gate)
CASES = gate.cases()
NEGATIVES = [(case, negative) for case in CASES for negative in case["counterexamples"]]


def mutate(packet: object, pointer: str, value: object) -> None:
    keys = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
    target = packet
    for key in keys[:-1]:
        target = target[int(key)] if isinstance(target, list) else target[key]
    key = int(keys[-1]) if isinstance(target, list) else keys[-1]
    target[key] = value


def check_for(case: dict) -> str:
    return case["expectedOutput"]["checks"][0]


def test_catalog_is_complete_and_does_not_close_live_findings() -> None:
    assert gate.validate(CASES) == []
    assert [case["id"] for case in CASES] == [f"pb-{index:03d}" for index in range(1, 9)]
    assert all(case["metadata"]["offline_can_close_production"] is False for case in CASES)
    assert sum(len(case["counterexamples"]) for case in CASES) >= 30


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_each_synthetic_positive_control_passes(case: dict) -> None:
    assert gate.CHECKS[check_for(case)](case["input"]["sample"]) is True


@pytest.mark.parametrize(
    "case,negative",
    NEGATIVES,
    ids=[f"{case['id']}:{negative['name']}" for case, negative in NEGATIVES],
)
def test_each_counterexample_fails_closed(case: dict, negative: dict) -> None:
    packet = copy.deepcopy(case["input"]["sample"])
    mutate(packet, negative["path"], negative["value"])
    assert gate.CHECKS[negative["check"]](packet) is False


def test_current_observations_remain_open_until_new_production_evidence() -> None:
    outcomes = {case["id"]: case["metadata"]["historical_outcome"] for case in CASES}
    assert outcomes["pb-002"] == "blocked"
    assert outcomes["pb-004"] == "fail"
    assert outcomes["pb-005"] == "fail"
    assert outcomes["pb-007"] == "blocked_gpu_pool"
    assert all(case["metadata"]["offline_can_close_production"] is False for case in CASES)


def test_sensitive_literals_are_rejected_without_echoing_source() -> None:
    rows = copy.deepcopy(CASES)
    rows[0]["input"]["sample"]["debug"] = "Bearer live-secret-value"
    failures = gate.validate(rows)
    assert any("literal secret" in failure for failure in failures)
    assert "live-secret-value" not in "\n".join(failures)


@pytest.mark.parametrize("bad_row", [None, True, [], "SECRET_CANARY", {"id": "invalid"}])
def test_invalid_catalog_cli_fails_without_traceback_or_echo(tmp_path: Path, bad_row: object) -> None:
    path = tmp_path / "invalid.jsonl"
    path.write_text(json.dumps(bad_row) + "\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPT), "--dataset", str(path)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 1
    assert "SECRET_CANARY" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_cli_separates_offline_contracts_from_production_acceptance() -> None:
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0
    assert "8 offline" in result.stdout
    assert "no new production acceptance" in result.stdout

    closed = subprocess.run([sys.executable, str(SCRIPT), "--require-closed"],
                            capture_output=True, text=True, timeout=20)
    assert closed.returncode == 1
    assert "OPEN pb-002" in closed.stdout
    assert "OPEN pb-007" in closed.stdout

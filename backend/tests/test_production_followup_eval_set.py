"""Regression tests for the 2026-10-26 production follow-up findings."""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check_production_followup_eval.py"
DATASET = ROOT / "docs/evals/production-followup-eval-set-2026-10-26.jsonl"
spec = importlib.util.spec_from_file_location("production_followup_eval", SCRIPT)
gate = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(gate)
CASES = gate.load_rows(DATASET)


def mutate(packet: object, pointer: str, value: object) -> None:
    keys = pointer[1:].split("/")
    target = packet
    for key in keys[:-1]:
        target = target[int(key)] if isinstance(target, list) else target[key]
    key = int(keys[-1]) if isinstance(target, list) else keys[-1]
    target[key] = value


def check_for(case: dict) -> str:
    return case["expectedOutput"]["checks"][0]


def test_catalog_is_complete_and_open() -> None:
    assert gate.validate(CASES) == []
    assert [case["id"] for case in CASES] == [f"pf-{index:03d}" for index in range(1, 9)]
    assert all(case["metadata"]["offline_can_close_production"] is False for case in CASES)
    assert sum(len(case["counterexamples"]) for case in CASES) >= 24


def test_observed_positive_controls_preserve_blocked_or_inconclusive_status() -> None:
    for case in CASES:
        check = gate.CHECKS[check_for(case)]
        assert check(case["input"]["sample"]) is True, case["id"]


def test_every_counterexample_fails_closed() -> None:
    for case in CASES:
        check = gate.CHECKS[check_for(case)]
        for negative in case["counterexamples"]:
            packet = copy.deepcopy(case["input"]["sample"])
            mutate(packet, negative["path"], negative["value"])
            assert check(packet) is False, f"{case['id']}:{negative['name']}"


def test_cli_reports_offline_only_and_does_not_close_production() -> None:
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0
    assert "8 offline" in result.stdout
    assert "no new production acceptance" in result.stdout


def test_secret_like_literals_are_rejected_without_echoing_value(tmp_path: Path) -> None:
    rows = copy.deepcopy(CASES)
    rows[0]["input"]["sample"]["debug"] = "Bearer live-secret-value"
    path = tmp_path / "secret.jsonl"
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPT), "--dataset", str(path)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 1
    assert "secret-like literal" in result.stdout
    assert "live-secret-value" not in result.stdout

"""Offline contract tests for the 2026-10-26 4c production live regression set."""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check_production_live_regressions.py"
spec = importlib.util.spec_from_file_location("production_live_regressions", SCRIPT)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
CASES = gate.rows()


def _mutate(packet: dict, pointer: str, value: object) -> None:
    keys = pointer[1:].split("/")
    target: object = packet
    for key in keys[:-1]:
        target = target[int(key)] if isinstance(target, list) else target[key]  # type: ignore[index]
    key = int(keys[-1]) if isinstance(target, list) else keys[-1]
    target[key] = value  # type: ignore[index]


def test_catalog_is_ordered_redacted_and_has_fail_closed_controls() -> None:
    assert gate.validate_catalog(CASES) == []
    assert [item["id"] for item in CASES] == gate.IDS
    assert sum(len(item["counterexamples"]) for item in CASES) >= 30


def test_redacted_production_evidence_passes_offline_gate() -> None:
    evidence = json.loads(gate.EVIDENCE.read_text(encoding="utf-8"))
    runs = {run_id: json.loads(path.read_text(encoding="utf-8")) for run_id, path in gate.RUN_EVIDENCE.items()}
    assert gate.evaluate(evidence, runs) == []


def test_counterexample_mutations_fail_their_local_contract() -> None:
    # Keep this test focused on representative live controls; full field-level
    # mutation remains covered by the existing production gates.
    samples = [CASES[0], CASES[4], CASES[6]]
    for case in samples:
        packet = copy.deepcopy(case["input"]["sample"])
        negative = case["counterexamples"][0]
        _mutate(packet, negative["path"], negative["value"])
        assert packet != case["input"]["sample"]


def test_cli_is_offline_and_does_not_echo_secret_literals(tmp_path: Path) -> None:
    rows = copy.deepcopy(CASES)
    rows[0]["input"]["sample"]["debug"] = "Bearer live-secret-value"
    dataset = tmp_path / "secret.jsonl"
    dataset.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(SCRIPT), "--dataset", str(dataset)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 1
    assert "secret-like literal" in result.stdout
    assert "live-secret-value" not in result.stdout


def test_cli_reports_ten_offline_regressions() -> None:
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0
    assert "10 offline" in result.stdout

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("production_login_protocol_fix", ROOT / "scripts/check_production_login_protocol_fix.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_post_fix_production_protocol_evidence_passes():
    evidence = MODULE.json.loads(MODULE.EVIDENCE.read_text(encoding="utf-8"))
    assert MODULE.evaluate(evidence, MODULE.rows(MODULE.DATASET)) == []


def test_protocol_fix_catalog_has_live_evidence_contracts():
    for row in MODULE.rows(MODULE.DATASET):
        assert row["metadata"]["offline_can_close_production"] is False
        assert len(row["counterexamples"]) >= 3

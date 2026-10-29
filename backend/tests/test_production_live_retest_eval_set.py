from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("production_live_retest", ROOT / "scripts/check_production_live_retest.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_fresh_production_retest_evidence_passes() -> None:
    evidence = MODULE.json.loads(MODULE.EVIDENCE.read_text(encoding="utf-8"))
    catalog = MODULE._rows(MODULE.DATASET)
    assert MODULE.evaluate(evidence, catalog) == []


def test_retest_catalog_is_real_evidence_only() -> None:
    for row in MODULE._rows(MODULE.DATASET):
        assert row["metadata"]["offline_can_close_production"] is False
        assert len(row["counterexamples"]) >= 3

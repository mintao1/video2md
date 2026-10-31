"""Strict regression contracts for the three Bilibili findings that remain OPEN.

These cases document what evidence is still required. They intentionally cannot be
closed by offline replay, changing a status field, or adding a green label.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "docs/evals/bilibili-open-items-eval-set.jsonl"
SECRET_RE = re.compile(r"(?:sk-[A-Za-z0-9]{12,}|AIza[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{16,})")
EXPECTED_IDS = ["bbr-open-002", "bbr-open-022", "bbr-open-044"]


def load() -> list[dict]:
    return [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_open_items_are_explicit_and_cannot_close_offline() -> None:
    rows = load()
    assert [row["id"] for row in rows] == EXPECTED_IDS
    assert [row["status"] for row in rows] == ["OPEN"] * 3
    assert all(row["offlineCanCloseProduction"] is False for row in rows)
    assert len({row["finding"] for row in rows}) == 3


def test_each_open_item_requires_fresh_production_evidence_and_artifacts() -> None:
    for row in load():
        required = row["input"]["requiredProductionEvidence"]
        assert len(required) >= 7
        assert row["expectedOutput"]["closure"] == "only_production_evidence_can_close"
        assert len(row["expectedOutput"]["requiredArtifacts"]) >= 2
        negatives = row["negativeCases"]
        assert len(negatives) >= 4
        assert all(case["expected"] == "fail" for case in negatives)
        assert len({case["name"] for case in negatives}) == len(negatives)


def test_open_item_contracts_preserve_distinct_missing_evidence_boundaries() -> None:
    rows = {row["id"]: row for row in load()}
    assert "sample_size_8" in rows["bbr-open-002"]["input"]["requiredProductionEvidence"]
    assert "same_run_id_after_reauth" in rows["bbr-open-022"]["input"]["requiredProductionEvidence"]
    assert "exact_value_comparison" in rows["bbr-open-044"]["input"]["requiredProductionEvidence"]
    assert "offline_replay_marked_production" in {case["name"] for case in rows["bbr-open-002"]["negativeCases"]}
    assert "cause_left_unknown" in {case["name"] for case in rows["bbr-open-022"]["negativeCases"]}
    assert "claim_exact_restore_without_preincident_snapshot" in {case["name"] for case in rows["bbr-open-044"]["negativeCases"]}


def test_eval_catalog_contains_no_secrets_or_live_credentials() -> None:
    text = DATASET.read_text(encoding="utf-8")
    assert not SECRET_RE.search(text)
    assert "?token=" not in text
    assert "?signature=" not in text
    assert "accounts.google.com" not in text


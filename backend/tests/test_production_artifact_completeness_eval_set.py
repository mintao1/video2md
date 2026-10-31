"""Offline regressions for the completed-without-readable-artifact production gap."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "docs/evals/production-artifact-completeness-eval-set.jsonl"


def rows() -> list[dict]:
    return [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]


def entry_readable(entry: dict) -> bool:
    if entry.get("skipped"):
        return bool(entry.get("from_run"))
    if not entry.get("note") or not entry.get("note_exists"):
        return False
    if entry.get("note_bytes", 1) <= 0:
        return False
    if entry.get("transcript") is not None and not entry.get("transcript_exists"):
        return False
    return True


def artifact_contract(record: dict) -> bool:
    status = record.get("run_status")
    entries = record.get("entries") or []
    if status in {"queued", "running", "partial"}:
        return True
    if status != "completed" or not entries:
        return False
    if record.get("errors"):
        return False
    return all(entry_readable(entry) for entry in entries)


def test_eval_set_has_twelve_unique_contract_cases() -> None:
    data = rows()
    assert len(data) == 12
    assert len({row["id"] for row in data}) == len(data)
    assert all(row["input"] and row["expectedOutput"] and row["negativeCases"] for row in data)
    assert all(row["metadata"]["offline_can_close_production"] is False for row in data)


def test_completed_without_readable_note_is_rejected() -> None:
    for row in rows():
        record = row["input"]
        expected = row["expectedOutput"]["decision"] == "pass"
        assert artifact_contract(record) is expected, row["id"]


def test_positive_and_negative_contracts_are_explicit() -> None:
    for row in rows():
        assert len(row["negativeCases"]) >= 2, row["id"]
        assert all(case["expected"] == "fail" for case in row["negativeCases"]), row["id"]
        assert len({case["name"] for case in row["negativeCases"]}) == len(row["negativeCases"])
        assert row["metadata"]["regressions"]
        assert row["metadata"]["productionEvidenceRequired"]


def test_gap_cases_fail_closed_when_note_is_removed_or_empty() -> None:
    positive = next(row["input"] for row in rows() if row["id"] == "rac-011")
    assert artifact_contract(positive)
    missing = json.loads(json.dumps(positive))
    missing["entries"][0]["note"] = None
    missing["entries"][0]["note_exists"] = False
    assert not artifact_contract(missing)
    empty = json.loads(json.dumps(positive))
    empty["entries"][0]["note_bytes"] = 0
    assert not artifact_contract(empty)


def test_health_and_usage_cannot_mask_business_artifact_gap() -> None:
    for case_id in ("rac-008", "rac-009", "rac-012"):
        row = next(item for item in rows() if item["id"] == case_id)
        assert not artifact_contract(row["input"])


def test_eval_rows_contain_no_credentials_or_signed_urls() -> None:
    text = DATASET.read_text(encoding="utf-8")
    assert "?token=" not in text
    assert "?signature=" not in text
    assert "Bearer " not in text
    assert "sk-" not in text

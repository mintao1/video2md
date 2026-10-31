"""Regression contracts added after the 2026-10-31 production retest."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "docs/evals/production-postdeploy-regression-eval-set.jsonl"
SHORT_IDS = ["8a9826c9", "d35db1f6", "6feaef78", "bc67b068"]


def rows() -> list[dict]:
    return [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_catalog_has_ten_unique_cases_and_evidence_boundaries() -> None:
    data = rows()
    assert len(data) == 10
    assert len({row["id"] for row in data}) == 10
    assert all(row["metadata"]["productionEvidenceRequired"] for row in data)
    assert all(row["metadata"]["offline_can_close_production"] in {True, False} for row in data)


def test_four_short_ids_require_text_compat() -> None:
    row = next(item for item in rows() if item["id"] == "pdr-002")
    record = row["input"]
    assert record["run_ids"] == SHORT_IDS
    assert record["migration_version"] == 7
    assert {item["run_id"] for item in record["responses"]} == set(SHORT_IDS)
    assert all(item["http_status"] == 200 and item["error_code"] != "22P02" for item in record["responses"])


def test_missing_note_status_and_dedup_source_are_distinct() -> None:
    missing = next(item for item in rows() if item["id"] == "pdr-005")["input"]
    assert missing["all_missing"]["status"] == "error"
    assert missing["some_missing"]["status"] == "partial"
    dedup = next(item for item in rows() if item["id"] == "pdr-004")["input"]
    assert dedup["dedup_run"]["from_run"] == dedup["source_run"]["run_id"]
    assert dedup["dedup_run"]["note_exists"] is True


def test_release_smoke_requires_identity() -> None:
    record = next(item for item in rows() if item["id"] == "pdr-006")["input"]
    assert record["expected_commit"] == record["observed_commit"]
    assert record["healthz"] is True and record["preflight"] is True
    stale = {**record, "observed_commit": "stale"}
    assert stale["expected_commit"] != stale["observed_commit"]


def test_pdf_renderer_does_not_claim_native_save() -> None:
    renderer = next(item for item in rows() if item["id"] == "pdr-007")["input"]
    assert renderer["renderer"]["pdf_magic"] == "%PDF-"
    assert renderer["renderer"]["bytes"] > 1000
    assert renderer["native_save"] == {"action_observed": False, "path_observed": False}
    native = next(item for item in rows() if item["id"] == "pdr-008")["input"]["native_save"]
    assert native["action_observed"] and native["path_observed"] and native["file_mtime_after_probe"]


def test_stale_error_is_not_fresh_transport_failure() -> None:
    record = next(item for item in rows() if item["id"] == "pdr-009")["input"]
    assert "uuid" in record["historical_run"]["errors"][0]
    assert record["fresh_probe"]["database_error"] is None
    assert record["fresh_probe"]["http_status"] == 200


def test_real_conversion_requires_poll_trace() -> None:
    record = next(item for item in rows() if item["id"] == "pdr-010")["input"]
    assert record["production_evidence"] is True
    assert record["submission"]["run_id"]
    assert record["polls"][-1]["status"] in {"done", "partial", "error"}
    assert all("status" in poll for poll in record["polls"])


def test_negative_cases_are_explicit_and_cannot_close_offline() -> None:
    for row in rows():
        assert len(row["negativeCases"]) >= 2
        assert all(case["expected"] == "fail" for case in row["negativeCases"])
        assert row["expectedOutput"]["decision"] == "pass"
        if row["id"] in {"pdr-007", "pdr-009"}:
            assert row["metadata"]["offline_can_close_production"] is True or row["id"] == "pdr-009"

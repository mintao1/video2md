"""Date-stamped acceptance matrix for the 2026-10-31 production retest.

This is an offline evidence contract. It records the review snapshot and keeps
unresolved production items fail-closed; it never treats fixtures as live proof.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "docs/evals/production-acceptance-matrix-2026-10-31.jsonl"
EXPECTED_IDS = [f"pam-{i:03d}" for i in range(1, 12)]


def rows() -> list[dict]:
    return [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_matrix_is_complete_and_ordered() -> None:
    data = rows()
    assert [row["id"] for row in data] == EXPECTED_IDS
    assert len({row["title"] for row in data}) == len(data)
    assert all(row["scope"] and row["probe"] for row in data)
    assert all(row["requiredEvidence"] and row["failCategories"] for row in data)
    assert all(row["offlineCanCloseProduction"] is False for row in data)


def test_review_snapshot_preserves_known_passes_and_open_items() -> None:
    data = {row["id"]: row for row in rows()}
    assert data["pam-001"]["currentStatus"] == "fail"
    assert data["pam-001"]["decision"] == "keep_open"
    for case_id in ("pam-002", "pam-003", "pam-004", "pam-005", "pam-006", "pam-007"):
        assert data[case_id]["currentStatus"] == "pass"
        assert data[case_id]["decision"] == "regression_pass"
    assert data["pam-008"]["currentStatus"] == "open"
    assert data["pam-008"]["observation"]["native_save_file_observed"] is False
    for case_id in ("pam-009", "pam-010", "pam-011"):
        assert data[case_id]["currentStatus"] == "open"
        assert data[case_id]["decision"] == "keep_open"


def test_new_task_and_pdf_cannot_be_closed_from_ui_only() -> None:
    data = {row["id"]: row for row in rows()}
    new_task = data["pam-001"]
    assert new_task["observation"]["ui_status"] == "completed"
    assert new_task["observation"]["readable_textbook"] is False
    assert "completed_without_note" in new_task["failCategories"]
    pdf = data["pam-008"]
    assert pdf["observation"]["preview_visible"] is True
    assert pdf["observation"]["native_save_file_observed"] is False
    assert "preview_only" in pdf["failCategories"]


def test_bilibili_items_require_live_evidence() -> None:
    data = {row["id"]: row for row in rows()}
    for case_id, required in {
        "pam-009": "sample_size_8",
        "pam-010": "same_run_id_after_reauth",
        "pam-011": "exact_value_comparison",
    }.items():
        row = data[case_id]
        assert row["currentStatus"] == "open"
        assert required in row["requiredEvidence"]
        assert row["offlineCanCloseProduction"] is False


def test_dataset_contains_no_credentials_or_signed_urls() -> None:
    text = DATASET.read_text(encoding="utf-8")
    for secret in ("Bearer ", "?token=", "?signature=", "sk-"):
        assert secret not in text

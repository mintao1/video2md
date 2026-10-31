"""Offline contract gates for export, bundle delivery and cleanup evidence."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "docs/evals/export-delivery-regression-eval-set.jsonl"
CASES = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_export_delivery_catalog_is_complete_and_fail_closed() -> None:
    assert [row["id"] for row in CASES] == [f"edr-{i:03d}" for i in range(1, 7)]
    assert len({row["title"] for row in CASES}) == len(CASES)
    for row in CASES:
        assert row["scope"] and row["probe"]
        assert row["pass"] and row["fail_categories"]
        assert row["evidence_fields"] and row["regressions"]
        assert row["offline_can_close_production"] in {True, False}
        assert all(isinstance(item, str) and item for item in row["fail_categories"])


def test_markdown_and_bundle_require_observable_nonempty_files() -> None:
    markdown = next(row for row in CASES if row["id"] == "edr-001")
    bundle = next(row for row in CASES if row["id"] == "edr-002")
    assert markdown["pass"]["download_started"] and markdown["pass"]["file_bytes_gt"] == 0
    assert bundle["pass"]["download_started"] and bundle["pass"]["zip_bytes_gt"] == 0
    assert "content_sha256" in markdown["evidence_fields"]
    assert "entries_sha256" in bundle["evidence_fields"]


def test_ack_requires_physical_burn_and_repeat_download_410() -> None:
    row = next(row for row in CASES if row["id"] == "edr-003")
    expected = row["pass"]
    assert expected == {
        "download_http": 200,
        "ack_http": 200,
        "artifact_directory_removed": True,
        "second_bundle_http": 410,
        "second_ack_http": 410,
    }
    assert "artifact_retained" in row["fail_categories"]
    assert "repeat_download_allowed" in row["fail_categories"]


def test_pdf_requires_preview_and_independent_save_evidence() -> None:
    preview = next(row for row in CASES if row["id"] == "edr-004")
    saved = next(row for row in CASES if row["id"] == "edr-005")
    assert preview["pass"]["page_count_gt"] == 0
    assert saved["pass"]["file_bytes_gt"] == 0
    assert saved["pass"]["file_mtime_after_probe"] is True
    assert saved["offline_can_close_production"] is False


def test_failed_download_never_allows_ack() -> None:
    row = next(row for row in CASES if row["id"] == "edr-006")
    assert row["pass"] == {
        "download_http_not_2xx": True,
        "ack_not_sent": True,
        "artifact_remains_recoverable": True,
    }
    assert "ack_on_failed_download" in row["fail_categories"]


def test_catalog_contains_no_credentials_or_signed_urls() -> None:
    text = DATASET.read_text(encoding="utf-8")
    assert "Bearer " not in text
    assert "?token=" not in text
    assert "?signature=" not in text

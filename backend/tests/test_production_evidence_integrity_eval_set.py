"""Evidence-quality contracts for the production browser retest.

These cases separate a visible UI state from a physical, fresh artifact. They
are offline guards only; the cases marked production-required need a real run
and redacted browser evidence before acceptance.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "docs/evals/production-evidence-integrity-eval-set.jsonl"
CASES = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]
EXPECTED = [f"pei-{i:03d}" for i in range(1, 9)]


def test_catalog_is_complete_and_fail_closed() -> None:
    assert [row["id"] for row in CASES] == EXPECTED
    assert len({row["title"] for row in CASES}) == len(CASES)
    assert len({row["probe"] for row in CASES}) == len(CASES)
    for row in CASES:
        assert row["scope"] and row["pass"] and row["requiredEvidence"]
        assert row["failCategories"]
        assert row["offlineCanCloseProduction"] in {True, False}
        assert all(isinstance(item, str) and item for item in row["failCategories"])


def test_reader_media_requires_physical_loading_and_playback_signals() -> None:
    image = next(row for row in CASES if row["id"] == "pei-001")
    video = next(row for row in CASES if row["id"] == "pei-002")
    assert image["pass"]["every_complete"] is True
    assert image["pass"]["natural_width_gt"] == 0
    assert video["pass"]["every_ready_state_gte"] == 4
    assert video["pass"]["current_time_gt"] == 0
    assert video["pass"]["paused"] is False
    assert video["pass"]["error"] is None
    assert image["offlineCanCloseProduction"] is False
    assert video["offlineCanCloseProduction"] is False


def test_exports_require_fresh_hashable_files_and_ack_burn() -> None:
    markdown = next(row for row in CASES if row["id"] == "pei-003")
    bundle = next(row for row in CASES if row["id"] == "pei-004")
    ack = next(row for row in CASES if row["id"] == "pei-005")
    assert markdown["pass"]["bytes_gt"] == 0 and markdown["pass"]["mtime_after_probe"] is True
    assert bundle["pass"]["zip_bytes_gt"] == 0 and bundle["pass"]["entries_sha256"] is True
    assert ack["pass"] == {
        "ack_http": 200,
        "artifact_directory_removed": True,
        "second_bundle_http": 410,
        "second_ack_http": 410,
    }


def test_pdf_keeps_renderer_and_native_save_as_two_acceptance_boundaries() -> None:
    preview = next(row for row in CASES if row["id"] == "pei-006")
    native = next(row for row in CASES if row["id"] == "pei-007")
    assert preview["pass"]["page_count_gt"] == 0
    assert preview["pass"]["local_mathjax"] is True
    assert native["pass"]["native_save_file_bytes_gt"] == 0
    assert native["pass"]["native_save_mtime_after_probe"] is True
    assert "page_pdf_only" in native["failCategories"]
    assert native["offlineCanCloseProduction"] is False


def test_stale_file_case_is_the_only_offline_closable_case() -> None:
    stale = next(row for row in CASES if row["id"] == "pei-008")
    assert stale["offlineCanCloseProduction"] is True
    assert stale["pass"]["old_file_mtime_before_probe"] is True
    assert stale["pass"]["current_file_mtime_after_probe"] is True
    assert "stale_file_accepted" in stale["failCategories"]

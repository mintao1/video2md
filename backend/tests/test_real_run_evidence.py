from __future__ import annotations

import hashlib
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("real_run_evidence", ROOT / "scripts/check_real_run_evidence.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _run(source: str, index: int) -> dict:
    return {
        "run_id": f"run-{index:03d}",
        "source_kind": source,
        "source_ref_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "route": {
            "resolve_provider": source,
            "asr_provider": "colab-cli",
            "ocr_provider": "colab-cli",
            "clips_provider": "colab-cli",
            "llm_model": "model-redacted",
            "fallback_used": False,
        },
        "stages": [{"name": name, "status": "completed", "duration_ms": 1000 + i}
                   for i, name in enumerate(MODULE.STAGES)],
        "quality": {
            "faithfulness": 0.91,
            "coverage": 0.87,
            "structure": 0.9,
            "non_hallucination": 1,
            "media_reference_valid": 1,
        },
        "artifact": {"transcript_chars": 5000, "note_chars": 1200,
                      "timestamp_count": 8, "image_refs": 4, "video_refs": 2},
        "usage": {"llm_in_tokens": 1000, "llm_out_tokens": 400,
                  "gpu_seconds": 30.0, "cost_usd": 0.02},
    }


def _evidence() -> dict:
    sources = ["bilibili", "youtube", "douyin", "xiaohongshu", "local"]
    return {
        "kind": "production-real-run",
        "redacted": True,
        "live_observed": True,
        "offline_can_close_production": False,
        "capture": {"captured_at": datetime.now(timezone.utc).isoformat()},
        "release": {"version": "0.2.0", "commit": "a" * 40, "image": "video2md:fixture"},
        "runs": [_run(source, index) for index, source in enumerate(sources, 1)],
    }


def test_complete_live_evidence_passes() -> None:
    assert MODULE.validate_evidence(_evidence()) == []


def test_offline_or_fixture_evidence_cannot_close_production() -> None:
    evidence = _evidence()
    evidence["offline_can_close_production"] = True
    evidence["live_observed"] = False
    failures = MODULE.validate_evidence(evidence)
    assert any("offline-boundary" in failure for failure in failures)
    assert any("root-live" in failure for failure in failures)


def test_missing_source_and_bad_quality_fail_closed() -> None:
    evidence = _evidence()
    evidence["runs"] = evidence["runs"][:4]
    evidence["runs"][0]["quality"]["faithfulness"] = 0.2
    failures = MODULE.validate_evidence(evidence, min_runs=4)
    assert any("quality-faithfulness" in failure for failure in failures)
    assert any("sources" in failure for failure in failures)


def test_raw_url_and_content_fields_are_rejected() -> None:
    evidence = _evidence()
    evidence["runs"][0]["source_url"] = "https://example.invalid/video"
    evidence["runs"][0]["prompt"] = "fixture prompt"
    failures = MODULE.validate_evidence(evidence)
    assert any("secrets" in failure for failure in failures)
    assert any("source_url" in failure for failure in failures)
    assert any("prompt" in failure for failure in failures)

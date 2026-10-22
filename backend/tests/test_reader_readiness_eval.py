"""Functional delivery does not silently close a slow/unknown readiness finding."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "reader_gate", Path(__file__).resolve().parents[2] / "scripts/check_reader_readiness.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def evidence():
    return {
        "kind": "synthetic",
        "run_id": "fixture",
        "opened_at": "2026-10-22T00:00:00+00:00",
        "verified_ready_at": "2026-10-22T00:00:12+00:00",
        "measurement_complete": True,
        "expected_videos": ["clip.mp4"],
        "videos": [{"label": "clip.mp4", "readyState": 4, "error": 0}],
        "pending": [],
    }


def test_positive_and_synthetic_cannot_close_production():
    assert gate.evaluate(evidence(), allow_synthetic=True)
    assert not gate.evaluate(evidence())


@pytest.mark.parametrize(
    "defect",
    [
        "18-minutes",
        "missing-sample",
        "still-loading",
        "decode-error",
        "wrong-video",
        "duplicates",
        "no-timezone",
        "negative-time",
        "pending",
    ],
)
def test_invalid_readiness_cannot_pass(defect):
    e = evidence()
    if defect == "18-minutes":
        e["verified_ready_at"] = "2026-10-22T00:18:00+00:00"
    elif defect == "missing-sample":
        e["measurement_complete"] = False
    elif defect == "still-loading":
        e["videos"][0]["readyState"] = 0
    elif defect == "decode-error":
        e["videos"][0]["error"] = 3
    elif defect == "wrong-video":
        e["videos"][0]["label"] = "other.mp4"
    elif defect == "duplicates":
        e["videos"].append(e["videos"][0])
    elif defect == "no-timezone":
        e["opened_at"] = "2026-10-22T00:00:00"
    elif defect == "negative-time":
        e["opened_at"] = "2026-10-22T01:00:00+00:00"
    else:
        e["pending"] = ["clip.mp4"]
    assert not gate.evaluate(e, allow_synthetic=True)

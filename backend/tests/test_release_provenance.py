from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("release_provenance", ROOT / "scripts/check_release_provenance.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _evidence(**overrides):
    source = {
        "local_sha": "a" * 40,
        "target_ref": "origin/main",
        "target_sha": "a" * 40,
        "dirty": False,
        "package_version": "0.2.0",
    }
    source.update(overrides.pop("source", {}))
    return {
        "source": source,
        "expected_sha": overrides.pop("expected_sha", None),
        "image": overrides.pop("image", {"package_version_match": True}),
        "healthz": overrides.pop("healthz", {"requested": False}),
        **overrides,
    }


def test_strict_provenance_accepts_matching_source_image_and_health() -> None:
    evidence = _evidence(
        expected_sha="a" * 40,
        image={"package_version_match": True},
        healthz={"requested": True, "ok": True, "version": "0.2.0"},
    )
    assert MODULE.evaluate(evidence, strict=True) == []


def test_strict_provenance_reports_dirty_drift_and_version_mismatch() -> None:
    evidence = _evidence(
        source={"dirty": True, "target_sha": "b" * 40},
        expected_sha="c" * 40,
        image={"package_version_match": False},
        healthz={"requested": True, "ok": True, "version": "0.1.0"},
    )
    failures = MODULE.evaluate(evidence, strict=True)
    assert {item.split(":", 1)[0] for item in failures} >= {
        "source-target", "source", "expected-sha", "image", "healthz",
    }

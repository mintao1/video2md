from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("release_consistency", ROOT / "scripts/check_release_consistency.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_repository_release_metadata_and_images_are_consistent() -> None:
    assert MODULE.check(ROOT) == []


def test_gate_rejects_mixed_kubernetes_images(tmp_path: Path) -> None:
    for relative in (
        "backend/pyproject.toml",
        "Dockerfile",
        "deploy/docker-compose.prod.yml",
        "deploy/k8s/base/deployment-api.yaml",
    ):
        source = ROOT / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    deployment = tmp_path / "deploy/k8s/base/deployment-api.yaml"
    text = deployment.read_text(encoding="utf-8")
    deployment.write_text(text.replace("video2md:0.2.0-colab-tenant-20261026d", "video2md:fixture-a", 1), encoding="utf-8")
    failures = MODULE.check(tmp_path)
    assert any("one image tag" in failure for failure in failures)

#!/usr/bin/env python3
"""Check that release metadata and deployment images describe one product.

This is deliberately a source-only gate. It does not contact GitHub or a
cluster; live provenance is checked by the deployment control-plane probe.
The gate catches the easy-to-miss failure where init, migration, and API
containers are built from different tags.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

IMAGE_RE = re.compile(r"\bimage:\s*(video2md:[^\s#]+)")
COMPOSE_IMAGE_RE = re.compile(r"image:\s*video2md:\$\{VIDEO2MD_VERSION:-([^}]+)\}")
COMPOSE_BUILD_RE = re.compile(r"VERSION:\s*\$\{VIDEO2MD_VERSION:-([^}]+)\}")
PYPROJECT_NAME_RE = re.compile(r'^name\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
PYPROJECT_VERSION_RE = re.compile(r'^version\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


def check(repo: Path) -> list[str]:
    failures: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    pyproject = (repo / "backend/pyproject.toml").read_text(encoding="utf-8")
    name = PYPROJECT_NAME_RE.search(pyproject)
    version = PYPROJECT_VERSION_RE.search(pyproject)
    require(name is not None and name.group(1) == "video2md", "backend package name must be video2md")
    require(version is not None and bool(version.group(1)), "backend package version is missing")

    dockerfile = (repo / "Dockerfile").read_text(encoding="utf-8")
    require('org.opencontainers.image.title="video2md"' in dockerfile,
            "Docker image title must be video2md")
    require('https://github.com/mintao1/video2md' in dockerfile,
            "Docker image source must point to the video2md repository")

    compose = (repo / "deploy/docker-compose.prod.yml").read_text(encoding="utf-8")
    compose_image = COMPOSE_IMAGE_RE.search(compose)
    compose_build = COMPOSE_BUILD_RE.search(compose)
    require(compose_image is not None and compose_build is not None,
            "production compose must derive image and build VERSION from VIDEO2MD_VERSION")
    if compose_image and compose_build:
        require(compose_image.group(1) == compose_build.group(1),
                "compose image tag and Docker VERSION default differ")
    if version:
        require(compose_image is not None and compose_image.group(1).startswith(version.group(1)),
                "compose default image must carry the backend package version")

    deployment = (repo / "deploy/k8s/base/deployment-api.yaml").read_text(encoding="utf-8")
    images = IMAGE_RE.findall(deployment)
    require(bool(images), "Kubernetes deployment has no video2md image")
    if images:
        require(len(set(images)) == 1,
                "all Kubernetes init/migrate/API containers must use one image tag: "
                + ", ".join(dict.fromkeys(images)))
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    failures = check(args.repo)
    if failures:
        print("FAIL")
        print("\n".join(f"- {item}" for item in failures))
        return 1
    print("PASS: release metadata and deployment images are consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

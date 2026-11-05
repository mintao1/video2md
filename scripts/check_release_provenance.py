#!/usr/bin/env python3
"""Collect and optionally enforce release provenance without mutating state.

The command joins four facts that are otherwise easy to compare by hand:
the checked-out source SHA, an optional target ref, the image tag selected for
deployment, and the version returned by ``/healthz``.  It never checks out a
ref, pushes, applies a manifest, or prints response bodies.

Normal mode is diagnostic and exits zero while reporting mismatches.  Use
``--strict`` in a clean release checkout or deployment gate to fail on dirty
source, ref drift, image/version drift, or an unhealthy health endpoint.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

VERSION_RE = re.compile(r'^version\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
IMAGE_RE = re.compile(r"^(?P<name>[^:]+):(?P<tag>.+)$")


def _git(repo: Path, *args: str) -> tuple[str | None, str | None]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        return None, detail.strip().splitlines()[-1][:240] if detail else type(exc).__name__
    return result.stdout.strip(), None


def _safe_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("health URL must be an absolute http(s) URL")
    host = parsed.hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))


def _healthz(url: str, timeout: float) -> dict[str, Any]:
    endpoint = _safe_url(url)
    if not endpoint.endswith("/healthz"):
        endpoint += "/healthz"
    request = urllib.request.Request(endpoint, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            payload = json.loads(response.read().decode("utf-8"))
        return {
            "endpoint": endpoint,
            "http_status": status,
            "version": payload.get("version") if isinstance(payload, dict) else None,
            "ok": status == 200 and isinstance(payload, dict) and payload.get("ok") is True,
        }
    except urllib.error.HTTPError as exc:
        return {"endpoint": endpoint, "http_status": int(exc.code), "ok": False,
                "error": "HTTPError"}
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {"endpoint": endpoint, "http_status": 0, "ok": False,
                "error": type(exc).__name__}


def _package_version(repo: Path) -> str | None:
    try:
        text = (repo / "backend/pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    match = VERSION_RE.search(text)
    return match.group(1) if match else None


def collect(
    repo: Path,
    *,
    target_ref: str | None = None,
    expected_sha: str | None = None,
    expected_image: str | None = None,
    health_url: str | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    local_sha, local_error = _git(repo, "rev-parse", "HEAD")
    status, _status_error = _git(repo, "status", "--porcelain", "--untracked-files=normal")
    target_sha = target_error = None
    if target_ref:
        target_sha, target_error = _git(repo, "rev-parse", target_ref)
    ahead = behind = None
    if target_ref and target_sha and local_sha:
        ahead_text, _ = _git(repo, "rev-list", "--count", f"{target_ref}..HEAD")
        behind_text, _ = _git(repo, "rev-list", "--count", f"HEAD..{target_ref}")
        try:
            ahead, behind = int(ahead_text or "0"), int(behind_text or "0")
        except ValueError:
            ahead = behind = None

    package_version = _package_version(repo)
    image = None
    if expected_image:
        image_match = IMAGE_RE.match(expected_image)
        image = {
            "value": expected_image,
            "name": image_match.group("name") if image_match else None,
            "tag": image_match.group("tag") if image_match else None,
            "package_version_match": bool(
                image_match and package_version and image_match.group("name") == "video2md"
                and image_match.group("tag").startswith(package_version)
            ),
        }

    health = _healthz(health_url, timeout) if health_url else {"requested": False}
    return {
        "kind": "release_provenance",
        "repo": str(repo.resolve()),
        "source": {
            "local_sha": local_sha,
            "local_error": local_error,
            "dirty": bool(status),
            "target_ref": target_ref,
            "target_sha": target_sha,
            "target_error": target_error,
            "ahead": ahead,
            "behind": behind,
            "package_version": package_version,
        },
        "image": image,
        "healthz": health,
        "expected_sha": expected_sha,
    }


def evaluate(evidence: dict[str, Any], *, strict: bool = False) -> list[str]:
    source = evidence.get("source") if isinstance(evidence.get("source"), dict) else {}
    image = evidence.get("image") if isinstance(evidence.get("image"), dict) else None
    health = evidence.get("healthz") if isinstance(evidence.get("healthz"), dict) else {}
    failures: list[str] = []

    if source.get("local_error") or not source.get("local_sha"):
        failures.append("source: unable to resolve local HEAD")
    if source.get("target_ref") and source.get("target_error"):
        failures.append("target: unable to resolve target ref")
    if source.get("target_sha") and source.get("local_sha") != source.get("target_sha"):
        failures.append("source-target: local SHA differs from target ref")
    if source.get("dirty"):
        failures.append("source: worktree is dirty")
    expected_sha = evidence.get("expected_sha")
    if expected_sha and source.get("local_sha") != expected_sha:
        failures.append("expected-sha: local SHA differs from expected SHA")
    if image and not image.get("package_version_match"):
        failures.append("image: image tag does not carry the package version")
    if health.get("requested") is not False and health.get("ok") is not True:
        failures.append("healthz: endpoint is not healthy")
    if health.get("requested") is not False and source.get("package_version") \
            and health.get("version") != source.get("package_version"):
        failures.append("healthz: version differs from package version")
    return failures if strict else []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--target-ref", help="local ref to compare with, e.g. origin/main")
    parser.add_argument("--expected-sha", help="expected checked-out source SHA")
    parser.add_argument("--expected-image", help="deployed image, e.g. video2md:0.2.0-abc")
    parser.add_argument("--health-url", help="production base URL or direct /healthz URL")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--strict", action="store_true", help="fail on provenance mismatches")
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be > 0")
    try:
        evidence = collect(args.repo, target_ref=args.target_ref, expected_sha=args.expected_sha,
                           expected_image=args.expected_image, health_url=args.health_url,
                           timeout=args.timeout)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    failures = evaluate(evidence, strict=args.strict)
    if args.json:
        print(json.dumps({**evidence, "failures": failures}, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
        print("FAIL" if failures else "PASS")
        if failures:
            print("\n".join(f"- {item}" for item in failures))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

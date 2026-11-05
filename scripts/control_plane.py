#!/usr/bin/env python3
"""Authenticated production control-plane probe.

The smoke scripts start a local API and exercise a synthetic video.  This
command is for the other half of the acceptance gap: run the same checks
against a deployed API with a real bearer token, while keeping credentials and
source URLs out of the evidence it prints.

Examples::

    # Anonymous surface + queue health (no task submission)
    uv run --project backend python scripts/control_plane.py \
      --base-url https://video2md.example.com

    # Poll an already-submitted task and download its bundle.  ACK is opt-in.
    V2MD_ACCESS_TOKEN=... uv run --project backend python scripts/control_plane.py \
      --base-url https://video2md.example.com --run-id RUN_ID \
      --wait 900 --download /tmp/RUN_ID.zip

The probe never submits a task unless ``--submit-url`` is explicitly passed.
It never deletes an artifact unless ``--ack`` is explicitly passed together
with ``--download``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

TERMINAL = {"done", "partial", "error"}


def _status(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if isinstance(status, str):
        return status
    task = payload.get("task")
    return task.get("status") if isinstance(task, dict) and isinstance(task.get("status"), str) else None


def _safe_origin(value: str) -> str:
    """Return a display-safe origin without credentials, query, or fragment."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("--base-url must be an absolute http(s) URL")
    # urlunsplit intentionally drops query/fragment; userinfo is removed from
    # netloc so a mistakenly pasted URL cannot leak a password in evidence.
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _error_count(payload: dict[str, Any]) -> int:
    errors = payload.get("errors")
    return len(errors) if isinstance(errors, list) else (1 if errors else 0)


@dataclass
class ProbeResult:
    status: int
    body: Any = None
    headers: dict[str, str] | None = None


class ControlPlane:
    """Small HTTP client whose methods return redaction-safe probe records."""

    def __init__(self, base_url: str, token: str | None = None, *, timeout: float = 20.0,
                 client: httpx.Client | None = None) -> None:
        self.origin = _safe_origin(base_url)
        self.token = token.strip() if token and token.strip() else None
        self.client = client or httpx.Client(base_url=self.origin, timeout=timeout,
                                             follow_redirects=False)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def request(self, method: str, path: str, **kwargs: Any) -> ProbeResult:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers = {**self._headers(), **headers}
        try:
            response = self.client.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            return ProbeResult(status=0, body={"error": type(exc).__name__})
        try:
            body: Any = response.json()
        except ValueError:
            body = None
        return ProbeResult(response.status_code, body, dict(response.headers))

    def public_surface(self) -> dict[str, Any]:
        expected = {"healthz": 200, "terms": 200, "privacy": 200}
        result: dict[str, Any] = {}
        for name, expected_status in expected.items():
            probe = self.request("GET", f"/{name}")
            result[name] = {"http_status": probe.status, "ok": probe.status == expected_status}
        me = self.request("GET", "/me")
        result["unauthenticated_me"] = {"http_status": me.status, "ok": me.status == 401}
        # /queue is intentionally hidden by the public reverse proxy but is
        # available on the internal API address used by the Colab watchdog.
        queue = self.request("GET", "/queue")
        result["queue"] = {"http_status": queue.status,
                           "ok": queue.status in (200, 404),
                           "visible": queue.status == 200}
        return result

    def authenticated_surface(self) -> dict[str, Any]:
        if not self.token:
            return {"provided": False}
        result: dict[str, Any] = {"provided": True}
        for name in ("me", "runs", "quota", "auth/consent"):
            probe = self.request("GET", f"/{name}")
            body = probe.body if isinstance(probe.body, dict) else {}
            terms_current = body.get("terms_current") if name == "auth/consent" else None
            result[name] = {
                "http_status": probe.status,
                "ok": probe.status == 200 and (name != "auth/consent" or terms_current is True),
                **({"terms_current": terms_current} if name == "auth/consent" else {}),
            }
        return result

    def queue(self) -> dict[str, Any]:
        probe = self.request("GET", "/queue")
        body = probe.body if isinstance(probe.body, dict) else {}
        return {
            "http_status": probe.status,
            "visible": probe.status == 200,
            "queue_len": body.get("queue_len") if probe.status == 200 else None,
            "running": body.get("running") if probe.status == 200 else None,
            "limits": body.get("limits") if probe.status == 200 else None,
        }

    def accept_terms(self, version: str = "2026-10-21") -> dict[str, Any]:
        """Persist the public agreement version for the authenticated tenant.

        This is intentionally an explicit mutating action; the CLI never calls
        it unless ``--accept-terms`` is supplied.
        """
        probe = self.request("POST", "/auth/consent", json={"terms_version": version})
        body = probe.body if isinstance(probe.body, dict) else {}
        return {
            "http_status": probe.status,
            "accepted": probe.status == 200 and body.get("terms_current") is True,
            "terms_current": body.get("terms_current"),
        }

    def submit(self, source_url: str) -> dict[str, Any]:
        if not self.token:
            raise RuntimeError("--submit-url requires V2MD_ACCESS_TOKEN or --token")
        probe = self.request("POST", "/runs", json={"url": source_url})
        body = probe.body if isinstance(probe.body, dict) else {}
        return {"http_status": probe.status, "run_id": body.get("run_id"),
                "status": _status(body), "accepted": probe.status in (200, 201, 202)}

    def get_run(self, run_id: str) -> ProbeResult:
        if not self.token:
            raise RuntimeError("--run-id requires V2MD_ACCESS_TOKEN or --token")
        return self.request("GET", f"/runs/{run_id}")

    def retry(self, run_id: str) -> dict[str, Any]:
        if not self.token:
            raise RuntimeError("--retry requires V2MD_ACCESS_TOKEN or --token")
        probe = self.request("POST", f"/runs/{run_id}/resume")
        body = probe.body if isinstance(probe.body, dict) else {}
        return {"http_status": probe.status, "status": _status(body),
                "accepted": probe.status in (200, 201, 202)}

    def wait_run(self, run_id: str, timeout_s: float, poll_s: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        last: ProbeResult | None = None
        while True:
            last = self.get_run(run_id)
            body = last.body if isinstance(last.body, dict) else {}
            status = _status(body)
            if status in TERMINAL:
                return {"http_status": last.status, "status": status,
                        "terminal": True,
                        # Error text can contain a source URL or provider
                        # details.  Keep the evidence useful without copying
                        # that payload into an operator artifact.
                        "error_count": _error_count(body)}
            if last.status >= 400 or time.monotonic() >= deadline:
                return {"http_status": last.status, "status": status,
                        "terminal": False, "timed_out": time.monotonic() >= deadline,
                        "error_count": _error_count(body)}
            time.sleep(poll_s)

    def download_bundle(self, run_id: str, destination: Path) -> dict[str, Any]:
        if not self.token:
            raise RuntimeError("--download requires V2MD_ACCESS_TOKEN or --token")
        response = self.client.get(f"/runs/{run_id}/bundle", headers=self._headers())
        if response.status_code != 200:
            return {"http_status": response.status_code, "saved": False}
        data = response.content
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        names: list[str] = []
        valid_zip = False
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                names = sorted(archive.namelist())
                valid_zip = archive.testzip() is None
        except zipfile.BadZipFile:
            valid_zip = False
        return {"http_status": 200, "saved": True, "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "valid_zip": valid_zip,
                "entries": names[:100]}

    def ack(self, run_id: str) -> dict[str, Any]:
        probe = self.request("DELETE", f"/runs/{run_id}/artifacts")
        body = probe.body if isinstance(probe.body, dict) else {}
        return {"http_status": probe.status, "status": body.get("status"),
                "burned": body.get("status") == "burned"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("V2MD_API_URL", "http://127.0.0.1:8790"))
    parser.add_argument("--token", default=os.environ.get("V2MD_ACCESS_TOKEN"), help=argparse.SUPPRESS)
    parser.add_argument("--submit-url", help="submit one source URL (explicit opt-in)")
    parser.add_argument("--run-id", help="poll an existing run")
    parser.add_argument("--retry", action="store_true", help="resume --run-id when it is error/partial")
    parser.add_argument("--accept-terms", action="store_true",
                        help="record the current terms receipt (explicit mutating opt-in)")
    parser.add_argument("--terms-version", default=os.environ.get("V2MD_TERMS_VERSION", "2026-10-21"),
                        help=argparse.SUPPRESS)
    parser.add_argument("--wait", type=float, default=0.0, metavar="SECONDS", help="poll submitted/existing run")
    parser.add_argument("--poll", type=float, default=5.0, metavar="SECONDS")
    parser.add_argument("--download", type=Path, metavar="ZIP", help="save a completed bundle locally")
    parser.add_argument("--ack", action="store_true", help="burn artifacts after a successful download")
    parser.add_argument("--json", action="store_true", help="emit JSON evidence only")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.poll <= 0 or args.wait < 0:
        print("FAIL: --poll must be > 0 and --wait must be >= 0", file=sys.stderr)
        return 2
    if args.ack and not args.download:
        print("FAIL: --ack requires --download", file=sys.stderr)
        return 2
    if args.submit_url and args.run_id:
        print("FAIL: choose --submit-url or --run-id, not both", file=sys.stderr)
        return 2
    evidence: dict[str, Any] = {"kind": "live_control_probe", "captured_at": _now()}
    try:
        control = ControlPlane(args.base_url, args.token)
    except ValueError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    try:
        evidence["origin"] = control.origin
        evidence["anonymous_surface"] = control.public_surface()
        evidence["authenticated_surface"] = control.authenticated_surface()
        evidence["queue"] = control.queue()
        if args.accept_terms:
            evidence["terms_acceptance"] = control.accept_terms(args.terms_version)
            if not evidence["terms_acceptance"]["accepted"]:
                return _emit(evidence, args.json, 1)
        run_id = args.run_id
        if args.submit_url:
            submission = control.submit(args.submit_url)
            evidence["submission"] = submission
            run_id = submission.get("run_id")
            if not submission.get("accepted") or not run_id:
                return _emit(evidence, args.json, 1)
        if run_id and args.retry:
            evidence["retry"] = control.retry(run_id)
        if run_id and args.wait:
            evidence["run"] = control.wait_run(run_id, args.wait, args.poll)
        if run_id and args.download:
            evidence["download"] = control.download_bundle(run_id, args.download)
        if run_id and args.ack:
            evidence["ack"] = control.ack(run_id)
        failed = any(not item.get("ok", True) for section in evidence.values()
                     if isinstance(section, dict) for item in section.values()
                     if isinstance(item, dict) and "ok" in item)
        if evidence.get("run", {}).get("timed_out"):
            failed = True
        if args.download and not evidence.get("download", {}).get("saved"):
            failed = True
        if args.ack and not evidence.get("ack", {}).get("burned"):
            failed = True
        return _emit(evidence, args.json, 1 if failed else 0)
    finally:
        control.close()


def _emit(evidence: dict[str, Any], json_only: bool, code: int) -> int:
    if json_only:
        print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True))
        print("FAIL" if code else "PASS")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

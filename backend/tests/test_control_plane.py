from __future__ import annotations

import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("control_plane", ROOT / "scripts" / "control_plane.py")
assert SPEC and SPEC.loader
control_plane = importlib.util.module_from_spec(SPEC)
sys.modules["control_plane"] = control_plane
SPEC.loader.exec_module(control_plane)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://api.test")


def test_origin_drops_userinfo_query_and_fragment() -> None:
    assert control_plane._safe_origin("https://u:p@example.test/api?token=secret#x") == "https://example.test/api"


def test_public_surface_accepts_hidden_queue_probe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/healthz", "/terms", "/privacy"}:
            return httpx.Response(200, json={})
        if request.url.path == "/me":
            return httpx.Response(401, json={})
        if request.url.path == "/queue":
            return httpx.Response(404)
        return httpx.Response(404)

    control = control_plane.ControlPlane("https://api.test", client=_client(handler))
    try:
        result = control.public_surface()
    finally:
        control.close()
    assert result["unauthenticated_me"] == {"http_status": 401, "ok": True}
    assert result["queue"] == {"http_status": 404, "ok": True, "visible": False}


def test_download_bundle_writes_and_validates_zip(tmp_path: Path) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("note.md", "# note")
    data = payload.getvalue()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer token"
        return httpx.Response(200, content=data, headers={"content-type": "application/zip"})

    control = control_plane.ControlPlane("https://api.test", "token", client=_client(handler))
    path = tmp_path / "bundle.zip"
    try:
        result = control.download_bundle("run-1", path)
    finally:
        control.close()
    assert result["saved"] is True and result["valid_zip"] is True
    assert result["entries"] == ["note.md"]
    assert path.read_bytes() == data
    assert "token" not in json.dumps(result)


def test_authenticated_surface_requires_current_terms() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/consent":
            return httpx.Response(200, json={"terms_current": False})
        return httpx.Response(200, json={})

    control = control_plane.ControlPlane("https://api.test", "token", client=_client(handler))
    try:
        result = control.authenticated_surface()
    finally:
        control.close()
    assert result["auth/consent"] == {
        "http_status": 200,
        "ok": False,
        "terms_current": False,
    }


def test_accept_terms_is_explicit_and_redacts_receipt() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert json.loads(request.content) == {"terms_version": "2026-10-21"}
        return httpx.Response(200, json={
            "terms_version": "2026-10-21",
            "terms_current": True,
            "terms_accepted_at": "2026-11-01T00:00:00+08:00",
        })

    control = control_plane.ControlPlane("https://api.test", "token", client=_client(handler))
    try:
        result = control.accept_terms()
    finally:
        control.close()
    assert result == {"http_status": 200, "accepted": True, "terms_current": True}


def test_cli_rejects_ack_without_download() -> None:
    assert control_plane.main(["--ack"]) == 2

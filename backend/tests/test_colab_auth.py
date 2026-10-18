"""OAuth boundary contracts: permissions, PKCE, ownership, expiry, replay and private writes."""
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from video2md import colab_auth as auth
from video2md.auth import Identity, current_identity


@pytest.fixture
def web(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "_directory", lambda: tmp_path / "credentials")
    monkeypatch.setitem(auth.ENV, "V2MD_COLAB_LOCK", str(tmp_path / "session.lock"))
    monkeypatch.setitem(auth.ENV, "V2MD_AUTH", "google")
    monkeypatch.setitem(auth.ENV, "V2MD_COLAB_ADMIN_UIDS", "admin,other")
    identity = Identity("admin", "google", "google-admin")
    calls = []

    def flow(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            code_verifier="pkce-secret",
            authorization_url=lambda **kw: ("https://accounts.google.com/o/oauth2/auth?state=test", "state"),
            fetch_token=lambda **kw: calls.append(kw),
            credentials=SimpleNamespace(refresh_token="refresh-secret", to_json=lambda: '{"refresh_token":"refresh-secret"}'),
        )

    monkeypatch.setattr(auth, "_flow", flow)
    app = FastAPI()
    app.include_router(auth.router)
    app.dependency_overrides[current_identity] = lambda: identity
    with TestClient(app) as client:
        yield client, identity, calls, tmp_path / "credentials"


def test_authorization_pkce_and_one_time_private_save(web):
    client, identity, calls, directory = web
    assert client.get("/settings/colab").json() == {"can_authorize": True, "has_credentials": False}
    started = client.post("/settings/colab/start").json()
    assert started["expires_in"] == 600
    assert "verifier" not in started
    pending = auth._pending(identity.uid)
    assert pending.stat().st_mode & 0o777 == 0o600
    body = {"flow_id": started["flow_id"], "code": "code-secret"}
    assert client.post("/settings/colab/complete", json=body).status_code == 200
    assert calls[0]["autogenerate_code_verifier"] is True
    assert calls[1] == {"state": "state", "code_verifier": "pkce-secret"}
    assert calls[2] == {"code": "code-secret", "timeout": 30}
    token = directory / "token.json"
    assert token.stat().st_mode & 0o777 == 0o600
    assert not pending.exists()
    assert client.post("/settings/colab/complete", json=body).status_code == 400
    assert "refresh-secret" not in client.get("/settings/colab").text


def test_other_users_cannot_replace_service_credentials(web):
    client, identity, calls, directory = web
    identity.uid = "regular-user"
    assert client.get("/settings/colab").json()["can_authorize"] is False
    assert client.post("/settings/colab/start").status_code == 403
    assert client.post("/settings/colab/complete", json={"flow_id": "x", "code": "x"}).status_code == 403
    assert not calls and not directory.exists()


def test_flow_is_bound_to_uid_and_expires(web):
    client, identity, calls, directory = web
    started = client.post("/settings/colab/start").json()
    body = {"flow_id": started["flow_id"], "code": "code"}
    identity.uid = "other"
    assert client.post("/settings/colab/complete", json=body).status_code == 400
    identity.uid = "admin"
    pending = auth._pending(identity.uid)
    state = json.loads(pending.read_text())
    state["expires"] = 0
    pending.write_text(json.dumps(state))
    assert client.post("/settings/colab/complete", json=body).status_code == 400
    assert not (directory / "token.json").exists()


def test_failed_exchange_hides_secrets_and_preserves_previous_token(web, monkeypatch):
    client, identity, calls, directory = web
    started = client.post("/settings/colab/start").json()
    token = directory / "token.json"
    token.write_text("old-token")

    def failed(**kwargs):
        def exchange(**kwargs):
            raise RuntimeError("sensitive-code-and-token")
        return SimpleNamespace(fetch_token=exchange)

    monkeypatch.setattr(auth, "_flow", failed)
    response = client.post("/settings/colab/complete", json={"flow_id": started["flow_id"], "code": "secret"})
    assert response.status_code == 400 and "sensitive-code-and-token" not in response.text
    assert token.read_text() == "old-token"
    assert not auth._pending(identity.uid).exists()


def test_auth_disabled_allows_only_local_development(web, monkeypatch):
    client, identity, _, _ = web
    monkeypatch.setitem(auth.ENV, "V2MD_AUTH", "off")
    monkeypatch.setitem(auth.ENV, "V2MD_ENV", "development")
    assert client.post("/settings/colab/start").status_code == 403  # TestClient host is not loopback.
    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
    assert auth._allowed(request, identity)
    monkeypatch.setitem(auth.ENV, "V2MD_ENV", "production")
    assert not auth._allowed(request, identity)


def test_validation_never_echoes_submitted_code(web):
    from fastapi.exceptions import RequestValidationError

    from video2md.api import _on_validation_error

    app = FastAPI(exception_handlers={RequestValidationError: _on_validation_error})
    app.include_router(auth.router)
    app.dependency_overrides[current_identity] = lambda: web[1]
    with TestClient(app) as client:
        response = client.post("/settings/colab/complete", json={"flow_id": "flow", "code": "secret-" * 1000})
    assert response.status_code == 422
    assert "secret-" not in response.text

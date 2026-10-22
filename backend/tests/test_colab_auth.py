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
    monkeypatch.setitem(auth.ENV, "V2MD_COLAB_USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setitem(auth.ENV, "V2MD_COLAB_LOCK", str(tmp_path / "session.lock"))
    monkeypatch.setitem(auth.ENV, "V2MD_AUTH", "google")
    identity = Identity("regular-user", "google", "google-user")
    calls = []

    def flow(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            code_verifier="pkce-secret",
            authorization_url=lambda **kw: (
                calls.append({"authorization": kw}) or "https://accounts.google.com/o/oauth2/auth?state=test", "state"),
            fetch_token=lambda **kw: calls.append(kw),
            credentials=SimpleNamespace(refresh_token="refresh-secret", to_json=lambda: '{"refresh_token":"refresh-secret"}'),
        )

    monkeypatch.setattr(auth, "_flow", flow)
    monkeypatch.setattr(auth, "_credential_subject", lambda credentials: identity.external_id)
    app = FastAPI()
    app.include_router(auth.router)
    app.dependency_overrides[current_identity] = lambda: identity
    with TestClient(app) as client:
        yield client, identity, calls, auth._directory(identity.uid)


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
    assert calls[1]["authorization"]["login_hint"] == identity.external_id
    assert calls[1]["authorization"]["access_type"] == "offline"
    assert calls[2] == {"state": "state", "code_verifier": "pkce-secret"}
    assert calls[3] == {"code": "code-secret", "timeout": 30}
    token = directory / "token.json"
    assert token.stat().st_mode & 0o777 == 0o600
    assert not pending.exists()
    assert client.post("/settings/colab/complete", json=body).status_code == 400
    assert "refresh-secret" not in client.get("/settings/colab").text


def test_each_google_user_can_connect_without_replacing_another_tenant(web):
    client, identity, calls, directory = web
    started = client.post("/settings/colab/start").json()
    pending_a = auth._pending(identity.uid)
    owner_a, subject_a = identity.uid, identity.external_id
    identity.uid, identity.external_id = "other-user", "google-other"
    assert client.get("/settings/colab").json() == {"can_authorize": True, "has_credentials": False}
    other = client.post("/settings/colab/start").json()
    assert client.post("/settings/colab/complete", json={"flow_id": other["flow_id"], "code": "other"}).status_code == 200
    other_token = auth._directory(identity.uid) / "token.json"
    assert other_token.is_file() and pending_a.is_file()
    identity.uid, identity.external_id = owner_a, subject_a
    assert client.get("/settings/colab").json()["has_credentials"] is False
    assert client.post("/settings/colab/complete", json={"flow_id": started["flow_id"], "code": "own"}).status_code == 200
    assert (directory / "token.json").is_file() and other_token.is_file()
    assert directory != other_token.parent


def test_non_google_identity_cannot_claim_a_google_account(web):
    client, identity, calls, directory = web
    identity.provider = "wechat"
    assert client.get("/settings/colab").json()["can_authorize"] is False
    assert client.post("/settings/colab/start").status_code == 403
    assert client.post("/settings/colab/complete", json={"flow_id": "x", "code": "x"}).status_code == 403
    assert not calls and not directory.exists()


def test_ordinary_google_user_is_not_gated_by_a_legacy_admin_list(web, monkeypatch):
    client, _, _, _ = web
    monkeypatch.setitem(auth.ENV, "V2MD_COLAB_ADMIN_UIDS", "unrelated-legacy-admin")
    assert client.get("/settings/colab").json()["can_authorize"] is True
    assert client.post("/settings/colab/start").status_code == 200


def test_flow_is_bound_to_uid_and_expires(web):
    client, identity, calls, directory = web
    started = client.post("/settings/colab/start").json()
    body = {"flow_id": started["flow_id"], "code": "code"}
    identity.uid = "other"
    assert client.post("/settings/colab/complete", json=body).status_code == 400
    identity.uid = "regular-user"
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


def test_wrong_google_account_cannot_replace_tenant_credentials(web, monkeypatch):
    client, identity, _, directory = web
    started = client.post("/settings/colab/start").json()
    token = directory / "token.json"
    token.write_text("previous-own-token")
    monkeypatch.setattr(auth, "_credential_subject", lambda credentials: "different-google-user")
    response = client.post("/settings/colab/complete", json={"flow_id": started["flow_id"], "code": "secret"})
    assert response.status_code == 400
    assert "相同的 Google 账号" in response.json()["detail"]
    assert token.read_text() == "previous-own-token"
    assert not auth._pending(identity.uid).exists()


def test_declined_colab_scope_is_not_a_connected_account(web, monkeypatch):
    client, _, _, directory = web
    original = auth._flow

    def partial(**kwargs):
        result = original(**kwargs)
        result.credentials.granted_scopes = ["openid"]
        return result

    monkeypatch.setattr(auth, "_flow", partial)
    started = client.post("/settings/colab/start").json()
    response = client.post("/settings/colab/complete", json={"flow_id": started["flow_id"], "code": "secret"})
    assert response.status_code == 400
    assert not (directory / "token.json").exists()


def test_missing_refresh_token_does_not_replace_a_previous_connection(web, monkeypatch):
    client, _, _, directory = web
    original = auth._flow

    def temporary(**kwargs):
        result = original(**kwargs)
        result.credentials.refresh_token = None
        return result

    monkeypatch.setattr(auth, "_flow", temporary)
    started = client.post("/settings/colab/start").json()
    token = directory / "token.json"
    token.write_text("previous-own-token")
    response = client.post("/settings/colab/complete", json={"flow_id": started["flow_id"], "code": "temporary-code"})
    assert response.status_code == 400
    assert token.read_text() == "previous-own-token"


def test_google_identity_check_failure_never_exposes_upstream_data(web, monkeypatch):
    client, _, _, directory = web
    started = client.post("/settings/colab/start").json()

    def failed(credentials):
        raise RuntimeError("private-access-token")

    monkeypatch.setattr(auth, "_credential_subject", failed)
    response = client.post("/settings/colab/complete", json={"flow_id": started["flow_id"], "code": "secret"})
    assert response.status_code == 400 and "private-access-token" not in response.text
    assert not (directory / "token.json").exists()


def test_colab_oauth_uses_the_configured_google_proxy(monkeypatch, tmp_path):
    proxy = "http://127.0.0.1:18080"
    monkeypatch.setitem(auth.ENV, "V2MD_GOOGLE_OAUTH_PROXY_URL", proxy)
    monkeypatch.setattr(auth.Path, "home", classmethod(lambda cls: tmp_path))
    flow = auth._flow(autogenerate_code_verifier=True)
    assert flow.oauth2session.proxies == {"http": proxy, "https": proxy}


def test_colab_account_verification_uses_the_configured_google_proxy(monkeypatch):
    import google.auth.transport.requests

    proxy = "http://127.0.0.1:18080"
    monkeypatch.setitem(auth.ENV, "V2MD_GOOGLE_OAUTH_PROXY_URL", proxy)
    session = SimpleNamespace(proxies={}, get=lambda url, **kwargs: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"id": "google-subject"}))

    class FakeSession:
        def __init__(self, credentials):
            pass

        def __enter__(self):
            return session

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(google.auth.transport.requests, "AuthorizedSession", FakeSession)
    assert auth._credential_subject(object()) == "google-subject"
    assert session.proxies == {"http": proxy, "https": proxy}


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

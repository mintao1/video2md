"""Google users connect their own Colab account through the CLI OAuth flow."""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from importlib import resources
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr

from .auth import Identity, current_identity
from .colab import _session, credential_directory
from .config import ENV


def _no_cache(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/settings/colab", tags=["Colab"], dependencies=[Depends(_no_cache)])
_TTL = 600


class CompleteBody(BaseModel):
    flow_id: str = Field(min_length=1, max_length=128)
    code: SecretStr = Field(min_length=1, max_length=4096)


def _allowed(request: Request, ident: Identity) -> bool:
    if (ENV.get("V2MD_AUTH") or "off").lower() in ("off", "none", ""):
        return ((ENV.get("V2MD_ENV") or "development") != "production"
                and request.client is not None
                and request.client.host in ("127.0.0.1", "::1", "localhost"))
    return bool(ident.uid) and ident.provider in ("google", "google_device")


def _require(request: Request, ident: Identity) -> None:
    if not _allowed(request, ident):
        raise HTTPException(403, "请使用 Google 账号登录后连接自己的 Colab")


def _directory(uid: str) -> Path:
    return credential_directory(uid)


def _pending(uid: str) -> Path:
    return _directory(uid) / "web-oauth.json"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".oauth-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        os.replace(name, path)  # mkstemp creates mode 0600, also when replacing an old token.
    finally:
        Path(name).unlink(missing_ok=True)


def _flow(**kwargs):
    from colab_cli.auth import PUBLIC_SCOPES, REMOTE_REDIRECT_URI
    from google_auth_oauthlib.flow import InstalledAppFlow

    custom = Path.home() / ".colab-cli-oauth-config.json"
    config = json.loads(custom.read_text() if custom.is_file() else
                        resources.files("colab_cli").joinpath("oauth_config.json").read_text())
    flow = InstalledAppFlow.from_client_config(config, PUBLIC_SCOPES, **kwargs)
    flow.redirect_uri = REMOTE_REDIRECT_URI
    proxy = (ENV.get("V2MD_GOOGLE_OAUTH_PROXY_URL")
             or ENV.get("V2PDF_GOOGLE_OAUTH_PROXY_URL") or "").strip()
    if proxy:
        flow.oauth2session.proxies.update({"http": proxy, "https": proxy})
    return flow


def _oauth_proxy_options() -> dict[str, str] | None:
    """Return explicit proxy kwargs for requests-oauthlib token exchanges.

    OAuth2Session.fetch_token passes its ``proxies`` argument through to
    ``requests``. Passing the default ``None`` overrides the session proxy
    mapping, so the production CONNECT relay must be supplied explicitly.
    """
    proxy = (ENV.get("V2MD_GOOGLE_OAUTH_PROXY_URL")
             or ENV.get("V2PDF_GOOGLE_OAUTH_PROXY_URL") or "").strip()
    return {"http": proxy, "https": proxy} if proxy else None


def _credential_subject(credentials) -> str:
    from google.auth.transport.requests import AuthorizedSession

    with AuthorizedSession(credentials) as session:
        proxy = (ENV.get("V2MD_GOOGLE_OAUTH_PROXY_URL")
                 or ENV.get("V2PDF_GOOGLE_OAUTH_PROXY_URL") or "").strip()
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
        proxy_options = _oauth_proxy_options()
        if proxy_options:
            response = session.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                timeout=15,
                proxies=proxy_options,
            )
        else:
            response = session.get("https://www.googleapis.com/oauth2/v2/userinfo", timeout=15)
        response.raise_for_status()
        return str(response.json().get("id") or "")


@router.get("")
def status(request: Request, ident: Identity = Depends(current_identity)) -> dict:
    allowed = _allowed(request, ident)
    return {"can_authorize": allowed,
            "has_credentials": (_directory(ident.uid) / "token.json").is_file() if allowed else False}


@router.post("/start")
def start(request: Request, ident: Identity = Depends(current_identity)) -> dict:
    _require(request, ident)
    try:
        with _session(wait=False, owner=ident.uid):
            flow = _flow(autogenerate_code_verifier=True)
            hint = {"login_hint": ident.external_id} if ident.provider in ("google", "google_device") else {}
            url, state = flow.authorization_url(prompt="consent", token_usage="remote", access_type="offline", **hint)
            flow_id = secrets.token_urlsafe(32)
            _write(_pending(ident.uid), json.dumps({"flow_id": flow_id, "state": state,
                   "verifier": flow.code_verifier, "expires": time.time() + _TTL}))
        return {"authorization_url": url, "flow_id": flow_id, "expires_in": _TTL}
    except BlockingIOError:
        raise HTTPException(409, "Colab 正在执行任务，请稍后授权") from None
    except (ImportError, FileNotFoundError):
        raise HTTPException(503, "Colab CLI 未安装，请先在后端运行 uv sync --frozen") from None


@router.post("/complete")
def complete(body: CompleteBody, request: Request, ident: Identity = Depends(current_identity)) -> dict:
    _require(request, ident)
    try:
        with _session(wait=False, owner=ident.uid):
            path = _pending(ident.uid)
            if not path.is_file():
                raise HTTPException(400, "请先打开 Google 授权页")
            pending = json.loads(path.read_text())
            if pending["expires"] < time.time() or not secrets.compare_digest(pending["flow_id"], body.flow_id):
                raise HTTPException(400, "授权已过期或已被替换，请重新开始")
            path.unlink()  # One exchange per flow, including failures; never replay a code.
            flow = _flow(state=pending["state"], code_verifier=pending["verifier"])
            try:
                code = body.code.get_secret_value().strip()
                proxy = _oauth_proxy_options()
                if proxy:
                    flow.fetch_token(code=code, timeout=30, proxies=proxy)
                else:
                    flow.fetch_token(code=code, timeout=30)
            except Exception:
                # Upstream OAuth errors can contain codes/tokens; never return them.
                raise HTTPException(400, "授权未完成，请重新打开授权页并填写新的授权码") from None
            if not flow.credentials.refresh_token:
                raise HTTPException(400, "未获得持续授权，请重新授权并同意所需权限")
            if ident.provider in ("google", "google_device"):
                try:
                    subject = _credential_subject(flow.credentials)
                except Exception:
                    raise HTTPException(400, "无法核对 Colab 账号，请重新授权") from None
                if not subject or subject != ident.external_id:
                    raise HTTPException(400, "请选择与当前登录相同的 Google 账号授权 Colab")
            granted = getattr(flow.credentials, "granted_scopes", None)
            if granted is not None and "https://www.googleapis.com/auth/colaboratory" not in granted:
                raise HTTPException(400, "尚未授予 Colab 权限，请重新授权并同意所需权限")
            _write(_directory(ident.uid) / "token.json", flow.credentials.to_json())
        return {"has_credentials": True, "can_authorize": True}
    except BlockingIOError:
        raise HTTPException(409, "Colab 正在执行任务，请稍后提交授权码") from None

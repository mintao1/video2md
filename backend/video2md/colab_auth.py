"""Browser entry for the CLI's remote copy/paste OAuth flow (service credentials)."""
from __future__ import annotations

import hashlib
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
from .colab import _session
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
    return ident.uid in {s.strip() for s in (ENV.get("V2MD_COLAB_ADMIN_UIDS") or "").split(",") if s.strip()}


def _require(request: Request, ident: Identity) -> None:
    if not _allowed(request, ident):
        raise HTTPException(403, "仅服务管理员可更换 Colab 账号，请配置 V2MD_COLAB_ADMIN_UIDS")


def _directory() -> Path:
    return Path.home() / ".config" / "colab-cli"


def _pending(uid: str) -> Path:
    return _directory() / ("web-oauth-" + hashlib.sha256(uid.encode()).hexdigest() + ".json")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    return flow


@router.get("")
def status(request: Request, ident: Identity = Depends(current_identity)) -> dict:
    allowed = _allowed(request, ident)
    return {"can_authorize": allowed,
            "has_credentials": (_directory() / "token.json").is_file() if allowed else False}


@router.post("/start")
def start(request: Request, ident: Identity = Depends(current_identity)) -> dict:
    _require(request, ident)
    try:
        with _session(wait=False):
            flow = _flow(autogenerate_code_verifier=True)
            url, state = flow.authorization_url(prompt="consent", token_usage="remote", access_type="offline")
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
        with _session(wait=False):
            path = _pending(ident.uid)
            if not path.is_file():
                raise HTTPException(400, "请先打开 Google 授权页")
            pending = json.loads(path.read_text())
            if pending["expires"] < time.time() or not secrets.compare_digest(pending["flow_id"], body.flow_id):
                raise HTTPException(400, "授权已过期或已被替换，请重新开始")
            path.unlink()  # One exchange per flow, including failures; never replay a code.
            flow = _flow(state=pending["state"], code_verifier=pending["verifier"])
            try:
                flow.fetch_token(code=body.code.get_secret_value().strip(), timeout=30)
            except Exception:
                # Upstream OAuth errors can contain codes/tokens; never return them.
                raise HTTPException(400, "授权未完成，请重新打开授权页并填写新的授权码") from None
            if not flow.credentials.refresh_token:
                raise HTTPException(400, "未获得持续授权，请重新授权并同意所需权限")
            _write(_directory() / "token.json", flow.credentials.to_json())
        return {"has_credentials": True, "can_authorize": True}
    except BlockingIOError:
        raise HTTPException(409, "Colab 正在执行任务，请稍后提交授权码") from None

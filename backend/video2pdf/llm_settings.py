"""Per-user LLM credentials. Secrets stay out of API responses and graph checkpoints."""
from __future__ import annotations

import contextlib
import contextvars
import fcntl
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from . import config

_CURRENT: contextvars.ContextVar[dict | None] = contextvars.ContextVar("user_llm_settings", default=None)


def normalize_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parts = urlsplit(value)
    if (any(char.isspace() for char in value) or parts.scheme not in ("http", "https")
            or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment):
        raise ValueError("请填写不含账号、查询参数和片段的 HTTP(S) API Base URL")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError("API URL 端口无效") from exc
    if value.endswith("/chat/completions"):
        value = value[:-len("/chat/completions")]
    return value


def _path() -> Path:
    return Path(config.ENV.get("V2PDF_LLM_SETTINGS_FILE") or
                config.PREFS_FILE.with_name("llm-settings.json"))


def _pool():
    if config.PREFS_STORE == "postgres":
        from .pg import shared_pool
        return shared_pool()
    return None


def load(uid: str) -> dict:
    pool = _pool()
    if pool is not None:
        with pool.connection() as conn:
            row = conn.execute("SELECT base_url, api_key, model FROM v2pdf_llm_settings WHERE uid=%s",
                               (uid,)).fetchone()
        return dict(zip(("base_url", "api_key", "model"), row)) if row else {}
    path = _path()
    # Atomic replacement means readers see either the old or new complete file.
    if not path.exists():
        return {}
    return dict(json.loads(path.read_text()).get(uid, {}))


def update(uid: str, *, base_url: str = "", api_key: str | None = None,
           model: str = "", clear: bool = False) -> dict:
    pool = _pool()
    if pool is not None:
        with pool.connection() as conn:
            if clear:
                conn.execute("DELETE FROM v2pdf_llm_settings WHERE uid=%s", (uid,))
                return {}
            row = conn.execute("""
                INSERT INTO v2pdf_llm_settings (uid, base_url, api_key, model)
                VALUES (%s, %s, COALESCE(%s, ''), %s)
                ON CONFLICT (uid) DO UPDATE SET base_url=EXCLUDED.base_url,
                    api_key=COALESCE(%s, v2pdf_llm_settings.api_key), model=EXCLUDED.model,
                    updated=now()
                RETURNING base_url, api_key, model
                """, (uid, base_url, api_key, model, api_key)).fetchone()
        return dict(zip(("base_url", "api_key", "model"), row))
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
            if clear:
                data.pop(uid, None)
                row = {}
            else:
                row = {"base_url": base_url, "model": model,
                       "api_key": api_key if api_key is not None else data.get(uid, {}).get("api_key", "")}
                data[uid] = row
            fd, name = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=path.parent)
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(data, fh, ensure_ascii=False)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(name, path)  # mkstemp creates the credential file with mode 0600.
            finally:
                Path(name).unlink(missing_ok=True)
            return dict(row)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def public(row: dict) -> dict:
    return {"base_url": row.get("base_url", ""), "model": row.get("model", ""),
            "has_api_key": bool(row.get("api_key")),
            "configured": bool(row.get("base_url") and row.get("api_key"))}


def current() -> dict | None:
    return _CURRENT.get()


@contextlib.contextmanager
def user_scope(uid: str):
    row = load(uid) if uid else {}
    token = _CURRENT.set(row if row.get("base_url") and row.get("api_key") else None)
    try:
        yield
    finally:
        _CURRENT.reset(token)

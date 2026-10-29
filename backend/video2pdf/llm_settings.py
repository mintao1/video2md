"""Per-user LLM credentials. Secrets stay out of API responses and graph checkpoints."""
from __future__ import annotations

import contextlib
import contextvars
import base64
import fcntl
import json
import os
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from . import config

_CURRENT: contextvars.ContextVar[dict | None] = contextvars.ContextVar("user_llm_settings", default=None)
_ENC_PREFIX = "v1:aesgcm:"


class CredentialEncryptionError(RuntimeError):
    """Raised when a credential cannot be decrypted safely."""


def _encryption_key() -> bytes:
    """Decode the 256-bit envelope key from the mounted secret file/env.

    Accepted formats are ``hex:<64 hex chars>`` and ``base64:<url-safe value>``;
    an unprefixed value is treated as url-safe base64 for concise secret files.
    There is deliberately no generated/default key: losing the key must fail
    closed rather than silently making old credentials unrecoverable.
    """
    raw = (config.ENV.get("V2MD_LLM_SETTINGS_KEY") or "").strip()
    if not raw:
        raise CredentialEncryptionError("未配置 V2MD_LLM_SETTINGS_KEY，无法读取用户 LLM 凭据")
    try:
        if raw.startswith("hex:"):
            key = bytes.fromhex(raw[4:])
        else:
            encoded = raw.removeprefix("base64:").encode("ascii")
            key = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
    except (ValueError, UnicodeEncodeError) as exc:
        raise CredentialEncryptionError("V2MD_LLM_SETTINGS_KEY 格式无效") from exc
    if len(key) != 32:
        raise CredentialEncryptionError("V2MD_LLM_SETTINGS_KEY 必须解码为 32 字节")
    return key


def _aad(uid: str) -> bytes:
    return f"video2md/llm-settings/{uid}".encode("utf-8")


def _encrypt(uid: str, value: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_encryption_key()).encrypt(nonce, value.encode("utf-8"), _aad(uid))
    token = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii").rstrip("=")
    return _ENC_PREFIX + token


def _decrypt(uid: str, value: str) -> str:
    if not value:
        return ""
    if not value.startswith(_ENC_PREFIX):
        raise CredentialEncryptionError(
            "检测到未加密的用户 LLM Key；请执行一次性凭据迁移后再启用生产服务")
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        encoded = value[len(_ENC_PREFIX):].encode("ascii")
        blob = base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
        if len(blob) <= 12:
            raise ValueError("ciphertext too short")
        return AESGCM(_encryption_key()).decrypt(blob[:12], blob[12:], _aad(uid)).decode("utf-8")
    except (InvalidTag, ValueError, UnicodeError, TypeError) as exc:
        raise CredentialEncryptionError("用户 LLM Key 解密失败（密钥错误或凭据已损坏）") from exc


def _decrypt_row(uid: str, row: dict) -> dict:
    result = dict(row)
    result["api_key"] = _decrypt(uid, str(result.get("api_key") or ""))
    return result


def migrate_plaintext(*, pool=None) -> int:
    """Encrypt legacy rows in-place before the API role accepts traffic.

    The schema constraint is ``NOT VALID`` so existing databases can be
    upgraded without putting plaintext into a new row.  The migrate command
    calls this with its owner connection; no plaintext value is logged or
    returned.  A wrong/missing envelope key fails closed.
    """
    if pool is None:
        pool = _pool()
    if pool is not None:
        changed = 0
        with pool.connection() as conn, conn.transaction():
            rows = conn.execute("SELECT uid, api_key FROM v2md_llm_settings").fetchall()
            for uid, value in rows:
                value = str(value or "")
                if not value or value.startswith(_ENC_PREFIX):
                    continue
                encrypted = _encrypt(uid, value)
                conn.execute("UPDATE v2md_llm_settings SET api_key=%s, updated=now() WHERE uid=%s",
                             (encrypted, uid))
                changed += 1
        return changed
    path = _path()
    if not path.exists():
        return 0
    with path.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(path.read_text())
        changed = 0
        for uid, row in data.items():
            value = str((row or {}).get("api_key") or "")
            if value and not value.startswith(_ENC_PREFIX):
                row["api_key"] = _encrypt(uid, value)
                changed += 1
        if changed:
            fd, name = tempfile.mkstemp(prefix=path.name + ".tmp.", dir=path.parent)
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(data, fh, ensure_ascii=False)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(name, path)
            finally:
                Path(name).unlink(missing_ok=True)
        fcntl.flock(lock, fcntl.LOCK_UN)
        return changed


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
        from .pg import tenant_connection
        with tenant_connection(pool) as conn:
            row = conn.execute("SELECT base_url, api_key, model FROM v2md_llm_settings WHERE uid=%s",
                               (uid,)).fetchone()
        return _decrypt_row(uid, dict(zip(("base_url", "api_key", "model"), row))) if row else {}
    path = _path()
    # Atomic replacement means readers see either the old or new complete file.
    if not path.exists():
        return {}
    row = dict(json.loads(path.read_text()).get(uid, {}))
    return _decrypt_row(uid, row) if row else {}


def update(uid: str, *, base_url: str = "", api_key: str | None = None,
           model: str = "", clear: bool = False) -> dict:
    pool = _pool()
    if pool is not None:
        from .pg import tenant_connection
        with tenant_connection(pool) as conn:
            if clear:
                conn.execute("DELETE FROM v2pdf_llm_settings WHERE uid=%s", (uid,))
                return {}
            encrypted = _encrypt(uid, api_key) if api_key is not None else None
            if not clear and encrypted is None:
                existing = conn.execute(
                    "SELECT api_key FROM v2md_llm_settings WHERE uid=%s", (uid,)).fetchone()
                if existing and existing[0]:
                    # Decrypt before preserving to validate the current key and
                    # prevent carrying forward an unreadable legacy token.
                    _decrypt(uid, str(existing[0]))
            row = conn.execute("""
                INSERT INTO v2pdf_llm_settings (uid, base_url, api_key, model)
                VALUES (%s, %s, COALESCE(%s, ''), %s)
                ON CONFLICT (uid) DO UPDATE SET base_url=EXCLUDED.base_url,
                    api_key=COALESCE(%s, v2pdf_llm_settings.api_key), model=EXCLUDED.model,
                    updated=now()
                RETURNING base_url, api_key, model
                """, (uid, base_url, encrypted, model, encrypted)).fetchone()
        return _decrypt_row(uid, dict(zip(("base_url", "api_key", "model"), row)))
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
                existing = data.get(uid, {})
                preserved = _decrypt(uid, str(existing.get("api_key") or "")) if existing.get("api_key") else ""
                row = {"base_url": base_url, "model": model,
                       "api_key": _encrypt(uid, api_key if api_key is not None else preserved)}
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
            return _decrypt_row(uid, row) if row else {}
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

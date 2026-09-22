"""身份存储（可插拔）：(provider, external_id) → 稳定内部 uid 的映射 + 首登自动开户。

uid 是「人」的稳定主键：一条记录挂多条外部身份（identities 列表），为账号联动
（同一人 Google + 微信归并到同一 uid）留位；资源 owner 恒存 uid，加 provider 不动隔离。

适配器：
- JsonFileStore：单文件 JSON + fcntl 跨进程锁（单实例部署够用；落盘纪律 =
  锁互斥读写整段 + per-thread tmp rename 原子替换）
- MemoryStore：进程内 dict（测试注入、以及验证协议本身够用的第二适配器）
换 Postgres/用户表 = 再写一个同协议适配器（resolve/put_identity），引擎与 provider 零改。
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import threading
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .models import Identity, Principal


def _noop_save(users: dict) -> None:
    pass


def _key(provider: str, external_id: str) -> str:
    return f"{provider}:{external_id}"


def _new_rec(uid: str, p: Principal | None = None) -> dict:
    return {
        "uid": uid,
        "email": (p.email if p else "") or "",
        "name": (p.name if p else "") or uid,
        "identities": ([{"provider": p.provider, "external_id": p.external_id}] if p else []),
        "created": datetime.now().isoformat(timespec="seconds"),
    }


def _find_by_external(users: dict, provider: str, external_id: str) -> tuple[str, dict] | None:
    want = _key(provider, external_id)
    for uid, rec in users.items():
        for ident in rec.get("identities", []):
            if _key(ident.get("provider", ""), ident.get("external_id", "")) == want:
                return uid, rec
    return None


def _provision(users: dict, p: Principal, save: Callable[[dict], None]) -> Identity:
    """锁内的纯 dict 操作（Json/Memory 共用）：命中回填 email/name，无则自动开户。"""
    hit = _find_by_external(users, p.provider, p.external_id)
    if hit:
        uid, rec = hit
        changed = False
        if p.email and not rec.get("email"):
            rec["email"] = p.email
            changed = True
        if p.name and not rec.get("name"):
            rec["name"] = p.name
            changed = True
        if changed:
            save(users)
        return Identity(uid=uid, provider=p.provider, external_id=p.external_id,
                        email=rec.get("email") or p.email, name=rec.get("name") or p.name)
    uid = "u_" + uuid.uuid4().hex[:12]
    users[uid] = _new_rec(uid, p)
    save(users)
    return Identity(uid=uid, provider=p.provider, external_id=p.external_id,
                    email=p.email, name=p.name, is_new=True)


def _add_identity(users: dict, provider: str, external_id: str, uid: str,
                  save: Callable[[dict], None]) -> None:
    """预置映射（存在则幂等追加）：账号联动/测试构造用。"""
    rec = users.get(uid)
    if rec is None:
        users[uid] = _new_rec(uid)
        rec = users[uid]
    if not any(_key(i.get("provider", ""), i.get("external_id", "")) == _key(provider, external_id)
               for i in rec["identities"]):
        rec["identities"].append({"provider": provider, "external_id": external_id})
    save(users)


class IdentityStore(ABC):
    """引擎依赖的最小协议：Principal → Identity（含落库副作用与自动开户）。"""

    @abstractmethod
    def resolve(self, p: Principal) -> Identity: ...

    @abstractmethod
    def put_identity(self, provider: str, external_id: str, uid: str) -> None:
        """预置 (provider, external_id) → uid（账号联动/测试构造用；幂等追加）。"""


class MemoryStore(IdentityStore):
    def __init__(self) -> None:
        self._users: dict[str, dict] = {}
        self._lock = threading.Lock()

    def resolve(self, p: Principal) -> Identity:
        with self._lock:
            return _provision(self._users, p, _noop_save)

    def put_identity(self, provider: str, external_id: str, uid: str) -> None:
        with self._lock:
            _add_identity(self._users, provider, external_id, uid, _noop_save)


class JsonFileStore(IdentityStore):
    """users.json 文件存储：fcntl 互斥读写整段；保存走 per-thread tmp + 原子 rename。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.lockfile = self.path.with_suffix(self.path.suffix + ".lock")
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save(self, users: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp.{threading.get_ident()}")
        tmp.write_text(json.dumps(users, ensure_ascii=False, indent=1))
        tmp.replace(self.path)

    @contextlib.contextmanager
    def _file_lock(self):
        """跨进程互斥：读写整段原子（同 repo 里 runs.json 的锁纪律）。"""
        with self._lock:
            self.lockfile.parent.mkdir(parents=True, exist_ok=True)
            with self.lockfile.open("a+") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    def resolve(self, p: Principal) -> Identity:
        with self._file_lock():
            return _provision(self._load(), p, self._save)

    def put_identity(self, provider: str, external_id: str, uid: str) -> None:
        with self._file_lock():
            _add_identity(self._load(), provider, external_id, uid, self._save)

"""用户偏好存储（教材库：收藏/阅读进度）：(uid, run_id) → {fav, progress}。

json 缺省档（V2MD_PREFS_FILE，仿 runs.json：fcntl 跨进程锁 + 原子 rename 落盘）；
V2MD_PREFS_STORE=postgres 且 V2MD_PG_DSN 在 → PostgresPrefsStore（v2pdf_prefs 表，
行级原子，建表上收 migrations/0003——构造函数不建表，P0-5 纪律）。

分派惰性解析仿 runs._runs_backend（ENV 调用时读，测试可直改 _STORE 切档）。
run 归属校验在端点层（_require_run_access）：表只按 (uid, run_id) 存偏好，不 FK
v2pdf_runs——json 档无处挂 FK，跨存储一致性由端点纪律保证。
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path

from .config import PREFS_FILE


class JsonPrefsStore:
    """文件档：{uid: {run_id: {fav, progress}}}，整表读写、锁内原子。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._lockfile = path.with_suffix(".lock")

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp.{os.getpid()}")  # 按 pid：rename 不打架
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        tmp.replace(self.path)

    def list_uid(self, uid: str) -> dict[str, dict]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._lockfile.open("a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                return {rid: dict(p) for rid, p in self._load().get(uid, {}).items()}
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def upsert(self, uid: str, run_id: str, *, fav: bool | None = None,
               progress: int | None = None) -> dict:
        """部分更新 upsert（None=不动该字段）；无行先建默认行再套用更新。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._lockfile.open("a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                data = self._load()
                row = data.setdefault(uid, {}).setdefault(run_id,
                                                          {"fav": False, "progress": 0})
                if fav is not None:
                    row["fav"] = bool(fav)
                if progress is not None:
                    row["progress"] = int(progress)
                self._save(data)
                return dict(row)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


class PostgresPrefsStore:
    """v2pdf_prefs 表（migrations/0003）：行级原子 upsert，多副本并发安全。"""

    def __init__(self, pool):
        self.pool = pool  # 建表已上收 migrations/0003，构造只接线

    def list_uid(self, uid: str) -> dict[str, dict]:
        from .pg import tenant_connection
        with tenant_connection(self.pool) as conn:
            rows = conn.execute("""
                SELECT run_id, fav, progress FROM v2pdf_prefs WHERE uid=%s
                """, (uid,)).fetchall()
        return {r[0]: {"fav": bool(r[1]), "progress": int(r[2])} for r in rows}

    def upsert(self, uid: str, run_id: str, *, fav: bool | None = None,
               progress: int | None = None) -> dict:
        from .pg import tenant_connection
        with tenant_connection(self.pool) as conn:
            row = conn.execute("""
                INSERT INTO v2pdf_prefs (uid, run_id, fav, progress)
                VALUES (%s, %s, COALESCE(%s, false), COALESCE(%s, 0))
                ON CONFLICT (uid, run_id) DO UPDATE SET
                    fav = COALESCE(%s, v2pdf_prefs.fav),
                    progress = COALESCE(%s, v2pdf_prefs.progress),
                    updated = now()
                RETURNING fav, progress
                """, (uid, run_id, fav, progress, fav, progress)).fetchone()
        return {"fav": bool(row[0]), "progress": int(row[1])}


_STORE: JsonPrefsStore | PostgresPrefsStore | None = None  # 测试可直改本属性切档
_store_resolved = False


def prefs_store() -> JsonPrefsStore | PostgresPrefsStore:
    global _STORE, _store_resolved
    if not _store_resolved:
        _store_resolved = True
        from .config import ENV
        if (ENV.get("V2MD_PREFS_STORE") or "").strip().lower() == "postgres":
            from . import pg
            _STORE = PostgresPrefsStore(pg.shared_pool())
        else:
            _STORE = JsonPrefsStore(PREFS_FILE)
    assert _STORE is not None  # _store_resolved 置位后必已装配（测试直改 _STORE 走同一出口）
    return _STORE

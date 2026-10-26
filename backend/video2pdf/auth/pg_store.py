"""IdentityStore 的 Postgres 适配器（宿主侧实现——acctkit 保持零依赖，只继承其协议）。

表：
- v2pdf_users(uid PK, created, record jsonb)——record 与 JsonFileStore 的用户记录同形
  （uid/email/name/identities[]/created），迁移/导出零转换。
- v2pdf_identities(provider, external_id) PK → uid——映射的规范化形态（文件档是
  记录体内 identities 列表 + O(n) 扫描，这里下沉成索引查询）。

resolve 语义逐条对齐 acctkit.store._provision：命中回填 email/name；miss 自动开户
（u_+uuid12，is_new=True）。并发首登靠 PK 冲突裁决：identity INSERT ... DO NOTHING
输掉的一方收回自己的孤儿开户行、以先落库者为准（文件档靠 fcntl 锁内串行达到同一效果）。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from psycopg.types.json import Json

from acctkit.models import Identity, Principal
from acctkit.store import IdentityStore

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool


class PostgresIdentityStore(IdentityStore):
    def __init__(self, pool: ConnectionPool):
        self.pool = pool  # 建表已上收 video2pdf/migrations/0001（P0-5），构造只接线

    # ---- 内部：用户记录读写（行锁串行化，防并发回填/追加丢更新）

    def _get_user(self, conn, uid: str) -> dict | None:
        row = conn.execute("SELECT record FROM v2pdf_users WHERE uid = %s", (uid,)).fetchone()
        return row[0] if row else None

    def _put_user(self, conn, rec: dict) -> None:
        conn.execute("""
            INSERT INTO v2pdf_users (uid, created, record) VALUES (%(uid)s, %(created)s, %(rec)s)
            ON CONFLICT (uid) DO UPDATE SET record = EXCLUDED.record""",
            {"uid": rec["uid"], "created": rec.get("created", ""),
             "rec": Json(rec)})

    def _find(self, conn, provider: str, external_id: str) -> tuple[str, dict] | None:
        row = conn.execute("""
            SELECT u.uid, u.record FROM v2pdf_identities i
            JOIN v2pdf_users u ON u.uid = i.uid
            WHERE i.provider = %s AND i.external_id = %s""",
            (provider, external_id)).fetchone()
        return (row[0], row[1]) if row else None

    # ---- 协议面

    def resolve(self, p: Principal) -> Identity:
        with self.pool.connection() as conn:
            with conn.transaction():  # 共享池是 autocommit——多语句原子性显式开事务
                hit = self._find(conn, p.provider, p.external_id)
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
                        self._put_user(conn, rec)
                    return Identity(uid=uid, provider=p.provider, external_id=p.external_id,
                                    email=rec.get("email") or p.email,
                                    name=rec.get("name") or p.name)
                # miss：开户 + 挂身份（并发同身份：identity 主键裁决，输家收孤儿行）
                uid = "u_" + uuid.uuid4().hex[:12]
                rec = {"uid": uid, "email": p.email or "", "name": p.name or uid,
                       "identities": [{"provider": p.provider, "external_id": p.external_id}],
                       "created": datetime.now().isoformat(timespec="seconds")}
                self._put_user(conn, rec)
                n = conn.execute("""
                    INSERT INTO v2pdf_identities (provider, external_id, uid) VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING""", (p.provider, p.external_id, uid)).rowcount
                if not n:  # 并发首登：以先落库者为准
                    hit = self._find(conn, p.provider, p.external_id)
                    if hit and hit[0] != uid:
                        conn.execute("DELETE FROM v2pdf_users WHERE uid = %s", (uid,))
                        uid, rec = hit
                return Identity(uid=uid, provider=p.provider, external_id=p.external_id,
                                email=rec.get("email") or p.email,
                                name=rec.get("name") or p.name, is_new=True)

    def put_identity(self, provider: str, external_id: str, uid: str) -> None:
        with self.pool.connection() as conn:
            with conn.transaction():
                rec = self._get_user(conn, uid)
                if rec is None:  # uid 尚无用户行：建最小记录（对齐 _add_identity 的开户行为）
                    rec = {"uid": uid, "email": "", "name": uid, "identities": [],
                           "created": datetime.now().isoformat(timespec="seconds")}
                if not any(i.get("provider") == provider and i.get("external_id") == external_id
                           for i in rec.get("identities", [])):
                    rec.setdefault("identities", []).append(
                        {"provider": provider, "external_id": external_id})
                    self._put_user(conn, rec)  # record 内 identities 与映射表保持同形
                conn.execute("""
                    INSERT INTO v2pdf_identities (provider, external_id, uid) VALUES (%s, %s, %s)
                    ON CONFLICT (provider, external_id) DO UPDATE SET uid = EXCLUDED.uid""",
                    (provider, external_id, uid))


def bulk_load(pool: ConnectionPool, users: dict) -> tuple[int, int]:
    """users.json 全量导入（迁移脚本用）：users 行 + identities 展开，幂等 upsert。"""
    n_users = n_idents = 0
    with pool.connection() as conn:
        with conn.transaction():
            for uid, rec in users.items():
                row = dict(rec)
                row["uid"] = uid
                conn.execute("""
                    INSERT INTO v2pdf_users (uid, created, record) VALUES (%(uid)s, %(created)s, %(rec)s)
                    ON CONFLICT (uid) DO UPDATE SET record = EXCLUDED.record""",
                    {"uid": uid, "created": rec.get("created", ""), "rec": Json(row)})
                n_users += 1
                for ident in rec.get("identities", []):
                    conn.execute("""
                        INSERT INTO v2pdf_identities (provider, external_id, uid) VALUES (%s, %s, %s)
                        ON CONFLICT (provider, external_id) DO UPDATE SET uid = EXCLUDED.uid""",
                        (ident.get("provider", ""), ident.get("external_id", ""), uid))
                    n_idents += 1
    return n_users, n_idents

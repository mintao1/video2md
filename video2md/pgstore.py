"""runs / state 的 Postgres 适配器（P3 存储分派 PG 档的实现体）。

设计口径：与 JSON 文件档**函数语义一一对应**——
- PostgresRunsStore.load() 返回 {run_id: run_dict}（dedup / 清理器的全表扫描语义保留）；
  merge(run) = 行级 upsert（多实例并发安全，替代文件档「fcntl 锁内读改写」）。
  无全表替换（文件档 _save_runs 的 save_all 语义在多实例下会覆盖他进程写入，PG 档禁用）。
- PostgresStateStore.save() 把「全量替换」降为逐 fp upsert——state 键集只增不删
  （重跑覆盖同 fp），语义等价。

表名带 v2md_ 前缀（与 langgraph checkpointer 的 checkpoints* 表同库不撞）；
owner/status/created 提取列供后续队列查询（P4b）与运维 SQL 用，真相仍是 record jsonb。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from psycopg.types.json import Json

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool


class PostgresRunsStore:
    """runs 表：run_id PK + 提取列 + record jsonb（整条 run dict）。"""

    def __init__(self, pool: ConnectionPool):
        self.pool = pool
        with pool.connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS v2md_runs (
                    run_id  text PRIMARY KEY,
                    owner   text NOT NULL DEFAULT '',
                    status  text NOT NULL DEFAULT '',
                    created text NOT NULL DEFAULT '',
                    updated timestamptz NOT NULL DEFAULT now(),
                    record  jsonb NOT NULL
                )""")
            conn.execute("CREATE INDEX IF NOT EXISTS v2md_runs_status_idx ON v2md_runs (status)")
            conn.execute("CREATE INDEX IF NOT EXISTS v2md_runs_owner_idx ON v2md_runs (owner)")

    def load(self) -> dict:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT record FROM v2md_runs").fetchall()
        out: dict = {}
        for (rec,) in rows:
            out[rec["run_id"]] = rec
        return out

    def merge(self, run: dict) -> None:
        """行级 upsert（原子）。调用方无需持锁——文件档的锁纪律在此内化为行锁。"""
        with self.pool.connection() as conn:
            conn.execute("""
                INSERT INTO v2md_runs (run_id, owner, status, created, record)
                VALUES (%(run_id)s, %(owner)s, %(status)s, %(created)s, %(rec)s)
                ON CONFLICT (run_id) DO UPDATE SET
                    owner = EXCLUDED.owner,
                    status = EXCLUDED.status,
                    created = EXCLUDED.created,
                    record = EXCLUDED.record,
                    updated = now()""",
                {"run_id": run.get("run_id", ""),
                 "owner": run.get("owner", "") or "",
                 "status": run.get("status", "") or "",
                 "created": run.get("created", "") or "",
                 "rec": Json(run)})


class PostgresStateStore:
    """state 表：fp PK + record jsonb（幂等记录：note/transcript/title/url/owner…）。

    文件档的 fp 是外层 dict key（不进记录体），故表单存 fp 列。"""

    def __init__(self, pool: ConnectionPool):
        self.pool = pool
        with pool.connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS v2md_state (
                    fp      text PRIMARY KEY,
                    updated timestamptz NOT NULL DEFAULT now(),
                    record  jsonb NOT NULL
                )""")

    def load(self) -> dict:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT fp, record FROM v2md_state").fetchall()
        return {fp: rec for fp, rec in rows}

    def save(self, s: dict) -> None:
        """逐 fp upsert（键只增不删 → 与文件档全量替换语义等价）。"""
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO v2md_state (fp, record) VALUES (%s, %s)
                    ON CONFLICT (fp) DO UPDATE SET record = EXCLUDED.record, updated = now()
                    """, [(fp, Json(rec)) for fp, rec in s.items()])

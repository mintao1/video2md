"""schema 迁移纪律（P0-5）：migrations/ 是 DDL 唯一真相，构造函数不再建表。

机制（对齐 ROADMAP P0-5 验收口径）：
- migrations/NNNN_name.sql 按号递增；本模块持有注册表 MIGRATIONS（号→文件名），
  加新迁移 = 落一个 SQL 文件 + 注册表加一行，别处零改动。
- v2md_schema_version 表逐迁移记账（version PK + 文件名 + 应用时间，审计可查）。
- apply_migrations：advisory lock 串行化（并发副本/并发跑只一份生效）+ 每迁移
  单事务（SQL 与版本行同进同退）。全部幂等——存量库（基线前建的表）重跑 no-op
  直接补记版本行，即 Flyway「基线盖戳」语义，无需特殊探测分支。
- validate_schema：库版本落后 → 拒起并提示跑 `python -m video2md migrate`；
  超前（新库旧代码，回滚场景）→ 拒起提示先处理部署，别让旧代码硬跑新 schema。
- 校验收口在 pg.shared_pool()（store/checkpointer/events 全走它）；make_pool 不校验
  ——migrate CLI 与真库测试用 make_pool 自管 schema。

注意：SQL 脚本经 ClientCursor 整体下发（简单查询协议，多语句/`$$` 函数体均可），
但不得含 `%s` 占位符；需要参数化的语句不属迁移脚本该干的事。
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import psycopg.errors
from psycopg import ClientCursor

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

# 注册表：新迁移在尾部加行（号必须递增、不复用已发布号）
MIGRATIONS: list[tuple[int, str]] = [
    (1, "0001_init.sql"),
    (2, "0002_membership.sql"),
    (3, "0003_prefs.sql"),
    (4, "0004_llm_settings.sql"),
    (5, "0005_tenant_rls.sql"),
    (6, "0006_llm_key_ciphertext.sql"),
]
SCHEMA_LATEST = MIGRATIONS[-1][0]

_MIG_DIR = Path(__file__).parent / "migrations"
_ADVISORY_KEY = 0x76326D64  # 'v2md' 四字节——本库迁移互斥锁键，勿改


def apply_migrations(pool: ConnectionPool) -> list[int]:
    """应用全部待迁移版本，返回本次新应用的版本号列表（空 = 已就绪，幂等重跑安全）。"""
    done: list[int] = []
    with pool.connection() as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_ADVISORY_KEY,))
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS v2md_schema_version (
                    version    integer PRIMARY KEY,
                    name       text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )""")
            applied = {int(r[0]) for r in
                       conn.execute("SELECT version FROM v2md_schema_version").fetchall()}
            for ver, name in MIGRATIONS:
                if ver in applied:
                    continue
                sql = (_MIG_DIR / name).read_text(encoding="utf-8")
                with conn.transaction():  # DDL 与版本行同进同退
                    # 多语句脚本必须走 ClientCursor（客户端绑定后走简单查询协议；
                    # 池连接默认扩展协议 = 预备语句，拒收多命令）。代价：脚本内不得
                    # 出现 %s 占位符（参数化语句走版本行 INSERT 那样的默认游标）。
                    orig_factory = conn.cursor_factory
                    conn.cursor_factory = ClientCursor
                    try:
                        conn.execute(sql)
                    finally:
                        conn.cursor_factory = orig_factory
                    conn.execute(
                        "INSERT INTO v2md_schema_version (version, name) VALUES (%s, %s)",
                        (ver, name))
                done.append(ver)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_KEY,))
    return done


def validate_schema(pool: ConnectionPool) -> None:
    """版本不符抛 RuntimeError（拒起并给修法）；供 shared_pool 建池即校验。"""
    try:
        with pool.connection() as conn:
            row = conn.execute("SELECT max(version) FROM v2md_schema_version").fetchone()
        v = int(row[0]) if row and row[0] is not None else 0
    except psycopg.errors.UndefinedTable:
        v = 0
    if v < SCHEMA_LATEST:
        raise RuntimeError(
            f"schema 版本落后（库 {v} < 代码 {SCHEMA_LATEST}）：跑 `python -m video2md migrate`"
            " 后再启动（容器：docker compose run --rm --no-deps video2md"
            " python -m video2md migrate）")
    if v > SCHEMA_LATEST:
        raise RuntimeError(
            f"schema 版本超前（库 {v} > 代码 {SCHEMA_LATEST}）：本代码比库旧——"
            "回滚部署或升级代码后再跑，勿让旧代码硬跑新 schema")

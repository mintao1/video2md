"""进程级共享 Postgres 设施：连接池单例 + DSN 解析。

存储分派（V2MD_RUNS_STORE / V2MD_STATE_STORE / V2MD_USERS_STORE = postgres 时）
与 checkpointer 的 postgres 档、事件扇出（V2MD_EVENTS=pg）共用这一个池——
四个消费者各开一套连接没有必要。池 lazy 建立（首次 shared_pool() 时），
api lifespan 退出时 close_shared_pool() 统一收尾；CLI（graph-run/__main__）进程
退出即释放，无需显式关。

池口径与 graph.make_checkpointer_from_env 既有实现一致：psycopg_pool +
autocommit + prepare_threshold=0（psycopg 单连接非线程安全，图跑在 to_thread
工作线程里，必须池化）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

from .config import ENV

_POOL: ConnectionPool | None = None


def pg_dsn() -> str:
    return (ENV.get("V2MD_PG_DSN") or "").strip()


def pg_enabled() -> bool:
    return bool(pg_dsn())


def make_pool(dsn: str) -> ConnectionPool:
    from psycopg_pool import ConnectionPool
    return ConnectionPool(conninfo=dsn, min_size=1, max_size=5, open=True,
                          kwargs={"autocommit": True, "prepare_threshold": 0})


def shared_pool() -> ConnectionPool:
    """lazy 单例；DSN 缺失抛 RuntimeError（调用方应先 pg_enabled() 判档）。

    P0-5：建池即校验 schema 版本——store/checkpointer/events 全走本收口，落后/超前
    一律拒起并提示跑 `python -m video2md migrate`。make_pool 不校验（migrate CLI
    与真库测试用 make_pool 自管 schema）。"""
    global _POOL
    if _POOL is None:
        if not pg_enabled():
            raise RuntimeError("V2MD_PG_DSN 未配置：PG 档存储/事件扇出不可用")
        _POOL = make_pool(pg_dsn())
        from .pgmigrate import validate_schema
        validate_schema(_POOL)
    return _POOL


def close_shared_pool() -> None:
    global _POOL
    if _POOL is not None:
        try:
            _POOL.close()
        except Exception as exc:  # 退出路径不拦
            print(f"⚠ PG 池关闭异常（忽略）：{exc}")
        _POOL = None

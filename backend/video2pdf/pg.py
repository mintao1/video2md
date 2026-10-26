"""进程级共享 Postgres 设施：连接池单例 + DSN 解析。

存储分派（V2PDF_RUNS_STORE / V2PDF_STATE_STORE / V2PDF_USERS_STORE = postgres 时）
与 checkpointer 的 postgres 档、事件扇出（V2PDF_EVENTS=pg）共用这一个池——
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
    return (ENV.get("V2PDF_PG_DSN") or "").strip()


def pg_enabled() -> bool:
    return bool(pg_dsn())


def pg_timeout_s() -> float:
    """PG 超时预算（P1-8，单一键三处生效：connect / 池获取 / 语句超时）。

    批判② R1「池获取无超时 = 故障放大器」在 P1-1/P1-4 后加重——PG 已是任务表真相源，
    check_cluster 等在事件循环上同步打 PG：无语句超时的锁等待会无限占住连接，池耗尽
    把每轮巡检冻在默认 30s。预算钳 ≥1s 防误配成 0（等价禁用全部超时）。
    """
    try:
        return max(float(ENV.get("V2PDF_PG_TIMEOUT_S", "10") or 10), 1.0)
    except ValueError:
        return 10.0


def make_pool(dsn: str) -> ConnectionPool:
    from psycopg_pool import ConnectionPool
    t = pg_timeout_s()
    return ConnectionPool(conninfo=dsn, min_size=1, max_size=5, open=True,
                          timeout=t,  # 池获取预算（默认 30s → 跟随统一预算）
                          kwargs={"autocommit": True, "prepare_threshold": 0,
                                  "connect_timeout": int(t),        # 建池/重连 TCP+auth 有界
                                  # Force UTF-8 for migration comments and JSON text even when
                                  # a legacy database was initialized with SQL_ASCII.
                                  "options": f"-c statement_timeout={int(t * 1000)} -c client_encoding=UTF8",
                                  # 语句超时：锁等待/挂起查询被打断——池耗尽退化为瞬态
                                  })


def shared_pool() -> ConnectionPool:
    """lazy 单例；DSN 缺失抛 RuntimeError（调用方应先 pg_enabled() 判档）。

    P0-5：建池即校验 schema 版本——store/checkpointer/events 全走本收口，落后/超前
    一律拒起并提示跑 `python -m video2pdf migrate`。make_pool 不校验（migrate CLI
    与真库测试用 make_pool 自管 schema）。"""
    global _POOL
    if _POOL is None:
        if not pg_enabled():
            raise RuntimeError("V2PDF_PG_DSN 未配置：PG 档存储/事件扇出不可用")
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

"""进程内 run 事件总线：RunLog._write 的第三个下游 sink → SSE 端点扇出。

图在 asyncio.to_thread 的工作线程里跑，publish 会从非事件循环线程被调；订阅队列活在
服务端主循环（SSE 协程里建）。故跨线程投递用 loop.call_soon_threadsafe(q.put_nowait)。
主循环在 lifespan 起时 bind_loop 钉住。

双实现（V2MD_EVENTS，缺省 local）：
- local：单进程 dict[run_id → list[Queue]]（Phase 1 现状，逐字节不变）。
- pg：PG LISTEN/NOTIFY pub/sub 跨进程扇出——publish 在进程内扇出之外补一条 NOTIFY；
  模块起一条专用 LISTEN 连接的后台泵线程，把**其它进程**的通知按同一套
  call_soon_threadsafe 机制投给本进程订阅者（payload 带 src 进程号，自身回声跳过，
  不双投）。端点与挂点（RunLog._write）零改动——契约测试只锁 local 档，PG 档在
  test_pg_stores.py 用双实例互通钉住。
"""
from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections import defaultdict

from .config import ENV

_LOCK = threading.Lock()
_LOOP: asyncio.AbstractEventLoop | None = None
_SUBS: dict[str, list[asyncio.Queue]] = defaultdict(list)

_QUEUE_MAX = 500  # 慢/断客户端兜底：队列满丢最旧之外的新事件，不反压主管线
_TERMINAL_EVENTS = {"graph:end"}  # 见到即视为 run 收尾，端点据此可关流

_CHANNEL = "v2md_events"
_NOTIFY_MAX = 7900  # PG NOTIFY payload 上限 8000B，留余量
_PROC_ID = uuid.uuid4().hex[:8]  # 扇出回声去重：泵跳过自己进程发的通知

_PUMP: threading.Thread | None = None
_PUMP_STOP = threading.Event()


def _pg_mode() -> bool:
    """每调用现读 env（dict 查找，够便宜）——测试可随时切档。"""
    return (ENV.get("V2MD_EVENTS") or "local").strip().lower() == "pg"


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _LOOP
    _LOOP = loop
    if _pg_mode():
        _ensure_pump()


def subscribe(run_id: str) -> asyncio.Queue:
    """SSE 协程（主循环）调用：建队列挂到 run_id。"""
    if _pg_mode():
        _ensure_pump()  # 泵可能先于 bind_loop 被订阅触发（防御；bind_loop 也会拉起）
    q: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    with _LOCK:
        _SUBS[run_id].append(q)
    return q


def unsubscribe(run_id: str, q: asyncio.Queue) -> None:
    with _LOCK:
        subs = _SUBS.get(run_id)
        if subs is not None and q in subs:
            subs.remove(q)
        if subs is not None and not subs:
            _SUBS.pop(run_id, None)


def _fanout_local(run_id: str, rec: dict) -> None:
    """投给本进程订阅者（跨线程安全：主循环 call_soon_threadsafe）。永不抛。"""
    if not run_id or _LOOP is None:
        return
    with _LOCK:
        subs = list(_SUBS.get(run_id, ()))
    if not subs:
        return

    def _put() -> None:  # 在主循环线程执行
        for q in subs:
            try:
                q.put_nowait(rec)
            except asyncio.QueueFull:
                pass  # 丢给慢消费者：反压主管线是大忌

    try:
        _LOOP.call_soon_threadsafe(_put)
    except RuntimeError:
        pass  # 主循环已关（进程退出中）


def publish(run_id: str, rec: dict) -> None:
    """RunLog._write（工作线程）调用：把已脱敏事件投给该 run 的所有订阅者。永不抛。"""
    _fanout_local(run_id, rec)
    if _pg_mode():
        _notify_pg(run_id, rec)


def _notify_pg(run_id: str, rec: dict, src: str = _PROC_ID) -> None:
    """NOTIFY 一条事件（超长截断保连接；异常静默——远程扇出失败不拖垮本地）。"""
    try:
        payload = json.dumps({"src": src, "run_id": run_id, "rec": rec}, ensure_ascii=False)
        if len(payload.encode()) > _NOTIFY_MAX:
            slim = {k: v for k, v in rec.items() if not isinstance(v, (dict, list))}
            slim["_truncated"] = True
            payload = json.dumps({"src": src, "run_id": run_id, "rec": slim}, ensure_ascii=False)
            if len(payload.encode()) > _NOTIFY_MAX:
                return  # 极端兜底：连骨架都超限，放弃远程扇出（本地订阅不受影响）
        from . import pg
        with pg.shared_pool().connection() as conn:
            conn.execute("SELECT pg_notify(%s, %s)", (_CHANNEL, payload))
    except Exception:
        pass


def _ensure_pump() -> None:
    global _PUMP
    if _PUMP is not None and _PUMP.is_alive():
        return
    _PUMP_STOP.clear()
    _PUMP = threading.Thread(target=_pump_loop, daemon=True, name="v2md-events-pg")
    _PUMP.start()


def _pump_loop() -> None:
    """专用 LISTEN 连接的后台泵：其它进程的 NOTIFY → 本进程订阅者。断线重连退避。"""
    import time

    import psycopg

    from . import pg
    dsn = pg.pg_dsn()
    while not _PUMP_STOP.is_set():
        conn = None
        try:
            conn = psycopg.connect(dsn, autocommit=True)
            conn.execute(f"LISTEN {_CHANNEL}")
            while not _PUMP_STOP.is_set():
                for n in conn.notifies(timeout=5):  # 空闲 5s 醒来查 stop
                    _on_notify(n.payload)
        except Exception:
            time.sleep(1.0)  # 断线/库未就绪：退避重连
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def _on_notify(payload: str) -> None:
    try:
        msg = json.loads(payload)
        if msg.get("src") == _PROC_ID:
            return  # 自身回声：本地扇出已投过，跳过防双投
        _fanout_local(str(msg.get("run_id") or ""), msg.get("rec") or {})
    except Exception:
        pass  # 脏 payload 不许杀泵


def shutdown() -> None:
    """lifespan 退出时停泵（local 档 no-op）。daemon 线程，进程退出本身也兜底。"""
    _PUMP_STOP.set()


def is_terminal(rec: dict) -> bool:
    return str(rec.get("event") or "") in _TERMINAL_EVENTS

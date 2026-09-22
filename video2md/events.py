"""进程内 run 事件总线：RunLog._write 的第三个下游 sink → SSE 端点扇出。

图在 asyncio.to_thread 的工作线程里跑，publish 会从非事件循环线程被调；订阅队列活在
服务端主循环（SSE 协程里建）。故跨线程投递用 loop.call_soon_threadsafe(q.put_nowait)。
主循环在 lifespan 起时 bind_loop 钉住。

Phase 1 单进程：dict[run_id → list[Queue]] 够用。Redis pub/sub（跨 worker/多实例扇出）
是二阶段——届时只换本模块的 publish/subscribe 实现，端点与挂点不动。
"""
from __future__ import annotations

import asyncio
import threading
from collections import defaultdict

_LOCK = threading.Lock()
_LOOP: asyncio.AbstractEventLoop | None = None
_SUBS: dict[str, list[asyncio.Queue]] = defaultdict(list)

_QUEUE_MAX = 500  # 慢/断客户端兜底：队列满丢最旧之外的新事件，不反压主管线
_TERMINAL_EVENTS = {"graph:end"}  # 见到即视为 run 收尾，端点据此可关流


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _LOOP
    _LOOP = loop


def subscribe(run_id: str) -> asyncio.Queue:
    """SSE 协程（主循环）调用：建队列挂到 run_id。"""
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


def publish(run_id: str, rec: dict) -> None:
    """RunLog._write（工作线程）调用：把已脱敏事件投给该 run 的所有订阅者。永不抛出。"""
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


def is_terminal(rec: dict) -> bool:
    return str(rec.get("event") or "") in _TERMINAL_EVENTS

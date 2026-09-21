"""Langfuse sink：RunLog 事件流的异步上报（OTLP/HTTP JSON，零 SDK 依赖）。

为什么不是官方 ingestion API：本项目 Langfuse 组织建于 2026-09-16 之后，
legacy /api/public/ingestion 对新组织「201 收下但永不落库」（实测黑洞）；
读写新组织只认 OTLP（POST /api/public/otel/v1/traces）+ v2 查询 API。

拓扑：每个 serve run 一条 OTel trace（traceId = sha256("serve-"+run_id) 前 32 hex），
root span = run（name 取 url，带 langfuse.trace.name / run_id / dify_run_id 属性），
stage → entry → 远端子操作逐级 parentSpanId 挂树。Dify workflow 的 trace 由 Dify
自己上报到同一项目；dify_run_id 属性让两条 trace 在 Langfuse 里可互查。

降级纪律：未配置 LANGFUSE_SK / 网络异常 / 队列满 → 静默丢弃（一行 stderr），
logs/run-*.jsonl 仍是唯一真相。__stage 子进程退出时 atexit flush。
凭据来自 dify_kit SSOT（~/.config/dify-zcode.env），由 config 模块注入 os.environ。
"""
from __future__ import annotations

import atexit
import hashlib
import os
import queue
import sys
import threading
import time
import uuid

import httpx

_QUEUE: queue.Queue = queue.Queue(maxsize=500)  # 满即丢：观测绝不能反压主管线
_FLUSH_INTERVAL_S = 2.0
_HTTP_TIMEOUT = 5.0
_STARTED = False
_DISABLED_NOTE_EMITTED = False


def _cfg() -> tuple[str, str, str] | None:
    pk = os.environ.get("LANGFUSE_PK", "")
    sk = os.environ.get("LANGFUSE_SK", "")
    host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
    return (pk, sk, host.rstrip("/")) if pk and sk else None


def _flush_loop() -> None:
    while True:
        time.sleep(_FLUSH_INTERVAL_S)
        _drain()


def _drain() -> None:
    spans = []
    while len(spans) < 100:
        try:
            spans.append(_QUEUE.get_nowait())
        except queue.Empty:
            break
    if spans:
        _post(spans)


def _post(spans: list[dict]) -> None:
    global _DISABLED_NOTE_EMITTED
    cfg = _cfg()
    if not cfg:
        return
    pk, sk, host = cfg
    body = {"resourceSpans": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "video2md-serve"}}]},
        "scopeSpans": [{"scope": {"name": "video2md"}, "spans": spans}],
    }]}
    try:
        r = httpx.post(f"{host}/api/public/otel/v1/traces", auth=(pk, sk),
                       json=body, timeout=_HTTP_TIMEOUT)
        if r.status_code != 200 and not _DISABLED_NOTE_EMITTED:
            print(f"⚠️ langfuse sink HTTP {r.status_code}（后续静默，jsonl 不受影响）",
                  file=sys.stderr)
            _DISABLED_NOTE_EMITTED = True
    except Exception:
        if not _DISABLED_NOTE_EMITTED:
            print("⚠️ langfuse sink 网络异常（后续静默，jsonl 不受影响）", file=sys.stderr)
            _DISABLED_NOTE_EMITTED = True


def _enqueue(span: dict) -> None:
    try:
        _QUEUE.put_nowait(span)
    except queue.Full:
        pass  # 队列满即丢，绝不阻塞


def _ensure_started() -> bool:
    """惰性启动 flush 线程（首次有事件且配了 key 才起）。"""
    global _STARTED
    if _STARTED:
        return True
    if not _cfg():
        return False
    _STARTED = True
    threading.Thread(target=_flush_loop, daemon=True).start()
    atexit.register(flush)
    return True


def flush(timeout_s: float = 5.0) -> None:
    """进程退出前排空（atexit 注册；__stage 子进程短生命周期尤其需要）。"""
    deadline = time.monotonic() + timeout_s
    while not _QUEUE.empty() and time.monotonic() < deadline:
        _drain()


def _otel_trace_id(run_id: str) -> str:
    return hashlib.sha256(f"serve-{run_id}".encode()).hexdigest()[:32]


def _otel_span_id(span_id: str) -> str:
    return hashlib.sha256(span_id.encode()).hexdigest()[:16]


def _attr(key: str, val) -> dict:
    return {"key": key, "value": {"stringValue": str(val)}}


def _span(trace_id: str, span_id: str, name: str, start_ns: int, end_ns: int,
          parent: str = "", attrs: list | None = None, error: str = "") -> dict:
    s = {"traceId": trace_id, "spanId": span_id, "name": name, "kind": 1,
         "startTimeUnixNano": str(start_ns), "endTimeUnixNano": str(max(end_ns, start_ns)),
         "attributes": list(attrs or []),
         "status": {"code": 2 if error else 1}}
    if parent:
        s["parentSpanId"] = parent
    if error:
        s["attributes"].append({"key": "error", "value": {"stringValue": error[:300]}})
    return s


class _RunTraceState:
    """每个 run 一条 OTel trace：root span 随首个事件开立，开口 span 跟踪到闭合。"""

    def __init__(self, run_id: str, label: str, trace_name: str, trace_meta: dict):
        self.trace_id = _otel_trace_id(run_id)
        self.root_span_id = _otel_span_id(f"root-{run_id}")
        self.open: dict[str, tuple[str, int, str]] = {}  # spanId → (event, start_ns, parent)
        t0 = time.time_ns()
        attrs = [_attr("langfuse.trace.name", trace_name or label),
                 _attr("run_id", run_id)]
        attrs += [_attr(k, v) for k, v in trace_meta.items() if v]
        # root 立即开立（长阶段跑到哪一步在 Langfuse 实时可见，不用等收尾）
        _enqueue(_span(self.trace_id, self.root_span_id, trace_name or label, t0, t0,
                       attrs=attrs))


_RUNS: dict[str, _RunTraceState] = {}
_RUNS_LOCK = threading.Lock()


def record(rec: dict, *, run_id: str, label: str, trace_name: str = "",
           trace_metadata: dict | None = None) -> None:
    """RunLog._write 的双写钩子。rec 是已脱敏的事件记录（含 event/spanId/status/durationMs…）。"""
    if not _ensure_started():
        return
    with _RUNS_LOCK:
        st = _RUNS.get(run_id)
        if st is None:
            st = _RunTraceState(run_id, label, trace_name, trace_metadata or {})
            _RUNS[run_id] = st
        status, span_id, event = rec.get("status"), rec.get("spanId"), rec.get("event")
        now = time.time_ns()
        if not span_id:
            # info 点事件 → 零时长 span 挂 root（OTel 没有 Langfuse 的 event 类型，瞬时点代替）
            _enqueue(_span(st.trace_id, uuid.uuid4().hex[:16], str(event), now, now,
                           parent=st.root_span_id,
                           attrs=[_attr(k, v) for k, v in rec.items()
                                  if k not in ("ts", "run_id", "event")]))
            return
        if status == "started":
            st.open[span_id] = (str(event), now, rec.get("parentSpanId") or st.root_span_id)
            return
        # completed/failed/exited → 闭合 span
        opened = st.open.pop(span_id, None)
        start_ns = opened[1] if opened else now - int((rec.get("durationMs") or 0) * 1e6)
        parent_raw = (opened[2] if opened else None) or rec.get("parentSpanId") or ""
        parent = st.root_span_id if not parent_raw else (
            parent_raw if parent_raw == st.root_span_id else _otel_span_id(parent_raw))
        err = str(rec.get("error") or "") if status in ("failed", "exited") else ""
        _enqueue(_span(st.trace_id, _otel_span_id(span_id), str(event), start_ns, now,
                       parent=parent,
                       attrs=[_attr("run_id", run_id),
                              _attr("durationMs", rec.get("durationMs"))],
                       error=err))

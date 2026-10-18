"""platform-otel-sdk 核心实现：provider 管理 + RunTrace 桥 + event→span 映射。

从 video2md/observability.py 和 bookserve/observability.py 提取的共享逻辑。
产品特有逻辑（metric 定义、LLM gen_ai 映射）保留在产品内部。
"""
from __future__ import annotations

import atexit
import base64
import hashlib
import os
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.trace import (
    NonRecordingSpan,
    Span,
    SpanContext,
    Status,
    StatusCode,
    TraceFlags,
    set_span_in_context,
)

# ── 默认直方图桶（秒） ──
DEFAULT_HISTOGRAM_BOUNDS_S = [1, 5, 15, 60, 300, 900, 1800, 3600, 7200]

# ── 控制字段（不进 attributes） ──
_CONTROL_KEYS = {"ts", "run_id", "event", "spanId", "parentSpanId", "status",
                 "durationMs", "error", "force_status", "ts_mono"}

# ── 模块状态 ──
_STARTED = False
_TRACER_PROVIDER: TracerProvider | None = None
_METER_PROVIDER: MeterProvider | None = None
_TRACER = None
_METER = None
_LOCK = threading.Lock()

_SERVICE_NAME: str = ""
_SERVICE_NAMESPACE: str = ""
_HASH_PREFIX: str = ""
_TRACE_TAGS: list[str] = []
_MAKE_INSTRUMENTS_FN: Callable | None = None
_RUNTIME_GAUGE_FN: Callable | None = None

_RUNS: dict[str, "_RunTrace"] = {}


# ═══════════════════════════════════════════════════════════════════════════════
# 端点解析
# ═══════════════════════════════════════════════════════════════════════════════

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _endpoint() -> tuple[str, dict, bool] | None:
    """→ (base, headers, is_collector)。base 为 OTLP 路径前缀。"""
    ep = _env("OTEL_EXPORTER_OTLP_ENDPOINT").rstrip("/")
    if ep:
        return ep, {}, True
    pk, sk = _env("LANGFUSE_PK"), _env("LANGFUSE_SK")
    if pk and sk:
        host = _env("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
        basic = base64.b64encode(f"{pk}:{sk}".encode()).decode()
        return f"{host}/api/public/otel", {"Authorization": f"Basic {basic}"}, False
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# 初始化
# ═══════════════════════════════════════════════════════════════════════════════

def init_otel(
    *,
    service_name: str,
    service_namespace: str,
    hash_prefix: str,
    trace_tags: list[str],
    deployment_env: str = "",
    endpoint: str = "",
    headers: dict | None = None,
    span_exporter=None,
    metric_reader=None,
    views: list[View] | None = None,
    make_instruments: Callable | None = None,
    metric_export_interval_ms: int = 15000,
) -> bool:
    """初始化 OTel TracerProvider + MeterProvider。幂等。

    Args:
        service_name: OTel service.name（如 "video2md-serve"）
        service_namespace: OTel service.namespace（如 "video2md"）
        hash_prefix: 确定性 trace_id 前缀（如 "video2md-"）
        trace_tags: Langfuse trace tags（如 ["video2md"]）
        deployment_env: 环境标签（默认从 PRODUCT_ENV 读取）
        endpoint: 显式 OTLP endpoint（覆盖自动发现）
        headers: 显式 OTLP headers
        span_exporter: 测试注入（InMemorySpanExporter）
        metric_reader: 测试注入（InMemoryMetricReader）
        views: 自定义 histogram views
        make_instruments: 回调 `(meter) -> dict[str, instrument]`
        metric_export_interval_ms: metrics 导出间隔

    Returns:
        True = 已初始化（可导出），False = 无端点（纯本地 no-op）
    """
    global _STARTED, _TRACER_PROVIDER, _METER_PROVIDER, _TRACER, _METER
    global _SERVICE_NAME, _SERVICE_NAMESPACE, _HASH_PREFIX, _TRACE_TAGS, _MAKE_INSTRUMENTS_FN

    with _LOCK:
        if _STARTED:
            return _TRACER_PROVIDER is not None
        _STARTED = True

        _SERVICE_NAME = service_name
        _SERVICE_NAMESPACE = service_namespace
        _HASH_PREFIX = hash_prefix
        _TRACE_TAGS = list(trace_tags)
        _MAKE_INSTRUMENTS_FN = make_instruments

        is_collector = True
        if endpoint:
            base, hdrs = endpoint.rstrip("/"), dict(headers or {})
        elif span_exporter is None:
            cfg = _endpoint()
            if cfg is None:
                return False
            base, hdrs, is_collector = cfg

        env_name = deployment_env or _env("PRODUCT_ENV", "development")
        resource = Resource.create({
            "service.name": service_name,
            "service.namespace": service_namespace,
            "deployment.environment.name": env_name,
        })

        # ── TracerProvider ──
        tp = TracerProvider(resource=resource)
        if span_exporter is not None:
            tp.add_span_processor(SimpleSpanProcessor(span_exporter))
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            tp.add_span_processor(BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{base}/v1/traces", headers=hdrs)))
        _TRACER_PROVIDER = tp
        _TRACER = tp.get_tracer(service_name)

        # ── MeterProvider ──
        if metric_reader is not None or (is_collector and span_exporter is None):
            readers = [metric_reader] if metric_reader is not None else None
            if readers is None:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
                from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
                readers = [PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=f"{base}/v1/metrics", headers=hdrs),
                    export_interval_millis=metric_export_interval_ms)]
            mp = MeterProvider(resource=resource, views=views or [], metric_readers=readers)
            _METER_PROVIDER = mp
            _METER = mp.get_meter(service_name)
            if _MAKE_INSTRUMENTS_FN is not None:
                _MAKE_INSTRUMENTS_FN(_METER)

        atexit.register(flush)
        return True


def get_tracer():
    """返回 Tracer（init_otel 后可用）。"""
    return _TRACER


def get_meter():
    """返回 Meter（init_otel 后可用，仅 collector 模式）。"""
    return _METER


# ═══════════════════════════════════════════════════════════════════════════════
# RunTrace: 每 run 一条 trace
# ═══════════════════════════════════════════════════════════════════════════════

class _RunTrace:
    """每 run 一条 trace 的进程内状态：root span + spanId → SDK span 映射。

    trace_id = sha256(hash_prefix + run_id)[:32]（确定性）。
    """

    def __init__(self, run_id: str, label: str, trace_name: str, trace_meta: dict,
                 user_id: str = ""):
        tid = int(hashlib.sha256(f"{_HASH_PREFIX}{run_id}".encode()).hexdigest()[:32], 16)
        sid = int(hashlib.sha256(f"root-{run_id}".encode()).hexdigest()[:16], 16)
        anchor = SpanContext(trace_id=tid, span_id=sid, is_remote=True,
                             trace_flags=TraceFlags(TraceFlags.SAMPLED))
        parent_ctx = set_span_in_context(NonRecordingSpan(anchor))

        env_name = _env("PRODUCT_ENV", "development")
        attrs: dict[str, str | bool | int | float | Sequence[str]] = {
            "langfuse.trace.name": f"{_SERVICE_NAME}-run",
            "run_id": run_id,
            "langfuse.environment": env_name,
        }
        if user_id:
            attrs["langfuse.user.id"] = user_id
        attrs["langfuse.trace.tags"] = _TRACE_TAGS
        if trace_name:
            attrs["langfuse.observation.input"] = trace_name
        attrs.update({f"langfuse.trace.metadata.{k}": str(v)
                      for k, v in (trace_meta or {}).items() if v})

        assert _TRACER is not None
        self.root: Span = _TRACER.start_span(f"{_SERVICE_NAME}-run", attributes=attrs,
                                             context=parent_ctx)
        self.open: dict[str, Span] = {}
        self.user_id = user_id


# ═══════════════════════════════════════════════════════════════════════════════
# record: RunLog event → span 映射
# ═══════════════════════════════════════════════════════════════════════════════

def _scalar_attrs(rec: dict, extra_control_keys: set[str] | None = None,
                  llm_attr_fn: Callable | None = None) -> dict:
    """rec 控制字段之外的标量 → OTel attributes。

    Args:
        rec: RunLog event dict
        extra_control_keys: 产品附加的控制字段（如 {"usage", "model"}）
        llm_attr_fn: 可选回调 `(event, rec) -> dict` 添加 LLM gen_ai 属性
    """
    out: dict = {}
    control = _CONTROL_KEYS | (extra_control_keys or set())
    event = str(rec.get("event") or "")
    if llm_attr_fn is not None:
        out.update(llm_attr_fn(event, rec))
    for k, v in rec.items():
        if k in control:
            continue
        if isinstance(v, bool):
            out[k] = v
        elif isinstance(v, (int, float)):
            out[k] = v
        elif isinstance(v, str) and v:
            out[k] = v
    return out


def record(
    rec: dict,
    *,
    run_id: str,
    label: str,
    trace_name: str = "",
    trace_metadata: dict | None = None,
    user_id: str = "",
    otel_attrs: dict | None = None,
    extra_control_keys: set[str] | None = None,
    llm_attr_fn: Callable | None = None,
) -> None:
    """RunLog._write 双写钩子：event 流 → OTel spans。

    产品调用方式（在 RunLog._write 中）：
        platform_otel_sdk.record(
            rec, run_id=run_id, label=label,
            extra_control_keys={"usage", "model"},  # video2md
            llm_attr_fn=video2md_llm_attrs,          # video2md
        )
    """
    if _TRACER is None:
        return
    event = str(rec.get("event") or "")
    span_id = rec.get("spanId", "")
    parent_span_id = rec.get("parentSpanId", "")
    status = rec.get("status", "")
    error = rec.get("error", "")
    duration_ms = rec.get("durationMs")

    # 确保 run 存在
    rt = _RUNS.get(run_id)
    if rt is None:
        rt = _RunTrace(run_id, label, trace_name, trace_metadata or {}, user_id)
        _RUNS[run_id] = rt

    attrs = _scalar_attrs(rec, extra_control_keys, llm_attr_fn)
    if otel_attrs:
        attrs.update(otel_attrs)
    if duration_ms is not None:
        attrs["duration_ms"] = float(duration_ms)
    if error:
        attrs["error.message"] = str(error)[:500]

    # 父 span
    if parent_span_id and parent_span_id in rt.open:
        parent_span = rt.open[parent_span_id]
        ctx = set_span_in_context(parent_span)
    else:
        ctx = set_span_in_context(rt.root)

    status_code = StatusCode.OK
    if status == "error" or error:
        status_code = StatusCode.ERROR

    if event == "start":
        if span_id not in rt.open:
            span = _TRACER.start_span(label or event, attributes=attrs, context=ctx)
            rt.open[span_id] = span
    elif event in ("completed", "failed"):
        span = rt.open.pop(span_id, None)
        if span is not None:
            span.set_attributes(attrs)
            span.set_status(Status(status_code))
            span.end()
    elif event == "info":
        _TRACER.start_span(label or "info", attributes=attrs, context=ctx).end()
    else:
        span = _TRACER.start_span(label or event, attributes=attrs, context=ctx)
        if span_id:
            rt.open[span_id] = span


def close_run(run_id: str) -> None:
    """强制关闭残余 open spans + root span。"""
    rt = _RUNS.pop(run_id, None)
    if rt is None:
        return
    for span in list(rt.open.values()):
        try:
            span.set_status(Status(StatusCode.ERROR, "force-closed"))
            span.end()
        except Exception:
            pass
    rt.open.clear()
    try:
        rt.root.end()
    except Exception:
        pass


def open_run_ids() -> list[str]:
    """返回当前 open 的 run_id 列表。"""
    return list(_RUNS.keys())


# ═══════════════════════════════════════════════════════════════════════════════
# Flush / Drain
# ═══════════════════════════════════════════════════════════════════════════════

def flush(timeout_s: float = 5.0) -> None:
    """Force-flush TracerProvider + MeterProvider。"""
    if _TRACER_PROVIDER is not None:
        try:
            _TRACER_PROVIDER.force_flush(timeout_millis=int(timeout_s * 1000))
        except Exception:
            pass
    if _METER_PROVIDER is not None:
        try:
            _METER_PROVIDER.force_flush(timeout_millis=int(timeout_s * 1000))
        except Exception:
            pass


def drain(settle_s: float = 6.0) -> None:
    """双次 flush + settle 延迟，确保 BatchSpanProcessor 根 span 送达。

    bookserve 发现单次 flush 不够——BatchSpanProcessor 的调度可能延迟根 span 导出。
    所有产品应使用 drain() 代替 flush() 作为 shutdown 路径。
    """
    flush()
    time.sleep(settle_s)
    flush()


# ═══════════════════════════════════════════════════════════════════════════════
# Runtime Gauges
# ═══════════════════════════════════════════════════════════════════════════════

def register_runtime_gauges(fn: Callable) -> None:
    """注册运行态表盘取数回调：fn() → {"queued": int, "active": int}。"""
    global _RUNTIME_GAUGE_FN
    _RUNTIME_GAUGE_FN = fn


def _get_runtime_gauge_fn():
    return _RUNTIME_GAUGE_FN


# ═══════════════════════════════════════════════════════════════════════════════
# 测试辅助
# ═══════════════════════════════════════════════════════════════════════════════

def reset_for_tests() -> None:
    """重置所有状态（测试隔离用）。"""
    global _STARTED, _TRACER_PROVIDER, _METER_PROVIDER, _TRACER, _METER
    global _SERVICE_NAME, _SERVICE_NAMESPACE, _HASH_PREFIX, _TRACE_TAGS
    global _MAKE_INSTRUMENTS_FN, _RUNTIME_GAUGE_FN
    flush()
    with _LOCK:
        _RUNS.clear()
        _TRACER_PROVIDER = None
        _METER_PROVIDER = None
        _TRACER = None
        _METER = None
        _STARTED = False
        _SERVICE_NAME = ""
        _SERVICE_NAMESPACE = ""
        _HASH_PREFIX = ""
        _TRACE_TAGS = []
        _MAKE_INSTRUMENTS_FN = None
        _RUNTIME_GAUGE_FN = None
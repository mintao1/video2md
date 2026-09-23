"""OTel SDK 桥：RunLog 事件流 → traces+metrics → 本机 collector（或直连 Langfuse 回退）。

拓扑（2026-09-22 定稿）：
    video2md ──OTLP/HTTP──► otel-collector(:4318) ─┬─ traces → Langfuse Cloud
                                                   └─ metrics → prometheus exporter :8889
出口优先级：OTEL_EXPORTER_OTLP_ENDPOINT（投 collector，无认证）
           > LANGFUSE_PK/SK/HOST 直连（回退，仅 traces——Langfuse 不收 metrics）
           > 都未配置：纯本地 jsonl（logs/run-*.jsonl 仍是唯一真相），零开销。

RunLog API 不动（span/info/current_log），本模块只是 _write 的下游 sink：
- 每 run 一条 trace：trace_id = sha256("video2md-"+run_id)[:32]（确定性——evals 旁路
  scripts/eval_notes.py 据此把分数回挂到原 trace；2026-09-22 前为 serve- 前缀，
  Langfuse 里的历史回放 trace 仍用旧前缀）；root span 随首个事件开立，
  stage → entry → llm:* 逐级父子；__stage 子进程与 api 主进程靠同一 trace_id 拼合。
- LLM span（llm:<task>）按 Langfuse OTel 映射打 gen_ai.* 属性 → 渲染为 generation 计成本
  （模型定价在 Langfuse 项目侧配；provider 不回 usage 时只记字符数，不编造）。
- 指标经 MeterProvider 周期导出，进程重启清零（运行态指标，非账本）。

降级纪律不变：export 异常绝不反压主管线（BatchProcessor 内部吞错），jsonl 先于 sink 落盘；
__stage 短命子进程靠 atexit flush。provider 刻意不注册进 opentelemetry 全局单例
（模块内自引用即可）——测试得以用 InMemory 导出器反复 init/reset，互不串台。
"""
from __future__ import annotations

import atexit
import base64
import hashlib
import os
import threading
from collections.abc import Sequence

from opentelemetry.metrics import Observation
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

from .config import ENV

# 直方图桶（秒）：覆盖转录/OCR 的秒级到本地剪辑的近百分钟级
_HISTOGRAM_BOUNDS_S = [1, 5, 15, 60, 300, 900, 1800, 3600, 7200]

# rec 里的控制字段（不进 attributes）；usage/model 由 LLM 映射单独处理
_CONTROL_KEYS = {"ts", "run_id", "event", "spanId", "parentSpanId", "status",
                 "durationMs", "error", "force_status", "ts_mono", "usage", "model",
                 "user_id"}

_STARTED = False
_TRACER_PROVIDER: TracerProvider | None = None
_METER_PROVIDER: MeterProvider | None = None
_TRACER = None
_LOCK = threading.Lock()

# 指标仪表（init 后非 None；辅助函数全部惰性判空，未配置端点时零开销）
_M: dict = {}  # name → instrument
_RUNTIME_GAUGE_FN = None  # api 注册：() → {"queued": int, "active": int}

_RUNS: dict[str, "_RunTrace"] = {}


def _env(key: str, default: str = "") -> str:
    """os.environ 显式设置（含空串=显式关闭）优先；config.ENV（.env/secrets 合并）兜底。"""
    if key in os.environ:
        return os.environ[key]
    return ENV.get(key, default)


def _artifact_mode() -> str:
    """产物模式 tag（dashboard 按 mode 分维度）。动态读 config 属性：测试可 monkeypatch。"""
    from . import config as _c  # 惰性：避免模块级循环；ARTIFACT_MODE 是 import 后常量
    return getattr(_c, "ARTIFACT_MODE", "vault")


def _endpoint() -> tuple[str, dict, bool] | None:
    """→ (base, headers, is_collector)。base 为 signal 路径前缀（…/v1/traces 由调用方拼）。"""
    ep = _env("OTEL_EXPORTER_OTLP_ENDPOINT").rstrip("/")
    if ep:
        return ep, {}, True
    pk, sk = _env("LANGFUSE_PK"), _env("LANGFUSE_SK")
    if pk and sk:
        host = _env("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
        basic = base64.b64encode(f"{pk}:{sk}".encode()).decode()
        return f"{host}/api/public/otel", {"Authorization": f"Basic {basic}"}, False
    return None


def init(*, endpoint: str = "", headers: dict | None = None,
         span_exporter=None, metric_reader=None) -> bool:
    """起 Tracer/Meter Provider。幂等。无端点且未注入导出器 → 纯本地 jsonl（False）。

    测试注入：span_exporter=InMemorySpanExporter() + metric_reader=InMemoryMetricReader()，
    不经网络（SimpleSpanProcessor 同步导出，断言即所得）。
    """
    global _STARTED, _TRACER_PROVIDER, _METER_PROVIDER, _TRACER
    with _LOCK:
        if _STARTED:
            return _TRACER_PROVIDER is not None
        _STARTED = True
        is_collector = True
        if endpoint:
            base, hdrs = endpoint.rstrip("/"), dict(headers or {})
        elif span_exporter is None:
            cfg = _endpoint()
            if cfg is None:
                return False
            base, hdrs, is_collector = cfg
        resource = Resource.create({"service.name": "video2md-serve",
                                    "service.namespace": "video2md",
                                    "deployment.environment.name": _env("V2MD_ENV", "development")})
        tp = TracerProvider(resource=resource)
        if span_exporter is not None:
            tp.add_span_processor(SimpleSpanProcessor(span_exporter))
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            tp.add_span_processor(BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{base}/v1/traces", headers=hdrs)))
        _TRACER_PROVIDER = tp
        _TRACER = tp.get_tracer("video2md")
        if metric_reader is not None or (is_collector and span_exporter is None):
            # 直连 Langfuse 回退时跳过 metrics：Langfuse 只收 traces，打了也是 404。
            # 测试注入必须 span_exporter + metric_reader 成对给（否则 metrics 无从导出）。
            views = [View(instrument_name="v2md_stage_seconds",
                          aggregation=ExplicitBucketHistogramAggregation(_HISTOGRAM_BOUNDS_S)),
                     View(instrument_name="v2md_llm_seconds",
                          aggregation=ExplicitBucketHistogramAggregation(_HISTOGRAM_BOUNDS_S))]
            readers = [metric_reader] if metric_reader is not None else None
            if readers is None:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
                from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
                readers = [PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=f"{base}/v1/metrics", headers=hdrs),
                    export_interval_millis=15000)]
            mp = MeterProvider(resource=resource, views=views, metric_readers=readers)
            _METER_PROVIDER = mp
            _make_instruments(mp.get_meter("video2md"))
        atexit.register(flush)
        return True


def _make_instruments(meter) -> None:
    _M["runs"] = meter.create_counter("v2md_runs", description="完成的 run（按终态）")
    _M["cb"] = meter.create_counter("v2md_circuit_breaker_trips", description="熔断次数（按阶段）")
    _M["watchdog"] = meter.create_counter("v2md_watchdog_trips", description="看门狗卡死判定次数")
    _M["stage_entries"] = meter.create_counter("v2md_stage_entries",
                                               description="阶段条目结果（ok/failed/skipped）")
    _M["llm_requests"] = meter.create_counter("v2md_llm_requests", description="LLM 调用（按任务/终态）")
    _M["llm_tokens"] = meter.create_counter("v2md_llm_tokens", description="LLM token（按任务/in|out）")
    _M["stage_seconds"] = meter.create_histogram("v2md_stage_seconds", description="阶段耗时（秒）")
    _M["llm_seconds"] = meter.create_histogram("v2md_llm_seconds", description="LLM 调用耗时（秒）")

    def _queued(_options):
        fn = _RUNTIME_GAUGE_FN
        yield Observation((fn() or {}).get("queued", 0) if fn else 0)

    def _active(_options):
        fn = _RUNTIME_GAUGE_FN
        yield Observation((fn() or {}).get("active", 0) if fn else 0)

    _M["queue_gauge"] = meter.create_observable_gauge("v2md_queue_queued", [_queued],
                                                      description="排队中的 run")
    _M["active_gauge"] = meter.create_observable_gauge("v2md_run_active", [_active],
                                                       description="运行中 run（0/1）")


def register_runtime_gauges(fn) -> None:
    """api 注册运行态表盘的取数回调：fn() → {"queued": int, "active": int}。"""
    global _RUNTIME_GAUGE_FN
    _RUNTIME_GAUGE_FN = fn


def _ensure_started() -> bool:
    return init() if not _STARTED else _TRACER_PROVIDER is not None


class _RunTrace:
    """每 run 一条 trace 的进程内状态：root span + 开口 span 表（RunLog spanId → SDK span）。

    trace_id 钉死为 sha256("video2md-"+run_id)（2026-09-22 前为 serve-）：SDK 不从父
    context 继承就会随机生成，所以伪造一个远端 SpanContext 作 root 的父锚点
    （锚点自身永不上报）。evals 旁路（scripts/eval_notes.py）与 __stage 子进程
    靠同一约定拼回这条 trace。
    """

    def __init__(self, run_id: str, label: str, trace_name: str, trace_meta: dict,
                 user_id: str = ""):
        tid = int(hashlib.sha256(f"video2md-{run_id}".encode()).hexdigest()[:32], 16)
        sid = int(hashlib.sha256(f"root-{run_id}".encode()).hexdigest()[:16], 16)
        anchor = SpanContext(trace_id=tid, span_id=sid, is_remote=True,
                             trace_flags=TraceFlags(TraceFlags.SAMPLED))
        parent_ctx = set_span_in_context(NonRecordingSpan(anchor))
        # Langfuse OTel 映射（官方 ingestion 文档）：trace 名/用户/metadata 走 langfuse.*
        # 前缀才落进 trace 的可过滤字段，裸 key 只会进 metadata.attributes 杂物袋
        attrs: dict[str, str | bool | int | float | Sequence[str]] = {
            "langfuse.trace.name": "video2md-run", "run_id": run_id,
            "langfuse.environment": _env("V2MD_ENV", "development")}
        if user_id:
            attrs["langfuse.user.id"] = user_id
        # Langfuse 官方口径 tags 是 string[]（OTel 原生支持序列值，无需拼串）
        attrs["langfuse.trace.tags"] = ["video2md", f"mode:{_artifact_mode()}"]
        # root 观察体的 input = 入口 URL（trace 级 input 在 v4 已废弃，用语义等价物；
        # 名/身份分离：名字当 API 保持常量，URL 是数据进 input）
        if trace_name:
            attrs["langfuse.observation.input"] = trace_name
        attrs.update({f"langfuse.trace.metadata.{k}": str(v)
                      for k, v in (trace_meta or {}).items() if v})
        # root 立即开立：长阶段跑到哪一步在 Langfuse 实时可见，不用等收尾
        assert _TRACER is not None  # _ensure_started() 已保证 init() 成功
        self.root: Span = _TRACER.start_span("video2md-run", attributes=attrs,
                                             context=parent_ctx)
        self.open: dict[str, Span] = {}
        self.user_id = user_id


def _scalar_attrs(rec: dict) -> dict:
    """rec 控制字段之外的标量 → OTel attributes；LLM span 加 gen_ai 映射。"""
    out: dict = {}
    event = str(rec.get("event") or "")
    if event.startswith("llm:"):
        out["langfuse.observation.type"] = "generation"
        out["llm.task"] = event[4:]
        if rec.get("model"):
            out["gen_ai.request.model"] = str(rec["model"])
        usage = rec.get("usage")
        if isinstance(usage, dict):
            if usage.get("prompt_tokens") is not None:
                out["gen_ai.usage.input_tokens"] = int(usage["prompt_tokens"])
            if usage.get("completion_tokens") is not None:
                out["gen_ai.usage.output_tokens"] = int(usage["completion_tokens"])
    for k, v in rec.items():
        if k in _CONTROL_KEYS:
            continue
        if isinstance(v, bool):
            out[k] = v
        elif isinstance(v, (int, float)):
            out[k] = v
        elif isinstance(v, str):
            out[k] = v[:500]
    if rec.get("durationMs") is not None:
        out["durationMs"] = float(rec["durationMs"])
    if rec.get("error"):
        out["error"] = str(rec["error"])[:300]
    return out


def record(rec: dict, *, run_id: str, label: str, trace_name: str = "",
           trace_metadata: dict | None = None,
           otel_attrs: dict | None = None) -> None:
    """RunLog._write 的双写钩子。rec 是已脱敏的事件记录（含 event/spanId/status/durationMs…）。

    otel_attrs：只喂 OTel 的大字段（LLM prompt/completion 全文），按 Langfuse 映射
    落到 generation 的 input/output——不经 jsonl/SSE（体积与「日志不养正文」口径）。"""
    if not _ensure_started():
        return
    with _LOCK:
        uid = str(rec.get("user_id") or "")
        rt = _RUNS.get(run_id)
        if rt is None:
            rt = _RunTrace(run_id, label, trace_name, trace_metadata or {}, user_id=uid)
            _RUNS[run_id] = rt
        elif uid and not rt.user_id:  # 首条记录没带 owner（graph 层先于 stage）→ 补登
            rt.user_id = uid
            rt.root.set_attribute("langfuse.user.id", uid)
        status, span_id, event = rec.get("status"), rec.get("spanId"), str(rec.get("event"))
        # Langfuse 要求 userId 落到 trace 内所有 span 才可过滤聚合（桥内集中补，免 Baggage）
        uid_attr = {"langfuse.user.id": rt.user_id} if rt.user_id else {}
        if not span_id:
            # info 点事件 → 零时长 span，挂在当前最深开口 span（无则 root）下，树形不摊平
            parent = list(rt.open.values())[-1] if rt.open else rt.root
            assert _TRACER is not None
            _TRACER.start_span(event, context=set_span_in_context(parent),
                               attributes={**_scalar_attrs(rec), **uid_attr}).end()
            if event == "graph:end":  # trace 出口：root 观察体 output = 终态
                rt.root.set_attribute("langfuse.observation.output",
                                      str(rec.get("status") or ""))
            return
        if status == "started":
            parent = rt.open.get(rec.get("parentSpanId") or "") or rt.root
            assert _TRACER is not None
            rt.open[span_id] = _TRACER.start_span(event, context=set_span_in_context(parent),
                                                  attributes={**_scalar_attrs(rec), **uid_attr})
            return
        # completed/failed/exited → 闭合 span；悬空闭合（进程重启丢 started）补零时长 span 留痕
        s = rt.open.pop(span_id, None)
        if s is None:
            assert _TRACER is not None
            s = _TRACER.start_span(event, context=set_span_in_context(rt.root))
        for k, v in _scalar_attrs(rec).items():
            s.set_attribute(k, v)
        for k in ("input", "output"):  # generation 全文（_otel 旁路；目前只有 llm:* 会带）
            if otel_attrs and otel_attrs.get(k):
                s.set_attribute(f"langfuse.observation.{k}", str(otel_attrs[k]))
        if rt.user_id:
            s.set_attribute("langfuse.user.id", rt.user_id)
        if status in ("failed", "exited"):
            s.set_status(Status(StatusCode.ERROR, str(rec.get("error") or "")[:300]))
        else:
            s.set_status(Status(StatusCode.OK))
        s.end()


def close_run(run_id: str) -> None:
    """run 收尾：强制闭合残留开口 span（崩溃路径）+ root。调方：stage 结束 / note 条目结束 / _drive。"""
    with _LOCK:
        rt = _RUNS.pop(run_id, None)
    if rt is None:
        return
    for s in rt.open.values():
        try:
            s.end()
        except Exception:
            pass
    try:
        rt.root.end()
    except Exception:
        pass


def flush(timeout_s: float = 5.0) -> None:
    """进程退出前排空（atexit 注册；__stage 子进程短生命周期尤其需要）。"""
    if not _STARTED:
        return
    for prov in (_TRACER_PROVIDER, _METER_PROVIDER):
        if prov is None:
            continue
        try:
            prov.force_flush(timeout_millis=int(timeout_s * 1000))
        except Exception:
            pass  # sink 任何异常都不许影响主管线


def _reset_for_tests() -> None:
    """测试用：关掉 providers 并清全局（InMemory 导出器每用例一换）。"""
    global _STARTED, _TRACER_PROVIDER, _METER_PROVIDER, _TRACER, _RUNTIME_GAUGE_FN
    flush()
    with _LOCK:
        _RUNS.clear()
        _M.clear()
        _RUNTIME_GAUGE_FN = None
        _STARTED = False
        _TRACER_PROVIDER = None
        _METER_PROVIDER = None
        _TRACER = None


# ---------------------------------------------------------------- 指标辅助（全部惰性判空）


def metric_run(status: str) -> None:
    if _M:
        _M["runs"].add(1, {"status": status})


def metric_stage(stage: str, status: str, seconds: float,
                 ok: int, failed: int, skipped: int) -> None:
    if not _M:
        return
    _M["stage_seconds"].record(seconds, {"stage": stage, "status": status})
    for k, n in (("ok", ok), ("failed", failed), ("skipped", skipped)):
        if n:
            _M["stage_entries"].add(n, {"stage": stage, "status": k})


def metric_cb(stage: str) -> None:
    if _M:
        _M["cb"].add(1, {"stage": stage})


def metric_watchdog() -> None:
    """看门狗卡死判定次数（watchdog.trip；告警规则 WatchdogTripped 消费）。"""
    if _M:
        _M["watchdog"].add(1)


def metric_llm(task: str, status: str, seconds: float,
               input_tokens: int = 0, output_tokens: int = 0) -> None:
    if not _M:
        return
    _M["llm_requests"].add(1, {"task": task, "status": status})
    _M["llm_seconds"].record(seconds, {"task": task})
    if input_tokens:
        _M["llm_tokens"].add(input_tokens, {"task": task, "kind": "input"})
    if output_tokens:
        _M["llm_tokens"].add(output_tokens, {"task": task, "kind": "output"})

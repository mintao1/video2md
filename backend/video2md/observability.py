"""OTel SDK 桥：RunLog 事件流 → traces+metrics → 本机 collector（或直连 Langfuse 回退）。

拓扑（2026-09-22 定稿）：
    video2md ──OTLP/HTTP──► otel-collector(:4318) ─┬─ traces → Langfuse Cloud
                                                   └─ metrics → prometheus exporter :8889
出口优先级：OTEL_EXPORTER_OTLP_ENDPOINT（投 collector，无认证）
           > LANGFUSE_PK/SK/HOST 直连（回退，仅 traces——Langfuse 不收 metrics）
           > 都未配置：纯本地 jsonl（logs/run-*.jsonl 仍是唯一真相），零开销。

RunLog API 不动（span/info/current_log），本模块只是 _write 的下游 sink：
- 每 run 一条 trace：trace_id = sha256("video2md-"+run_id)[:32]（确定性——evals 旁路
  backend/scripts/eval_notes.py 据此把分数回挂到原 trace；2026-09-22 前为 serve- 前缀，
  Langfuse 里的历史回放 trace 仍用旧前缀）；root span 随首个事件开立，
  stage → entry → llm:* 逐级父子；__stage 子进程与 api 主进程靠同一 trace_id 拼合。
- LLM span（llm:<task>）按 Langfuse OTel 映射打 gen_ai.* 属性 → 渲染为 generation 计成本
  （模型定价在 Langfuse 项目侧配；provider 不回 usage 时只记字符数，不编造）。
- 指标经 MeterProvider 周期导出，进程重启清零（运行态指标，非账本）。

降级纪律不变：export 异常绝不反压主管线（BatchProcessor 内部吞错），jsonl 先于 sink 落盘；
__stage 短命子进程靠 atexit flush。provider 刻意不注册进 opentelemetry 全局单例
（模块内自引用即可）——测试得以用 InMemory 导出器反复 init/reset，互不串台。

2026-10-02 迁移：provider 管理、RunTrace、flush/drain 已抽到 platform_otel_sdk；
本文件保留视频特有指标定义、LLM gen_ai 映射、graph:end 逻辑。
"""
from __future__ import annotations

import os

import platform_otel_sdk as sdk
from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.trace import Status, StatusCode, set_span_in_context
from platform_otel_sdk import _RUNS, _RunTrace, get_tracer

from .config import ENV

# 直方图桶（秒）：覆盖转录/OCR 的秒级到本地剪辑的近百分钟级
_HISTOGRAM_BOUNDS_S = [1, 5, 15, 60, 300, 900, 1800, 3600, 7200]
_SSE_FIRST_EVENT_BOUNDS_S = [0.1, 0.25, 0.5, 1, 2, 3, 5, 10]  # SLO p95<3s 的判读桶（P1-5）

# rec 里的控制字段（不进 attributes）；usage/model 由 LLM 映射单独处理
_CONTROL_KEYS = {"ts", "run_id", "event", "spanId", "parentSpanId", "status",
                 "durationMs", "error", "force_status", "ts_mono", "usage", "model",
                 "user_id"}

# 指标仪表（init 后非 None；辅助函数全部惰性判空，未配置端点时零开销）
_M: dict = {}  # name → instrument


def _env(key: str, default: str = "") -> str:
    """os.environ 显式设置（含空串=显式关闭）优先；config.ENV（.env/secrets 合并）兜底。"""
    if key in os.environ:
        return os.environ[key]
    return ENV.get(key, default)


def _artifact_mode() -> str:
    """产物模式 tag（dashboard 按 mode 分维度）。动态读 config 属性：测试可 monkeypatch。"""
    from . import config as _c  # 惰性：避免模块级循环；ARTIFACT_MODE 是 import 后常量
    return getattr(_c, "ARTIFACT_MODE", "vault")


def _otel_content_enabled() -> bool:
    """Return whether content-bearing trace fields are allowed for this process."""
    from . import config as _c
    return bool(getattr(_c, "OTEL_INCLUDE_CONTENT", False))


def init(*, endpoint: str = "", headers: dict | None = None,
         span_exporter=None, metric_reader=None) -> bool:
    """起 Tracer/Meter Provider。幂等。无端点且未注入导出器 → 纯本地 jsonl（False）。

    测试注入：span_exporter=InMemorySpanExporter() + metric_reader=InMemoryMetricReader()，
    不经网络（SimpleSpanProcessor 同步导出，断言即所得）。
    """
    views = [View(instrument_name="v2md_stage_seconds",
                  aggregation=ExplicitBucketHistogramAggregation(_HISTOGRAM_BOUNDS_S)),
             View(instrument_name="v2md_llm_seconds",
                  aggregation=ExplicitBucketHistogramAggregation(_HISTOGRAM_BOUNDS_S)),
             View(instrument_name="v2md_sse_first_event_seconds",
                  aggregation=ExplicitBucketHistogramAggregation(_SSE_FIRST_EVENT_BOUNDS_S))]
    return sdk.init_otel(
        service_name="video2md-serve",
        service_namespace="video2md",
        hash_prefix="video2md-",
        trace_tags=["video2md", f"mode:{_artifact_mode()}"],  # mode tag 迁移漏带，回补（06 篇口径）
        deployment_env=_env("V2MD_ENV", "development"),
        endpoint=endpoint,
        headers=headers,
        span_exporter=span_exporter,
        metric_reader=metric_reader,
        views=views,
        make_instruments=_make_instruments,
    )


def _make_instruments(meter) -> None:
    _M["runs"] = meter.create_counter("v2md_runs", description="完成的 run（按终态）")
    _M["cb"] = meter.create_counter("v2md_circuit_breaker_trips", description="熔断次数（按阶段）")
    _M["watchdog"] = meter.create_counter("v2md_watchdog_trips", description="看门狗卡死判定次数")
    _M["stage_entries"] = meter.create_counter("v2md_stage_entries",
                                               description="阶段条目结果（ok/failed/skipped）")
    _M["llm_requests"] = meter.create_counter("v2md_llm_requests", description="LLM 调用（按任务/终态）")
    _M["llm_tokens"] = meter.create_counter("v2md_llm_tokens", description="LLM token（按任务/in|out）")
    _M["stage_seconds"] = meter.create_histogram("v2md_stage_seconds", description="阶段耗时（秒）")
    _M["sse_first"] = meter.create_histogram(
        "v2md_sse_first_event_seconds",
        description="SSE connected→首个业务事件延迟（秒；SLO p95<3s，P1-5）")
    _M["llm_seconds"] = meter.create_histogram("v2md_llm_seconds", description="LLM 调用耗时（秒）")

    def _queued(_options):
        from platform_otel_sdk._otel import _get_runtime_gauge_fn
        fn = _get_runtime_gauge_fn()
        yield Observation((fn() or {}).get("queued", 0) if fn else 0)

    def _active(_options):
        from platform_otel_sdk._otel import _get_runtime_gauge_fn
        fn = _get_runtime_gauge_fn()
        yield Observation((fn() or {}).get("active", 0) if fn else 0)

    _M["queue_gauge"] = meter.create_observable_gauge("v2md_queue_queued", [_queued],
                                                      description="排队中的 run")
    _M["active_gauge"] = meter.create_observable_gauge("v2md_run_active", [_active],
                                                       description="运行中 run（0/1）")


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


def _ensure_started() -> bool:
    """惰性初始化：首次调用时触发 SDK init（init_otel 幂等，重复调用零开销）。"""
    return init()


def record(rec: dict, *, run_id: str, label: str, trace_name: str = "",
           trace_metadata: dict | None = None,
           otel_attrs: dict | None = None) -> None:
    """RunLog._write 的双写钩子。rec 是已脱敏的事件记录（含 event/spanId/status/durationMs…）。

    otel_attrs：仅在开发配置显式允许时喂 OTel 的大字段。生产通过
    V2MD_OTEL_INCLUDE_CONTENT=off 阻断 LLM prompt/completion 正文，只保留 usage/长度
    元数据；正文不经 collector/Langfuse，也不落 jsonl/SSE。

    注意：video2md 自有 record（而非 SDK 的 record）：
    - LLM gen_ai 映射、user_id 追溯传播、graph:end 根输出写入——均视频特有逻辑。
    """
    if not _ensure_started():
        return
    tracer = get_tracer()
    if tracer is None:
        return

    uid = str(rec.get("user_id") or "")
    status = rec.get("status")
    span_id = rec.get("spanId")
    event = str(rec.get("event") or "")

    # 确保 run 存在
    rt = _RUNS.get(run_id)
    # URLs can contain signed query parameters or local tenant paths.  Treat
    # them as content too: production keeps the trace identity/metrics but
    # drops the root input unless explicit local debugging opted in.
    trace_content = _otel_content_enabled()
    if rt is None:
        rt = _RunTrace(run_id, label, trace_name if trace_content else "",
                       trace_metadata if trace_content else {}, user_id=uid)
        _RUNS[run_id] = rt
    elif uid and not rt.user_id:
        rt.user_id = uid
        rt.root.set_attribute("langfuse.user.id", uid)

    uid_attr = {"langfuse.user.id": rt.user_id} if rt.user_id else {}

    if not span_id:
        # info 点事件 → 零时长 span，挂在当前最深开口 span（无则 root）下
        parent = list(rt.open.values())[-1] if rt.open else rt.root
        tracer.start_span(event, context=set_span_in_context(parent),
                          attributes={**_scalar_attrs(rec), **uid_attr}).end()
        if event == "graph:end":
            rt.root.set_attribute("langfuse.observation.output",
                                  str(rec.get("status") or ""))
        return

    if status == "started":
        parent = rt.open.get(rec.get("parentSpanId") or "") or rt.root
        rt.open[span_id] = tracer.start_span(
            event, context=set_span_in_context(parent),
            attributes={**_scalar_attrs(rec), **uid_attr})
        return

    # completed/failed/exited → 闭合 span；悬空闭合补零时长 span 留痕
    s = rt.open.pop(span_id, None)
    if s is None:
        s = tracer.start_span(event, context=set_span_in_context(rt.root))
    for k, v in _scalar_attrs(rec).items():
        s.set_attribute(k, v)
    for k in ("input", "output"):
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
    """run 收尾：强制闭合残留开口 span（崩溃路径）+ root。"""
    sdk.close_run(run_id)


def flush(timeout_s: float = 5.0) -> None:
    """进程退出前排空（atexit 注册；__stage 子进程短生命周期尤其需要）。"""
    sdk.flush(timeout_s)


def drain(settle_s: float = 6.0) -> None:
    """双次 flush + settle 延迟，确保 BatchSpanProcessor 根 span 送达。"""
    sdk.drain(settle_s)


def register_runtime_gauges(fn) -> None:
    """api 注册运行态表盘的取数回调：fn() → {"queued": int, "active": int}。"""
    sdk.register_runtime_gauges(fn)


def _reset_for_tests() -> None:
    """测试用：关掉 providers 并清全局（InMemory 导出器每用例一换）。"""
    global _M
    sdk.reset_for_tests()
    _M.clear()


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


def metric_sse_first_event(seconds: float) -> None:
    """SSL/SLI：connected→首个业务事件的延迟（08 篇 SLO 草案 p95<3s 的测量源，P1-5）。"""
    if _M:
        _M["sse_first"].record(seconds)


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

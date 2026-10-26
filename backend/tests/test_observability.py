#!/usr/bin/env python3
"""test_observability.py — OTel SDK 桥离线单测。

覆盖：无端点零开销 / span 树父子与属性 / LLM gen_ai 映射 / traceId 确定性 /
熔断+指标仪表（InMemoryMetricReader）/ 运行态 gauge / deadman ping 三态。

运行：python3 test_observability.py（InMemory 导出器 + MockTransport，无网络）。
"""
import hashlib
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/（包根）

# 隔离：日志进临时目录；显式空串关掉任何真实端点（防开发机 secrets 文件串台——
# os.environ 显式设置优先于 config.ENV，见 observability._env）
os.environ["V2PDF_LOG_DIR"] = tempfile.mkdtemp(prefix="v2pdf-test-logs-")
for k in ("OTEL_EXPORTER_OTLP_ENDPOINT", "LANGFUSE_PK", "LANGFUSE_SK", "LANGFUSE_HOST"):
    os.environ[k] = ""

from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from platform_otel_sdk import _otel as _sdk  # _TRACER_PROVIDER 随 otel 迁移入 SDK（033ae72）

from video2pdf import deadman, observability
from video2pdf.util import RunLog


def _metrics_map(reader: InMemoryMetricReader) -> dict:
    """name → {attrs_tuple: value}，直方图取 sum。"""
    out: dict = {}
    data = reader.get_metrics_data()
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                pts = {}
                for dp in m.data.data_points:
                    val = getattr(dp, "sum", None) or getattr(dp, "value", None) or 0
                    pts[tuple(sorted(dp.attributes.items()))] = val
                    if hasattr(dp, "bucket_counts"):  # 直方图：顺带记桶边界供断言
                        pts["__bounds__"] = tuple(dp.explicit_bounds)
                out[m.name] = pts
    return out


# ---- 无端点：纯本地 jsonl，零开销 --------------------------------------------
observability._reset_for_tests()
assert observability.init() is False
log = RunLog("t", run_id="offcase")
with log.span("stage:download"):
    log.info("artifacts", stage="download", n=1)
observability.close_run("offcase")
observability.flush()  # 不炸即过
assert observability._M == {} and _sdk._TRACER_PROVIDER is None


# ---- InMemory：span 树 / 属性 / gen_ai 映射 ----------------------------------
observability._reset_for_tests()
spans = InMemorySpanExporter()
reader = InMemoryMetricReader()
assert observability.init(span_exporter=spans, metric_reader=reader) is True

log = RunLog("t", run_id="r1", trace_name="http://video", trace_meta={"stage": "download"},
             user_id="uid_alice")
with log.span("stage:download"):
    with log.span("entry 1/2 download"):
        log.info("artifacts", stage="download", duration_s=42)
    with log.span("llm:organize") as sp:
        sp["model"] = "Kimi-K3"
        sp["usage"] = {"prompt_tokens": 11, "completion_tokens": 7}
        sp["ttftMs"] = 120
        sp["_otel"] = {"input": '[{"role":"user","content":"转录全文"}]', "output": "正文"}
log.info("graph:end", status="done")  # trace 出口：root observation output
try:
    with log.span("stage:frames"):
        raise RuntimeError("ffmpeg 炸了")
except RuntimeError:
    pass
observability.close_run("r1")

tree = {s.name: s for s in spans.get_finished_spans()}
root, stage, entry = tree["video2pdf-serve-run"], tree["stage:download"], tree["entry 1/2 download"]
# trace 名常量化（低基数原则：名字是 API，URL 是数据进 input）
# 现状命名=f"{service_name}-run"（otel 迁移后 SDK 生成；465b53c 兼容锚）
assert root.attributes["langfuse.trace.name"] == "video2pdf-serve-run"
assert root.attributes["run_id"] == "r1"
# Langfuse OTel 映射：metadata 带 langfuse.trace.metadata.* 前缀才可过滤
assert root.attributes["langfuse.trace.metadata.stage"] == "download"
# userId 映射 + 全 span 传播（官方：过滤/聚合的前提）；tags 给 dashboard 分维度
assert root.attributes["langfuse.user.id"] == "uid_alice"
assert stage.attributes["langfuse.user.id"] == "uid_alice"
assert "video2pdf" in root.attributes["langfuse.trace.tags"]
assert any(t.startswith("mode:") for t in root.attributes["langfuse.trace.tags"])
# trace 输入/输出（root 观察体语义等价物：v4 废弃 trace 级 input/output）
assert root.attributes["langfuse.observation.input"] == "http://video"
assert root.attributes["langfuse.observation.output"] == "done"
assert stage.parent.span_id == root.context.span_id
assert entry.parent.span_id == stage.context.span_id
info = tree["artifacts"]
assert info.parent.span_id == entry.context.span_id  # info 挂当前最深开口 span
assert info.attributes["duration_s"] == 42

gen = tree["llm:organize"]
assert gen.attributes["langfuse.observation.type"] == "generation"
assert gen.attributes["llm.task"] == "organize"
assert gen.attributes["gen_ai.request.model"] == "Kimi-K3"
assert gen.attributes["gen_ai.usage.input_tokens"] == 11
assert gen.attributes["gen_ai.usage.output_tokens"] == 7
assert gen.attributes["ttftMs"] == 120
assert gen.attributes["durationMs"] >= 0
# generation 全文：_otel 旁路 → langfuse.observation.input/output
assert "转录全文" in gen.attributes["langfuse.observation.input"]
assert gen.attributes["langfuse.observation.output"] == "正文"
assert "_otel" not in log.spans[-2], "span_log 摘要不得养 prompt 全文副本"

from opentelemetry.trace import StatusCode

assert tree["stage:frames"].status.status_code == StatusCode.ERROR
assert "ffmpeg" in tree["stage:frames"].attributes["error"]

# traceId 确定性：全树同 trace；与 evals.trace_id_of 同约定（evals 旁路回挂的依据）
want_tid = int(hashlib.sha256(b"video2md-r1").hexdigest()[:32], 16)
assert all(s.context.trace_id == want_tid for s in tree.values())
# 同 run_id 换进程（新 RunLog/新 _RunTrace）仍同 trace；不同 run_id 不同 trace
log2 = RunLog("t", run_id="r1", trace_name="http://video")
with log2.span("stage:ocr"):
    pass
observability.close_run("r1")
tree2 = {s.name: s for s in spans.get_finished_spans() if s.name == "stage:ocr"}
assert tree2 and tree2["stage:ocr"].context.trace_id == want_tid
log3 = RunLog("t", run_id="r2")
with log3.span("stage:ocr"):
    pass
observability.close_run("r2")
other = [s for s in spans.get_finished_spans()
         if s.name == "stage:ocr" and s.context.trace_id != want_tid]
assert other, "不同 run_id 应落在不同 trace"


# ---- 指标仪表（counter/histogram/gauge）--------------------------------------
observability.metric_run("done")
observability.metric_run("partial")
observability.metric_stage("download", "completed", 12.5, ok=3, failed=1, skipped=0)
observability.metric_cb("transcribe")
observability.metric_llm("organize", "ok", 30.0, input_tokens=100, output_tokens=2000)
observability.register_runtime_gauges(lambda: {"queued": 2, "active": 1})
reader.collect()

m = _metrics_map(reader)
assert m["v2pdf_runs"][(("status", "done"),)] == 1
assert m["v2pdf_runs"][(("status", "partial"),)] == 1
st = m["v2pdf_stage_seconds"]
assert st[(("stage", "download"), ("status", "completed"))] == 12.5
assert st["__bounds__"] == tuple(observability._HISTOGRAM_BOUNDS_S)  # 显式桶生效
assert m["v2pdf_stage_entries"][(("stage", "download"), ("status", "ok"))] == 3
assert m["v2pdf_stage_entries"][(("stage", "download"), ("status", "failed"))] == 1
assert m["v2pdf_circuit_breaker_trips"][(("stage", "transcribe"),)] == 1
assert m["v2pdf_llm_requests"][(("status", "ok"), ("task", "organize"))] == 1
assert m["v2pdf_llm_tokens"][(("kind", "input"), ("task", "organize"))] == 100
assert m["v2pdf_llm_tokens"][(("kind", "output"), ("task", "organize"))] == 2000
assert m["v2pdf_llm_seconds"][(("task", "organize"),)] == 30.0
assert m["v2pdf_queue_queued"][()] == 2 and m["v2pdf_run_active"][()] == 1

# 未 init 时指标全 no-op（纯本地模式不炸）
observability._reset_for_tests()
observability.metric_run("done")
observability.metric_llm("t", "failed", 1.0)


# ---- deadman ping 三态 --------------------------------------------------------
os.environ["V2PDF_DEADMAN_PING_URL"] = "https://hc.test/abcd"
hits = []

def fake_get(url, timeout=0, **_kw):
    hits.append(url)

    class _R:
        status_code = 200
    return _R()

observability._reset_for_tests()
spans2 = InMemorySpanExporter()
observability.init(span_exporter=spans2, metric_reader=InMemoryMetricReader())
with mock.patch.object(deadman.httpx, "get", fake_get):
    deadman.ping("start", run_id="r9", trace_name="http://v")
    deadman.ping("ok", run_id="r9", trace_name="http://v")
    deadman.ping("fail")  # 无 run_id：只 ping 不打 trace
assert hits == ["https://hc.test/abcd/start", "https://hc.test/abcd",
                "https://hc.test/abcd/fail"], hits
dm = [s for s in spans2.get_finished_spans() if s.name == "deadman"]
assert len(dm) == 2 and dm[0].attributes["state"] == "start" and dm[1].attributes["state"] == "ok"

# 未配 URL = 完全 no-op
os.environ["V2PDF_DEADMAN_PING_URL"] = ""
hits.clear()
with mock.patch.object(deadman.httpx, "get", fake_get):
    deadman.ping("ok", run_id="r9")
assert hits == []

# ping 异常静默（监控通道不许炸主管线）
with mock.patch.object(deadman.httpx, "get",
                       mock.Mock(side_effect=deadman.httpx.ConnectError("down"))):
    os.environ["V2PDF_DEADMAN_PING_URL"] = "https://hc.test/x"
    deadman.ping("ok")
os.environ.pop("V2PDF_DEADMAN_PING_URL", None)

print("✅ test_observability.py 全部通过")

#!/usr/bin/env python3
"""test_otel_replay.py — 历史回放离线单测（InMemory 导出器，无网络）。

覆盖：span 树父子链 / traceId 确定性（sha256("video2md-"+run_id) 约定）/
gen_ai 属性映射（复用 _scalar_attrs）/ info 点挂最深开口 span /
悬空 started = 崩溃点（ERROR 强制闭合）/ 悬空 completed 补零时长 /
时间戳 ns 还原 / 乱序输入稳定排序 / runs.json 联表 root 名 / 多 run 分 trace。

运行：python3 test_otel_replay.py
"""
import hashlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "scripts"))

# 隔离：日志进临时目录；显式空串关掉任何真实端点（防开发机 secrets 文件串台）
import os

os.environ["V2MD_LOG_DIR"] = tempfile.mkdtemp(prefix="v2md-replay-test-logs-")
for k in ("OTEL_EXPORTER_OTLP_ENDPOINT", "LANGFUSE_PK", "LANGFUSE_SK", "LANGFUSE_HOST"):
    os.environ[k] = ""

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from otel_replay import _ns, group_by_run, replay_run


def _provider():
    spans = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(spans))
    return tp, tp.get_tracer("video2md"), spans


# ---- 合成一个 run 的事件流（含 info 点 / llm usage / 悬空 started 崩溃点）----
T = "2026-09-21T10:00:"
RECS = [
    {"ts": f"{T}00", "run_id": "r1", "spanId": "aaa", "event": "stage:download", "status": "started"},
    {"ts": f"{T}05", "run_id": "r1", "spanId": "bbb", "event": "entry 1/2 download e1",
     "status": "started", "parentSpanId": "aaa"},
    {"ts": f"{T}06", "run_id": "r1", "event": "artifacts", "stage": "download", "n": 3},
    {"ts": f"{T}07", "run_id": "r1", "spanId": "bbb", "event": "entry 1/2 download e1",
     "status": "completed", "durationMs": 2000},
    {"ts": f"{T}08", "run_id": "r1", "spanId": "ccc", "event": "llm:organize",
     "status": "started", "parentSpanId": "aaa"},
    {"ts": f"{T}09", "run_id": "r1", "spanId": "ccc", "event": "llm:organize",
     "status": "completed", "durationMs": 1000, "model": "Kimi-K3",
     "usage": {"prompt_tokens": 5, "completion_tokens": 6}, "ttftMs": 120},
    {"ts": f"{T}10", "run_id": "r1", "spanId": "aaa", "event": "stage:download",
     "status": "completed", "durationMs": 10000, "ok": 3, "failed": 0},
    {"ts": f"{T}11", "run_id": "r1", "spanId": "ddd", "event": "stage:ocr", "status": "started"},
]

tp, tracer, spans = _provider()
meta = {"r1": {"name": "http://video", "status": "done"}}
stats = replay_run(tracer, "r1", RECS, meta["r1"], source="run-x.jsonl")
tp.shutdown()

tree = {s.name: s for s in spans.get_finished_spans()}
root, stage, entry, gen = tree["http://video"], tree["stage:download"], \
    tree["entry 1/2 download e1"], tree["llm:organize"]

# traceId 确定性（与在线/evals 同约定）
want_tid = int(hashlib.sha256(b"video2md-r1").hexdigest()[:32], 16)
assert all(s.context.trace_id == want_tid for s in tree.values())
assert stats["trace_id"] == f"{want_tid:032x}"

# root：联表名/回放留痕/时间戳包络
assert root.attributes["langfuse.trace.name"] == "http://video"
assert root.attributes["run_id"] == "r1" and root.attributes["replayed"] is True
assert root.attributes["run_status"] == "done"
assert root.attributes["replay.source"] == "run-x.jsonl"
assert root.start_time == _ns(f"{T}00") and root.end_time == _ns(f"{T}11")

# 父子链：stage 挂 root，entry/llm 挂 stage
assert stage.parent.span_id == root.context.span_id
assert entry.parent.span_id == stage.context.span_id
assert gen.parent.span_id == stage.context.span_id

# info 点：挂当前最深开口 span（bbb），零时长
info = tree["artifacts"]
assert info.parent.span_id == entry.context.span_id
assert info.start_time == info.end_time == _ns(f"{T}06")
assert info.attributes["n"] == 3

# llm span 的 gen_ai 映射（closed 行携带 usage/model）
assert gen.attributes["langfuse.observation.type"] == "generation"
assert gen.attributes["llm.task"] == "organize"
assert gen.attributes["gen_ai.request.model"] == "Kimi-K3"
assert gen.attributes["gen_ai.usage.input_tokens"] == 5
assert gen.attributes["gen_ai.usage.output_tokens"] == 6
assert gen.attributes["durationMs"] == 1000.0
assert gen.status.status_code == StatusCode.OK

# 事件行时间戳 → span 起止（秒级精度，本地时钟还原）
assert stage.start_time == _ns(f"{T}00") and stage.end_time == _ns(f"{T}10")
assert entry.start_time == _ns(f"{T}05") and entry.end_time == _ns(f"{T}07")

# 悬空 started（ddd，进程崩溃点）：ERROR + 强制闭合到 run 末尾
dang = tree["stage:ocr"]
assert dang.status.status_code == StatusCode.ERROR
assert dang.attributes["replay.dangling"] is True
assert dang.end_time == _ns(f"{T}11")
assert stats["dangling"] == 1 and stats["info"] == 1
assert stats["spans"] == len(RECS)

# failed 闭合：ERROR + error 属性
tp, tracer, spans = _provider()
fail_recs = [
    {"ts": f"{T}00", "run_id": "r2", "spanId": "f1", "event": "stage:transcribe", "status": "started"},
    {"ts": f"{T}30", "run_id": "r2", "spanId": "f1", "event": "stage:transcribe",
     "status": "failed", "durationMs": 30000, "error": "ffmpeg 炸了"},
]
replay_run(tracer, "r2", fail_recs, {})
tp.shutdown()
fs = {s.name: s for s in spans.get_finished_spans()}["stage:transcribe"]
assert fs.status.status_code == StatusCode.ERROR
assert "ffmpeg" in fs.attributes["error"]
assert fs.end_time == _ns(f"{T}30")

# 悬空闭合（completed 无 started，进程重启丢 started）：补零时长 span 挂 root 留痕
tp, tracer, spans = _provider()
replay_run(tracer, "r3", [{"ts": f"{T}00", "run_id": "r3", "spanId": "z9",
                           "event": "entry 9/9 ocr x", "status": "failed", "error": "boom"}], {})
tp.shutdown()
zs = {s.name: s for s in spans.get_finished_spans()}
zs.pop("r3")  # root（无联表名 → 回退 run_id）
synth = zs["entry 9/9 ocr x"]
assert synth.parent.span_id is not None
assert synth.start_time == synth.end_time == _ns(f"{T}00")
assert synth.status.status_code == StatusCode.ERROR
root_r3 = {s.name: s for s in spans.get_finished_spans()}["r3"]
assert root_r3.attributes["replayed"] is True and "run_status" not in root_r3.attributes

# 乱序输入：group_by_run 稳定排序恢复事件序（同秒保序）
shuffled = list(reversed(RECS))
runs = group_by_run(shuffled)
assert [r["ts"] for r in runs["r1"]] == sorted(r["ts"] for r in RECS)
tp, tracer, spans = _provider()
replay_run(tracer, "r1", runs["r1"], meta["r1"])
tp.shutdown()
tree2 = {s.name: s for s in spans.get_finished_spans()}
assert tree2["llm:organize"].parent.span_id == tree2["stage:download"].context.span_id
assert tree2["artifacts"].parent.span_id == tree2["entry 1/2 download e1"].context.span_id

# 多 run 分 trace；group 顺序保持首现序
runs = group_by_run(RECS + fail_recs)
assert list(runs.keys()) == ["r1", "r2"]
tp, tracer, spans = _provider()
for rid, rs in runs.items():
    replay_run(tracer, rid, rs, {})
tp.shutdown()
tids = {s.context.trace_id for s in spans.get_finished_spans()}
assert len(tids) == 2 and want_tid in tids

# 空 recs → None（调用方跳过）
assert replay_run(tracer, "r0", [], {}) is None

print("✅ test_otel_replay.py 全部通过")

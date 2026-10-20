#!/usr/bin/env python3
"""test_graph_obs.py — LangGraph 图编排层自动插桩离线单测。

覆盖：graph:* 事件序列（节点序 / 路由决策 / note 回环游标）/ llm:select_frames
缝隙修复（_STAGE_LOGS 注册）/ fail 路由可见 / OTel InMemory（确定性 trace 归并 +
点事件纪律 + 属性）/ 插桩兜底（info 炸了不许影响主管线）。

运行：python3 test_graph_obs.py（阶段函数/LLM 打桩、state/runs/日志隔离到 tmp；
langgraph 1.2.12 字段口径，升级漂移在此先红）。
"""
import hashlib
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

os.environ["LANGFUSE_PK"] = os.environ["LANGFUSE_SK"] = ""  # 观测 sink 强制静默
os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/（包根）

from video2md import graph, graph_obs, observability, runs, util
from video2md.util import RunLog

_TMP = Path(tempfile.mkdtemp(prefix="video2md-test-graphobs-"))
_ORIG_STATE_FILE = util.STATE_FILE
_ORIG_LOG_DIR = util.LOG_DIR
_ORIG_RUNS_FILE = runs.RUNS_FILE
_ORIG_RUNS_LOCKFILE = runs._RUNS_LOCKFILE
util.STATE_FILE = _TMP / "state.json"
util.LOG_DIR = _TMP / "logs"
runs.RUNS_FILE = _TMP / "runs.json"
runs._RUNS_LOCKFILE = _TMP / "runs.lock"
observability.flush = lambda: None  # 离线：不碰任何真实端点


# ---- 打桩：阶段函数 / LLM（对齐 test_graph.py 的注入 seam）--------------------
def _fake_stage(stage, mutate):
    def fn(run, entry, art, tmp, args):
        if mutate:
            mutate(run, entry, art, args)
    return fn


def _happy(stage):
    def mut(run, entry, art, args):
        art["fp"] = art.get("fp") or f"fp-{run['entries'].index(entry) + 1}"
        if stage == "download":
            art.update(video="/tmp/fake.mp4", duration=120.0)
        elif stage == "transcribe":
            art.update(segs=[{"start": 0, "end": 1, "text": "你好"}], transcript="[00:00] 你好" * 50)
        elif stage == "frames":
            art["candidates"] = [[0.5, "frames/f1.jpg"]]
        elif stage == "ocr":
            art.update(ocr_raw=[[0.5, "题", 0.9, None]], ocr_items=[[0.5, "题", 0.9]],
                       cand_manifest="f1.jpg [00:00] OCR: 题", transcript=art["transcript"])
        elif stage == "select":
            art.update(selected=[{"name": "f1.jpg", "t": 0.5}], manifest="f1.jpg [00:00]",
                       published=[["f1.jpg", 0.5]])
        elif stage == "clips":
            art.update(clips={}, clips_done=True)
    return _fake_stage(stage, mut)


def _fake_note(run, entry, art, tmp, args):
    idx = run["entries"].index(entry) + 1
    art.update(note=f"/vault/笔记{idx}.md", title=f"标题{idx}",
               fp=art.get("fp") or f"fp-{idx}", transcript_path="/vault/t.md")


def _fake_select(tr, cand, total):
    # 缝隙修复断言：select_frames_llm 节点内 current_log() 应取到 graph RunLog
    # （对齐 llm.llm_task 的挂 span 路径 llm.py:218-222——修复前这里拿不到 log）
    log = util.current_log()
    assert log is not None and log.label == "graph", \
        f"select_frames 内 current_log 应为 graph RunLog，实得 {log and log.label}"
    with log.span("llm:select_frames"):
        pass
    return {"frames": [{"name": "f1.jpg", "caption": "c"}]}, 1, ""


def _install_fakes():
    for s in ("download", "transcribe", "frames", "ocr", "select", "clips"):
        runs._STAGE_FNS[s] = _happy(s)
    runs._STAGE_FNS["note"] = _fake_note
    graph.llm_task = lambda task, **kw: {"organize": "正文" * 100,
                                         "review": "校对" * 100,
                                         "metadata": '{"title": "题"}'}[task]
    graph.select_frames = _fake_select


def _fresh():
    for p in ("state.json", "runs.json", "runs.lock"):
        (_TMP / p).unlink(missing_ok=True)
    _install_fakes()


def _events(run_id: str, prefix: str = "graph:") -> list[dict]:
    """从 jsonl 里筛本 run 的事件（按前缀）。"""
    path = util.LOG_DIR / f"run-{date.today().isoformat()}.jsonl"
    out = []
    for line in path.read_text().splitlines():
        rec = json.loads(line)
        if rec.get("run_id") == run_id and str(rec.get("event", "")).startswith(prefix):
            out.append(rec)
    return out


# ---- 1. 单条目 happy path：事件序列 / 路由决策 / state 增量 ---------------------
_fresh()
final = graph.run_graph("/tmp/fake.mp4", checkpointer=None)
assert final["status"] == "done", final.get("errors")
ev = _events(final["run_id"])
assert ev[0]["event"] == "graph:start" and ev[0]["url"] == "/tmp/fake.mp4", ev[0]
assert ev[-1]["event"] == "graph:end" and ev[-1]["status"] == "done", ev[-1]
nodes = [e["node"] for e in ev if e["event"] == "graph:node"]
assert nodes == ["resolve", "download", "transcribe", "frames", "ocr",
                 "select_frames_llm", "select", "clips", "note_entry", "finalize"], nodes
# 条件边路由决策（select_frames_llm/finalize 无条件边 → 无 route 事件，dict 精确等值钉住）
routes = {e["frm"]: e["decision"] for e in ev if e["event"] == "graph:route"}
assert routes == {"resolve": "ok", "download": "ok", "transcribe": "ok", "frames": "ok",
                  "ocr": "ok", "select": "ok", "clips": "ok", "note_entry": "done"}, routes
states = {e["node"]: e for e in ev if e["event"] == "graph:state"}
assert all("keys" in e and "step" in e and "durMs" in e for e in states.values())
assert states["note_entry"]["entry_idx"] == 2, states["note_entry"]       # 回环游标
assert states["finalize"]["status"] == "done"
steps = [e["step"] for e in ev if e["event"] == "graph:node"]
assert steps == list(range(1, 11)), steps                                 # superstep 1..10
# 缝隙修复：llm:select_frames span 落进 jsonl（修复前该调用无活跃 RunLog）
llm = _events(final["run_id"], prefix="llm:select_frames")
assert [e["status"] for e in llm] == ["started", "completed"], llm
print("✅ 1. happy path：事件序列 / 路由决策 / state 增量 / llm:select_frames 缝隙修复")


# ---- 2. 多条目回环：note_entry 迭代 / loop→done 路由轨迹 -----------------------
_fresh()
runs.resolve_url = lambda url: [  # 假合集展开（剥网络）
    {"url": "https://bilibili.com/video/BV1aaa", "id": "1", "playlist_index": 1},
    {"url": "https://bilibili.com/video/BV2bbb", "id": "2", "playlist_index": 2}]
final = graph.run_graph("https://bilibili.com/video/BV1aaa", checkpointer=None)
assert final["status"] == "done", final.get("errors")
ev = _events(final["run_id"])
note_states = [e for e in ev if e["event"] == "graph:state" and e.get("node") == "note_entry"]
assert [e["entry_idx"] for e in note_states] == [2, 3], note_states       # 逐条目游标推进
note_routes = [e for e in ev if e["event"] == "graph:route" and e.get("frm") == "note_entry"]
assert [e["decision"] for e in note_routes] == ["loop", "done"], note_routes
note_nodes = [e for e in ev if e["event"] == "graph:node" and e.get("node") == "note_entry"]
assert len(note_nodes) == 2 and note_nodes[0]["step"] < note_nodes[1]["step"]
print("✅ 2. 多条目回环：note_entry 逐条目迭代（entry_idx 游标 + loop/done 路由）")


# ---- 3. 阶段失败 → fail 路由：错误走向与终态可见 --------------------------------
_fresh()
def _boom(run, entry, art, tmp, args):
    raise SystemExit("模拟全局故障（token 过期）")
runs._STAGE_FNS["download"] = _boom
final = graph.run_graph("/tmp/fake.mp4", checkpointer=None)
assert final["status"] == "error", final.get("status")
ev = _events(final["run_id"])
nodes = [e["node"] for e in ev if e["event"] == "graph:node"]
assert nodes[-1] == "fail", nodes                                        # 显式失败终节点可见
routes = {e["frm"]: e["decision"] for e in ev if e["event"] == "graph:route"}
assert routes.get("download") == "fail", routes                          # 路由走向留痕
dl_state = [e for e in ev if e["event"] == "graph:state" and e.get("node") == "download"][0]
assert dl_state["status"] == "error" and dl_state["errors_n"] >= 1, dl_state
assert ev[-1]["event"] == "graph:end" and ev[-1]["status"] == "error"
print("✅ 3. 阶段失败 → fail 路由：decision=fail / status=error / 终节点可见")


# ---- 4. OTel InMemory：确定性 trace 归并 + 点事件纪律 + 属性 -------------------
observability._reset_for_tests()
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

spans, reader = InMemorySpanExporter(), InMemoryMetricReader()
assert observability.init(span_exporter=spans, metric_reader=reader) is True
_fresh()
final = graph.run_graph("/tmp/fake.mp4", checkpointer=None)
assert final["status"] == "done", final.get("errors")
trace_id = int(hashlib.sha256(f"video2md-{final['run_id']}".encode()).hexdigest()[:32], 16)
finished = spans.get_finished_spans()
g = [s for s in finished if s.name.startswith("graph:")]
assert g, "graph:* spans 缺席"
assert all(s.context.trace_id == trace_id for s in g), "graph:* 未归并到确定性 trace"
assert all(s.parent is not None for s in g), "点事件应挂 root/深开口 span（不摊平）"
assert all(s.end_time - s.start_time < 1_000_000 for s in g), "info 点事件应为零时长"  # <1ms
node_attrs = {s.attributes["node"]: s.attributes for s in g if s.name == "graph:node"}
assert node_attrs["note_entry"]["step"] >= 1
route_attrs = [s.attributes for s in g if s.name == "graph:route"]
assert any(a.get("decision") == "ok" and a.get("frm") == "resolve" for a in route_attrs)
llm_sel = [s for s in finished if s.name == "llm:select_frames"]
assert llm_sel and all(s.context.trace_id == trace_id for s in llm_sel), \
    "llm:select_frames 应补齐且落同一条 trace"
observability._reset_for_tests()
print("✅ 4. OTel：确定性 trace 归并 / 零时长点事件 / 属性齐 / llm:select_frames 同 trace")


# ---- 5. 插桩兜底：handler 内部炸了也不许影响主管线 ------------------------------
class _BrokenInfo(RunLog):
    def info(self, event, **fields):
        raise RuntimeError("插桩炸了也不许影响主管线")
_orig = graph_obs.RunLog
graph_obs.RunLog = _BrokenInfo
try:
    _fresh()
    final = graph.run_graph("/tmp/fake.mp4", checkpointer=None)
    # 不止 done：连 errors 都不许有——注册先于事件写，select_frames 缝隙不被连累
    assert final["status"] == "done" and not final.get("errors"), final.get("errors")
finally:
    graph_obs.RunLog = _orig

util.STATE_FILE = _ORIG_STATE_FILE
util.LOG_DIR = _ORIG_LOG_DIR
runs.RUNS_FILE = _ORIG_RUNS_FILE
runs._RUNS_LOCKFILE = _ORIG_RUNS_LOCKFILE
print("✅ 5. 兜底：graph 事件写失败被吞，run 照常 done 无留痕（注册先于写事件）")

print("\n✅ test_graph_obs.py 全部通过")

#!/usr/bin/env python3
"""test_graph.py — LangGraph 编排离线单测：节点序列 / 失败路由 / 幂等短路 / 兜底。

运行：python3 test_graph.py（全离线：阶段函数打桩、LLM 打桩、state/runs 文件隔离到 tmp）。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["LANGFUSE_PK"] = os.environ["LANGFUSE_SK"] = ""  # 观测 sink 强制静默

sys.path.insert(0, str(Path(__file__).parent))

from video2md import graph, runs, util
from video2md import observability

_TMP = Path(tempfile.mkdtemp(prefix="video2md-test-graph-"))

# ---- 隔离文件 SSOT：state.json / runs.json / runs.lock / 日志目录全部进 tmp ----
util.STATE_FILE = _TMP / "state.json"
util.LOG_DIR = _TMP / "logs"
runs.RUNS_FILE = _TMP / "runs.json"
runs._RUNS_LOCKFILE = _TMP / "runs.lock"
observability.flush = lambda: None  # 离线：不碰 Langfuse


# ---- 打桩：阶段函数 / LLM ----------------------------------------------------
CALLS: list[str] = []  # 执行轨迹（节点顺序断言用）


def _fake_stage(stage, mutate):
    def fn(run, entry, art, tmp, args):
        CALLS.append(f"{stage}:{run['entries'].index(entry) + 1}")
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
    CALLS.append(f"note:{idx}")
    assert args.get("body"), "note args 缺 body"
    art.update(note=f"/vault/笔记{idx}.md", title=f"标题{idx}",
               fp=art.get("fp") or f"fp-{idx}", transcript_path="/vault/t.md")


def _install_fakes():
    CALLS.clear()
    for s in ("download", "transcribe", "frames", "ocr", "select", "clips"):
        runs._STAGE_FNS[s] = _happy(s)
    runs._STAGE_FNS["note"] = _fake_note
    graph.llm_task = lambda task, **kw: {"organize": "正文" * 100,
                                         "review": "校对" * 100,
                                         "metadata": '{"title": "题", "tags": ["t"], "summary": "摘"}'}[task]
    graph.select_frames = lambda tr, cand, total: ({"frames": [{"name": "f1.jpg", "caption": "c"}]}, 1, "")


_install_fakes()


def _fresh():
    for p in ("state.json", "runs.json", "runs.lock"):
        (_TMP / p).unlink(missing_ok=True)
    _install_fakes()


# ---- 1. 单条目 happy path ----------------------------------------------------
_fresh()
final = graph.run_graph("/tmp/fake.mp4", checkpointer=None)
assert final["status"] == "done", final.get("errors")
assert CALLS == [f"{s}:1" for s in ("download", "transcribe", "frames", "ocr", "select", "clips")] \
    + ["note:1"], CALLS
state_db = json.loads((_TMP / "state.json").read_text())
assert state_db["fp-1"]["status"] == "completed"
run = json.loads((_TMP / "runs.json").read_text())[final["run_id"]]
assert run["status"] == "done" and run["tmp_cleaned"]
print("✅ 1. 单条目 happy path（节点序 / state.json 落盘 / tmp 清理）")


# ---- 2. 多条目 + 幂等短路 ----------------------------------------------------
_fresh()
(_TMP / "state.json").write_text(json.dumps({
    "x": {"url": "https://bilibili.com/video/BV1aaa/", "status": "completed"}}))
runs.resolve_url = lambda url: [  # 假合集展开（剥网络）
    {"url": "https://bilibili.com/video/BV1aaa", "id": "1", "playlist_index": 1},
    {"url": "https://bilibili.com/video/BV2bbb", "id": "2", "playlist_index": 2}]
final = graph.run_graph("https://bilibili.com/video/BV1aaa", checkpointer=None)
assert final["status"] == "done", final.get("errors")
# 条目 1 幂等命中：全阶段跳过（_run_stage_inner 的 skipped 分支），LLM 也不烧
assert "note:1" not in CALLS and "note:2" in CALLS, CALLS
assert "download:1" not in CALLS and "download:2" in CALLS, CALLS
print("✅ 2. 多条目 + state.json 幂等短路（跳过条目不烧 LLM）")


# ---- 3. 阶段熔断 → fail 路由，下游节点不执行 ---------------------------------
_fresh()
def _boom(run, entry, art, tmp, args):
    raise SystemExit("模拟全局故障（token 过期）")
runs._STAGE_FNS["transcribe"] = _fake_stage("transcribe", lambda *_: (_ for _ in ()).throw(
    SystemExit("模拟全局故障")))
final = graph.run_graph("/tmp/fake.mp4", checkpointer=None)
assert final["status"] == "error" and any("transcribe" in e for e in final["errors"])
assert "frames:1" not in CALLS and "note:1" not in CALLS, CALLS
run = json.loads((_TMP / "runs.json").read_text())[final["run_id"]]
assert run["status"] == "error"
print("✅ 3. 阶段失败 → fail 路由（下游不执行，runs.json 留痕）")


# ---- 4. note LLM 三连败 → 整条 run 判 error ----------------------------------
_fresh()
graph.llm_task = lambda task, **kw: (_ for _ in ()).throw(RuntimeError("LLM 连续 3 次失败：HTTP 500"))
final = graph.run_graph("/tmp/fake.mp4", checkpointer=None)
assert final["status"] == "error" and any("LLM" in e for e in final["errors"])
print("✅ 4. note LLM 全局故障 → run error（不拿死 LLM 烧剩余条目）")


# ---- 5. 写盘质量门禁拒绝 → partial，不炸整 run --------------------------------
_fresh()
def _refuse(run, entry, art, tmp, args):
    raise SystemExit("正文疑似 LLM 拒答/空壳，拒绝写盘")
runs._STAGE_FNS["note"] = _refuse
final = graph.run_graph("/tmp/fake.mp4", checkpointer=None)
assert final["status"] == "partial", final
assert final["run"]["partial_failures"]["note"] == ["1"]
assert not (_TMP / "state.json").exists() or "fp-1" not in json.loads((_TMP / "state.json").read_text())
print("✅ 5. 拒答门禁 → partial（state.json 不落该条目，run 不炸）")


# ---- 6. 选帧 LLM 失败 → 启发式兜底，run 继续 ---------------------------------
_fresh()
graph.select_frames = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("HTTP 500"))
seen = {}
orig_select = runs._STAGE_FNS["select"]
def _spy_select(run, entry, art, tmp, args):
    seen.update(args)
    orig_select(run, entry, art, tmp, args)
runs._STAGE_FNS["select"] = _spy_select
final = graph.run_graph("/tmp/fake.mp4", checkpointer=None)
assert final["status"] == "done", final.get("errors")
assert seen.get("frames") is None and seen.get("selections") is None, seen  # 空 payload → 启发式
assert any("select_frames" in e for e in final["errors"])
print("✅ 6. 选帧 LLM 失败 → 启发式兜底（run 不炸，错误留痕）")


# ---- 7. checkpointer：done 后 delete_thread 裁剪 ------------------------------
_fresh()
saver = graph.make_checkpointer(_TMP / "ck" / "checkpoints.sqlite")
final = graph.run_graph("/tmp/fake.mp4", checkpointer=saver)
assert final["status"] == "done"
left = list(saver.list({"configurable": {"thread_id": final["run_id"]}}))
assert left == [], f"done 后 checkpoint 应被裁剪，剩 {len(left)} 条"
print("✅ 7. SqliteSaver 落库 + done 后 delete_thread 裁剪")

print("\n✅ test_graph.py 全部通过")

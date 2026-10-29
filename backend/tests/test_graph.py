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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/（包根）

from video2pdf import graph, observability, runs, util

_TMP = Path(tempfile.mkdtemp(prefix="video2pdf-test-graph-"))

# ---- 隔离文件 SSOT：state.json / runs.json / runs.lock / 日志目录全部进 tmp ----
_ORIG_STATE_FILE = util.STATE_FILE
_ORIG_LOG_DIR = util.LOG_DIR
_ORIG_RUNS_FILE = runs.RUNS_FILE
_ORIG_RUNS_LOCKFILE = runs._RUNS_LOCKFILE
_ORIG_STAGE_FNS = runs._STAGE_FNS.copy()
_ORIG_RESOLVE_URL = runs.resolve_url
_ORIG_GRAPH_LLM_TASK = graph.llm_task
_ORIG_GRAPH_SELECT_FRAMES = graph.select_frames
_ORIG_OBSERVABILITY_FLUSH = observability.flush
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


# ---- 7b. make_checkpointer_from_env：sqlite 默认 / postgres 参数守卫 ----------------
saver2, closer2 = graph.make_checkpointer_from_env({})  # 缺省 → sqlite 分支（closer no-op）
assert type(saver2).__name__ == "SqliteSaver" and callable(closer2)
closer2()
for bad, why in [({"V2PDF_CHECKPOINTER": "mysql"}, "未知类型"),
                 ({"V2PDF_CHECKPOINTER": "postgres"}, "没配 DSN")]:
    try:
        graph.make_checkpointer_from_env(bad)
        raise AssertionError(f"应报错（{why}）却没报")
    except RuntimeError as e:
        assert str(e)
print("✅ 7b. checkpointer env 分派（sqlite 默认；postgres 守卫在 import 前，不联网）")


# ---- 8. V2PDF_EVAL=1：eval_entry 接线——本 run 写盘条目评测 / 失败不致命 / 缺省不接线
from video2pdf import evals as _ev

os.environ["V2PDF_EVAL"] = "1"
_note_md = _TMP / "笔记1.md"
_note_md.write_text("# 笔记\n正文" * 50, encoding="utf-8")
_tr_md = _TMP / "t.md"
_tr_md.write_text("转录本" * 200, encoding="utf-8")
_orig_note_fn = runs._STAGE_FNS["note"]


def _note_real_files(run, entry, art, tmp, args):
    _orig_note_fn(run, entry, art, tmp, args)
    art.update(note=str(_note_md), transcript_path=str(_tr_md))  # eval 节点要读真实文件


_ev_calls = []
_ev_score_orig, _ev_rep_orig = _ev.score_note, _ev.report_scores

_fresh()
runs._STAGE_FNS["note"] = _note_real_files
_ev.score_note = lambda note, tr, **kw: _ev_calls.append(1) or {"note_quality": 0.9}
_ev.report_scores = lambda *a, **kw: "off"
final = graph.run_graph("/tmp/fake.mp4", checkpointer=None)
assert final["status"] == "done", final.get("errors")
assert len(_ev_calls) == 1  # 本 run 写盘的唯一条目被评
run = json.loads((_TMP / "runs.json").read_text())[final["run_id"]]
assert next(iter(run["artifacts"].values())).get("eval") == {"note_quality": 0.9}

# eval 全灭 → run 仍 done，错误留痕（旁路不炸批）
_fresh()
runs._STAGE_FNS["note"] = _note_real_files
_ev.score_note = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("judge 全灭"))
final = graph.run_graph("/tmp/fake.mp4", checkpointer=None)
assert final["status"] == "done", final.get("errors")
assert any("eval 条目" in e for e in final["errors"]), final["errors"]

# 缺省（开关关）→ 不接线，拓扑零变化
_ev.score_note = _ev_score_orig
os.environ.pop("V2PDF_EVAL")
_fresh()
final = graph.run_graph("/tmp/fake.mp4", checkpointer=None)
assert final["status"] == "done" and len(_ev_calls) == 1  # 没新增评测调用
_ev.report_scores = _ev_rep_orig
print("✅ 8. V2PDF_EVAL 在线评测节点：接线评测写回 artifacts / 失败不致命 / 缺省不接线")


# ---- 9. deliver 模式：artsink 激活缝 + finalize 搬家剥正文 ----------------------
from datetime import datetime as _dt

from video2pdf import artsink
from video2pdf import config as v2config
from video2pdf import note as note_mod

_orig_mode, _orig_ob, _orig_outdir = (v2config.ARTIFACT_MODE,
                                      v2config.OUTBOX_DIR, v2config.OUT_DIR)

# 9a. 激活守卫：deliver 模式下 sink_for 之外的写盘必须炸（宁可炸也不静默漏进 vault）
v2config.ARTIFACT_MODE = "deliver"
_violated = False
try:
    artsink.note_root()
except RuntimeError:
    _violated = True
assert _violated, "deliver 模式漏激活点没炸——「云端不储存」口径有兜底漏网"
# vault 模式：no-op 直通，访问器 = config 常量（现状逐字节不变）
v2config.ARTIFACT_MODE = "vault"
assert artsink.note_root() is _orig_outdir
with artsink.sink_for({"run_id": "x", "tmp": "/ignored"}):
    pass  # vault 下 sink_for 不建目录不激活
# deliver 下 sink_for 建齐三目录并线程内激活，退出即复位
v2config.ARTIFACT_MODE = "deliver"
_stub_run = {"run_id": "r_sink", "tmp": str(_TMP / "sinkrun")}
with artsink.sink_for(_stub_run):
    assert artsink.note_root() == Path(_stub_run["tmp"]) / "outbox"
    assert artsink.attach_dir().is_dir() and artsink.transcript_dir().is_dir()
assert artsink._SINK.get() is None
print("✅ 9a. artsink 激活缝（漏激活炸 / vault 直通 / sink 建目录+复位）")

# 9b. _relocate_outbox 单元级：剥 md、搬家、只改写 outbox 下的路径、在架位重入不炸
v2config.ARTIFACT_MODE, v2config.OUTBOX_DIR = "deliver", _TMP / "shelf-u"
_u = _TMP / "urun"
(_u / "outbox" / "transcripts").mkdir(parents=True)
(_u / "outbox" / "n.md").write_text("# 正文", encoding="utf-8")
(_u / "outbox" / "transcripts" / "t.txt").write_text("转录", encoding="utf-8")
run_u = {"run_id": "r_u", "tmp": str(_u), "artifacts": {
    "u1": {"md": "全文", "note": str(_u / "outbox" / "n.md"),
           "transcript_path": str(_u / "outbox" / "transcripts" / "t.txt"),
           "published": [["f1.jpg", 0.5]]},  # published 只存文件名：搬家不改写
    "u2": {"md": "全文", "note": "/vault/旧笔记.md"}}}  # 非 outbox 历史路径：原样保留
graph._relocate_outbox(run_u)
_shelf_u = _TMP / "shelf-u" / "r_u"
assert all("md" not in a for a in run_u["artifacts"].values()), "runs.json 养正文=口径破裂"
assert run_u["outbox"] == str(_shelf_u) and (_shelf_u / "n.md").is_file()
assert run_u["artifacts"]["u1"]["note"] == str(_shelf_u / "n.md")
assert run_u["artifacts"]["u1"]["transcript_path"] == str(_shelf_u / "transcripts" / "t.txt")
assert run_u["artifacts"]["u2"]["note"] == "/vault/旧笔记.md"
assert _dt.fromisoformat(run_u["artifacts_expire"]) > _dt.now()
assert not (_u / "outbox").exists()
graph._relocate_outbox(run_u)  # 崩溃恢复重入：src 已没了 → 只剩剥正文，幂等不炸
assert run_u["outbox"] == str(_shelf_u)
print("✅ 9b. _relocate_outbox：剥正文/搬家/路径改写/历史路径保留/重入幂等")

# 9c. 全链路（假 LLM 假阶段）：note 阶段经 artsink 真写文件 → finalize 搬家进在架位
_fresh()
v2config.OUTBOX_DIR = _TMP / "shelf-e2e"
v2config.OUT_DIR = _TMP / "vault-must-stay-empty"


def _deliver_note(run, entry, art, tmp, args):
    tp = note_mod.write_transcript("投递条目", art.get("transcript", "x"))
    p = note_mod.write_note("# 投递正文\n见转录", "投递条目")
    note_mod.append_index("投递条目", "2:00", "摘")
    art.update(note=str(p), title="投递条目", transcript_path=str(tp),
               fp="fp-deliver", md="# 投递正文\n见转录")


runs._STAGE_FNS["note"] = _deliver_note
final = graph.run_graph("/tmp/fake.mp4", checkpointer=None)
assert final["status"] == "done", final.get("errors")
rec = json.loads((_TMP / "runs.json").read_text())[final["run_id"]]
art = next(iter(rec["artifacts"].values()))
assert "md" not in art, "finalize 后 runs.json 不得残留正文"
shelf = _TMP / "shelf-e2e" / final["run_id"]
assert rec["outbox"] == str(shelf) and shelf.is_dir()
assert art["note"] == str(shelf / "投递条目.md") and Path(art["note"]).is_file()
assert (shelf / "transcripts").is_dir() and (shelf / "索引-append.md").is_file()
assert _dt.fromisoformat(rec["artifacts_expire"]) > _dt.now()
assert not (_TMP / "vault-must-stay-empty").exists(), "deliver 写入漏进了 vault！"
print("✅ 9c. deliver 全链路：note 经 sink 写 outbox → finalize 搬家，vault 零写入")

v2config.ARTIFACT_MODE, v2config.OUTBOX_DIR, v2config.OUT_DIR = (
    _orig_mode, _orig_ob, _orig_outdir)

# test_graph.py runs assertions at import/collection time. Restore every shared
# module global it temporarily redirects so later test modules keep their own
# persistence roots regardless of pytest collection order.
util.STATE_FILE = _ORIG_STATE_FILE
util.LOG_DIR = _ORIG_LOG_DIR
runs.RUNS_FILE = _ORIG_RUNS_FILE
runs._RUNS_LOCKFILE = _ORIG_RUNS_LOCKFILE
runs._STAGE_FNS.clear()
runs._STAGE_FNS.update(_ORIG_STAGE_FNS)
runs.resolve_url = _ORIG_RESOLVE_URL
graph.llm_task = _ORIG_GRAPH_LLM_TASK
graph.select_frames = _ORIG_GRAPH_SELECT_FRAMES
observability.flush = _ORIG_OBSERVABILITY_FLUSH

print("\n✅ test_graph.py 全部通过")

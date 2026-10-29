"""LangGraph 编排：Dify 画布「视频转文档」的代码化替代。

拓扑（逐节点对应原 DSL 的阶段 loop / LLM 节点 / if 闸门）：

    resolve → download → transcribe → frames → ocr
      → select_frames_llm（LLM 内容选帧；失败/空选 → 空 payload，select 阶段启发式兜底）
      → select → clips → note_entry（逐条目回环：整理→校对?→元数据→写盘）
        → [V2PDF_EVAL=1 时插入 eval_entry：刚写盘条目在线评测 → Langfuse scores，失败不致命]
      → finalize（状态落盘/清 tmp）→ END
    每个阶段节点失败 → fail（显式终止，对齐 DSL「失败即终止」闸门）

设计纪律（对齐旧系统语义，逐条有据）：
- 阶段节点复用 runs._run_stage：条目级容错 + 熔断器 + RunLog span 全部保留；
  节点入口先从 runs.json 重载 run——阶段产物只在阶段结束落盘，重载 = 幂等重入。
- 网络型阶段不挂 retry_policy：transcribe 一类阶段单次几十分钟，框架级重试会把
  熔断器省下的钱又烧回去；重试只活在 llm.chat（3 次，对齐 Dify 节点 retry_config）。
- note_entry 里 LLM 三连败视为全局故障 → 整条 run 判 error（不拿死 LLM 白烧剩余条目），
  对齐熔断哲学；单条目写盘被质量门禁拒绝则记 partial 继续下一条。
- state.json 幂等 SSOT 在 note_entry 写盘成功后**逐条**落盘（移植自 _stage_kb 末段），
  重发同 URL 即跳过；finalize 不再逐条写 state。
- thread_id == run_id：checkpoint（data/checkpoints.sqlite）与 runs.json 同键。
  run 完成后 delete_thread 裁掉本 thread 全部 checkpoint（磁盘只增不减的解药）；
  失败 run 的 checkpoint 保留备查（量小，且 _cleanup_stale_runs 级别的问题不归它管）。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from . import artsink
from .config import ENV, PROJ
from .graph_obs import GraphObsHandler
from .llm import assemble_entry_note, llm_task, select_frames
from .note import MIN_NOTE_TRANSCRIPT_CHARS
from .resolve import extract_url
from .runs import _STAGE_FNS, _load_runs, _run_outputs, _run_stage, _runs_file_lock, _runs_merge, create_run
from .state import RunState
from .util import (
    _STAGE_LOGS,
    RunLog,
    bind_current_run,
    fmt_hms,
    load_state,
    redact,
    save_state,
    state_key,
    state_record,
    state_file_lock,
)

CHECKPOINT_DB = PROJ / "data" / "checkpoints.sqlite"

# 全量重跑 force=True 时 note 阶段覆盖写盘（对应旧 serve 的 force_note 语义入口）
_FORCE_NOTE_ARGS = {"force_note": True}



# ---------------------------------------------------------------- 工具

def _reload(run_id: str, fallback: dict) -> dict:
    """节点入口从 runs.json 重载 run：checkpoint 里的是节点入口快照，
    只有 runs.json 带着上一阶段（或上一进程崩溃前）落盘的最新产物。"""
    return _load_runs().get(run_id) or fallback



def _persist(run: dict) -> None:
    with _runs_file_lock():
        _runs_merge(run)



# ---------------------------------------------------------------- 节点

def node_resolve(state: RunState) -> dict:
    """剥分享文本 → 建 run（resolve 展开条目 + state 幂等标记）+ 同链去重复用。

    state 已带 run（api.py 同步解析过）→ 直接用，不重复建——POST /runs 的同步语义
    保留给调用方（建 run 即知 total/all_skipped），图内只做兜底。
    """
    if state.get("run"):
        return {}
    url = extract_url(state.get("url", ""))
    part_items = (state.get("part_items") or "").strip()
    force = bool(state.get("force"))
    if not url:
        return {"status": "error", "errors": [*state.get("errors", []), "缺 url"]}
    run, err, _dup = resolve_run(url, part_items, force, run_id=state["run_id"],
                                 owner=state.get("owner", ""))
    if err:
        return {"status": "error", "errors": [*state.get("errors", []), err]}
    return {"run": run}



def resolve_run(url: str, part_items: str = "", force: bool = False,
                run_id: str = "", owner: str = "") -> tuple[dict, str, bool]:
    """建 run 的同步入口（api.py POST /runs 与 node_resolve 共用）。

    返回 (run, err, is_dup)：err 非空即失败；is_dup=True 时 run 是进行中旧 run
    （调用方须自己判活：旧 run 的驱动已死就得重新入队驱动它）。
    owner 参与去重比对：多用户下同一 URL 由不同 owner 提交各建各的 run（互不复用、
    不串产物），同一 owner 重发同链仍复用其进行中的 run。CLI/开发 owner="" 保持旧全局去重。
    """
    part_items = (part_items or "").strip()
    # 同链接同分区同归属的进行中 run 复用（对齐 serve._post_runs 的 dedup；
    # part_items 必须参与比对——同合集的分区补跑共享 URL，只按 URL 去重会把新分区并进旧 run）
    dup = next((r for r in _load_runs().values()
                if not r.get("deleted") and r.get("url") == url and str(r.get("part_items", "")).strip() == part_items
                and (r.get("owner", "") or "") == (owner or "")
                and r.get("status") in ("created", "queued", "running")), None)
    if dup:
        print(f"♻️ 复用进行中的 run {dup['run_id']}（{dup['total']} 条目），继续推进")
        return dup, "", True
    try:
        run = create_run(url, part_items, force=force, owner=owner)
    except RuntimeError as exc:
        return {}, str(exc)[:300], False
    if run_id:
        run["run_id"] = run_id  # thread_id 即 run_id
    run["status"] = "created"   # created=已建未驱动；_drive 开跑时转 running（启动恢复只认 running）
    _persist(run)
    skipped = sum(1 for a in run["artifacts"].values() if a.get("skipped"))
    print(f"✅ run {run['run_id']}：{run['total']} 条" + (f"（{skipped} 条幂等跳过）" if skipped else ""))
    return run, "", False



def _stage_node(stage: str, args_of=None):
    """阶段节点工厂：包一层 runs._run_stage，失败 → errors（路由到 fail 节点）。"""

    def _node(state: RunState) -> dict:
        run = _reload(state["run_id"], state.get("run") or {})
        args = args_of(state) if args_of else {}
        try:
            _run_stage(run, stage, args)
            if stage == "clips":
                # All GPU artifacts are local now. Release Colab before note LLMs.
                from .colab import release_session
                release_session()
        except (SystemExit, Exception) as exc:  # 熔断/全灭/环境炸 → 显式失败（DSL 的 if_ok 闸门）
            msg = redact(str(exc))[-300:]
            print(f"❌ 阶段 {stage} 失败：{msg}")
            # 留痕落 run 记录（P1-4）：errors 此前只进 LangGraph state + 驱动进程 _TASKS，
            # 跨副本/重启后 GET 看不到失败原因——record jsonb 是状态唯一真相，这里补写
            # （含 cancel 的「用户取消（cancelled）」、熔断与全灭消息，≤20 条对齐留痕纪律）。
            line = f"阶段 {stage}: {msg}"
            prev = run.get("errors") or []
            if not prev or prev[-1] != line:  # fail/finalize 路径可能同消息双写，尾去重
                run["errors"] = [*prev, line][-20:]
                _persist(run)
            return {"run": run, "status": "error",
                    "errors": [*state.get("errors", []), f"阶段 {stage}: {msg}"]}
        return {"run": run}

    _node.__name__ = f"node_{stage}"
    return _node



def _failed_dependencies(run: dict, idx: int, stages: tuple[str, ...]) -> list[str]:
    failures = run.get("partial_failures") or {}
    return [stage for stage in stages
            if str(idx) in {str(value) for value in failures.get(stage, [])}]


def node_select_frames(state: RunState) -> dict:
    """内容选帧 LLM：整 run 转录本 + 候选清单（多条目带分节头）→ select 阶段 payload。

    LLM 失败或空选不致命：空 payload 喂给 _stage_select，逐条目走本地启发式兜底
    （对齐 DSL code_parse_select 的 note 语义；比 Dify 时代「LLM 挂=整 run 挂」多一层韧性）。
    """
    run = _reload(state["run_id"], state.get("run") or {})
    # Keep original collection indexes in prompts while excluding failed entries.
    eligible = {entry["url"]: run["artifacts"].get(entry["url"], {})
                for idx, entry in enumerate(run["entries"], 1)
                if not _failed_dependencies(run, idx, ("download", "transcribe", "frames", "ocr"))
                and not run["artifacts"].get(entry["url"], {}).get("skipped")
                and not run["artifacts"].get(entry["url"], {}).get("note")}
    selection_run = {**run, "artifacts": eligible}
    pending = [str(a.get('transcript') or '').strip() for a in eligible.values()
               if not a.get('skipped') and not a.get('note') and str(a.get('transcript') or '').strip()]
    if pending and all(len(text) < MIN_NOTE_TRANSCRIPT_CHARS for text in pending):
        message = f"note: 待处理转录本过短（均不足 {MIN_NOTE_TRANSCRIPT_CHARS} 字），停止模型生成与剪辑"
        return {'run': run, 'status': 'error', 'errors': [*state.get('errors', []), message]}
    transcript = _run_outputs(selection_run, "transcript")
    candidates = _run_outputs(selection_run, "candidates")
    total = int(run.get("total") or 1)
    if not transcript.strip() or not candidates.strip():
        # 全跳过 run（state 幂等全命中）没有产物——正常路径，不是错误
        note = "无转录本/候选清单（全跳过？），select 阶段按条目各自处理"
        return {"run": run, "select_payload": {}, "select_note": note}
    try:
        payload, n_sel, note = select_frames(transcript, candidates, total)
        print(f"  🧠 LLM 选帧 {n_sel} 帧" + (f"（{note}）" if note else ""))
    except Exception as exc:  # LLM 三连败 → 启发式兜底（不熔断：选帧错不该废掉整条 run）
        msg = redact(str(exc))[:200]
        print(f"  ⚠ LLM 选帧失败（{msg}），降级本地启发式")
        payload, note = {}, f"LLM 选帧失败已降级启发式：{msg}"
        return {"run": run, "select_payload": payload, "select_note": note,
                "errors": [*state.get("errors", []), f"select_frames LLM: {msg}"]}
    return {"run": run, "select_payload": payload, "select_note": note}



def node_note_entry(state: RunState) -> dict:
    """逐条目写盘：本条目转录本 → 整理 →（校对）→ 元数据 → 组装 → _stage_note。

    幂等旁路（对齐 DSL 的 if_entry_done）：skipped / 已写盘（note_done）条目
    跳过全部 LLM——不视同情跳会把空转录本喂给 LLM 白烧 3 次调用/条。
    """
    run = _reload(state["run_id"], state.get("run") or {})
    idx = int(state.get("entry_idx") or 1)
    entry = run["entries"][idx - 1]
    art = run["artifacts"].setdefault(entry["url"], {})
    nxt = {"run": run, "entry_idx": idx + 1}
    if art.get("skipped") or art.get("note"):
        print(f"  ⏭ 条目 {idx}/{run['total']} 已写盘/幂等跳过，不烧 LLM")
        return nxt
    blocked = _failed_dependencies(run, idx, ("download", "transcribe", "frames", "ocr", "select", "clips"))
    if (not art.get("images") and art.get("published") and art.get("clips") is None
            and (ENV.get("V2PDF_CLIPS_REMOTE") or "").strip() and "clips" not in blocked):
        blocked.append("clips")
    if blocked:
        msg = f"note 条目 {idx}: 依赖阶段 {', '.join(blocked)} 未成功，停止模型生成，请先补跑失败阶段"
        failed = run.setdefault("partial_failures", {}).setdefault("note", [])
        if str(idx) not in failed:
            failed.append(str(idx))
        _persist(run)
        return {**nxt, "errors": [*state.get("errors", []), msg]}
    transcript = (art.get("transcript") or "").strip()
    if len(transcript) < MIN_NOTE_TRANSCRIPT_CHARS:
        # 对齐 DSL if_entry_text 门禁 + _stage_note 硬门禁：无转录本绝不喂 LLM、绝不写盘
        msg = (f"note 条目 {idx} 无转录本（transcribe 未成功），跳过写盘" if not transcript else
               f"note 条目 {idx}: 转录本过短（{len(transcript)} 字 < {MIN_NOTE_TRANSCRIPT_CHARS}），跳过模型生成")
        print(f"  ⚠ {msg}")
        run.setdefault("partial_failures", {}).setdefault("note", []).append(str(idx))
        _persist(run)
        return {**nxt, "errors": [*state.get("errors", []), msg]}
    video_name = entry.get("title") or (Path(art["video"]).stem if art.get("video") else "")
    dur_str = fmt_hms(art.get("duration") or 0.0)
    # RunLog 注册进 _STAGE_LOGS：llm_task 的 span 才能挂进 run 的 trace（对齐 _run_stage 的待遇）
    log = RunLog("stage:note", run_id=run["run_id"], trace_name=run.get("url", ""),
                 trace_meta={"stage": "note", "entry": str(idx)},
                 user_id=run.get("owner", ""))
    _STAGE_LOGS[run["run_id"]] = log
    try:
        organize = llm_task("organize", idx=idx, total=run["total"],
                            video_filename=video_name, duration=dur_str,
                            entry_frames=art.get("manifest") or "",
                            entry_transcript=transcript)
        review_text = llm_task("review", entry_transcript=transcript,
                               organize_text=organize) if state.get("review") else ""
        meta_raw = llm_task("metadata", video_filename=video_name, idx=idx,
                            organize_text=organize)
    except Exception as exc:
        # LLM 三连败 = 全局故障（鉴权/中转/配额）：整条 run 判 error，不拿死 LLM 烧剩余条目
        msg = redact(str(exc))[-300:]
        print(f"  ❌ 条目 {idx} LLM 链失败：{msg}")
        return {**nxt, "status": "error",
                "errors": [*state.get("errors", []), f"note 条目 {idx} LLM: {msg}"]}
    finally:
        run.setdefault("span_log", []).extend(log.spans)
        _STAGE_LOGS.pop(run["run_id"], None)
    args = {**assemble_entry_note(organize, review_text, meta_raw),
            "model": "turbo", **(_FORCE_NOTE_ARGS if run.get("force") else {})}
    tmp = Path(run["tmp"])
    etmp = tmp if run["total"] == 1 else tmp / f"e{idx:02d}"
    etmp.mkdir(parents=True, exist_ok=True)
    try:
        with artsink.sink_for(run):  # note 直调不经 _run_stage，产物落点须单独激活
            _STAGE_FNS["note"](run, entry, art, etmp, args)
    except SystemExit as exc:
        # 质量门禁（拒答/空壳/转录本过短）：记 partial 继续下一条，不炸整 run
        msg = f"note 条目 {idx}: {redact(str(exc))[:200]}"
        print(f"  ⚠ {msg}")
        run.setdefault("partial_failures", {}).setdefault("note", []).append(str(idx))
        _persist(run)
        return {**nxt, "errors": [*state.get("errors", []), msg]}
    # state.json 幂等 SSOT 逐条落盘（移植自 _stage_kb 末段：重发同 URL 跳过本条目）。
    # 「load→改→save」整段持锁：并发 run 各落各的 fp 时不丢更新
    with state_file_lock():
        state_db = load_state()
        # owner is part of the state key as well as the record. This keeps
        # authenticated tenants from overwriting each other's idempotency row
        # even when a legacy/development vault backend is selected.
        state_db[state_key(art["fp"], run.get("owner", ""))] = {"note": art.get("note", ""), "transcript": art.get("transcript_path", ""),
                               "title": art.get("title", ""), "url": entry.get("url", ""),
                               "date": date.today().isoformat(), "run_id": run["run_id"],
                               "owner": run.get("owner", ""),
                               "status": "completed",
                               "spans": [{k: v for k, v in sp.items() if k != "spanId"}
                                         for sp in run.get("span_log", [])][-80:]}
        save_state(state_db)
    _persist(run)
    return nxt



def node_eval_entry(state: RunState) -> dict:
    """在线逐条目评测（仅 V2PDF_EVAL=1 时接线）：刚写盘的 note+transcript → evals 打分
    → Langfuse scores + 分数写回 artifacts[url].eval。

    旁路纪律：任何失败只记 errors 继续（评测是旁路不是门禁，不继承 note 的 3-strikes
    语义）；只评「本 run 新写盘」的条目——state.json 幂等记录的 run_id 比对，
    幂等跳过/门禁拒答/无转录本的条目不评（也没东西可评）。
    """
    run = _reload(state["run_id"], state.get("run") or {})
    if state.get("status") == "error":
        return {"run": run}  # 上一步 LLM 全局故障：交给路由去 fail，不评
    idx = int(state.get("entry_idx") or 2) - 1  # note_entry 已推进游标，评刚处理完的那条
    try:
        entry = run["entries"][idx - 1]
        art = (run.get("artifacts") or {}).get(entry.get("url"), {})
        if not (art.get("note") and art.get("transcript_path")):
            return {"run": run}  # 幂等跳过/门禁拒答：本 pass 没写盘
        rec = state_record(load_state(), art.get("fp") or "", run.get("owner", ""))
        if rec.get("run_id") != run["run_id"]:
            return {"run": run}  # note 是旧 run 写的（幂等复用），不重复评
        note_p, tr_p = Path(art["note"]), Path(art["transcript_path"])
        if not (note_p.exists() and tr_p.exists()):
            return {"run": run}
        from . import evals  # 延迟 import：deepeval/ragas 是 extras，graph 运行时不染
        scores = evals.score_note(note_p.read_text(encoding="utf-8"),
                                  tr_p.read_text(encoding="utf-8"))
        if scores:
            art["eval"] = scores
            _persist(run)
            via = evals.report_scores(run["run_id"], scores,
                                      comment=f"在线 eval 条目 {idx}/{run.get('total')}")
            print(f"  📊 条目 {idx}/{run.get('total')} 评测（{via}）：" +
                  "  ".join(f"{k}={v:.3f}" for k, v in scores.items()))
    except Exception as exc:
        msg = redact(str(exc))[:200]
        print(f"  ⚠ 条目 {idx} 在线评测失败（不影响 run）：{msg}")
        return {"run": run, "errors": [*state.get("errors", []), f"eval 条目 {idx}: {msg}"]}
    return {"run": run}



def _relocate_outbox(run: dict) -> None:
    """deliver 收尾：tmp/outbox 整体搬到在架位 OUTBOX_DIR/<run_id>，并把正文从 runs.json 剥掉。

    「云端不储存」的诚实口径：正文只活在在架窗口（TTL/ACK 即焚，见 api.py），runs.json
    连 md 全文副本都不留；note/transcript_path 改写到在架位，端点按它取文件。
    崩溃恢复后重入 finalize 看到旧在架：整份替换（本 run 才是产物的权威版本）。
    """
    for art in (run.get("artifacts") or {}).values():
        art.pop("md", None)
    src = Path(run.get("tmp") or "") / "outbox"
    if not src.is_dir():
        return
    dst = artsink.outbox_dir() / run["run_id"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.move(str(src), str(dst))
    run["outbox"] = str(dst)
    run["artifacts_expire"] = (datetime.now() + timedelta(
        hours=artsink.outbox_ttl_h())).isoformat(timespec="seconds")
    run.pop("artifacts_burned", None)
    for art in (run.get("artifacts") or {}).values():
        for key in ("note", "transcript_path"):
            p = art.get(key)
            if p:
                try:
                    art[key] = str(dst / Path(p).relative_to(src))
                except ValueError:
                    pass  # 非 outbox 下的历史路径（vault 旧数据重跑）：不改写，随 tmp 一起没了就没了
    # 帧/节段发布记录（published）与选中清单只存文件名，wikilink 按名解析，搬家不用改写


def node_finalize(state: RunState) -> dict:
    """收尾：run 状态落盘 + 清临时目录（deliver 模式先把 outbox 搬家到在架位）。"""
    run = _reload(state["run_id"], state.get("run") or {})
    errors = state.get("errors") or []
    if errors:
        run['errors'] = list(dict.fromkeys([*(run.get('errors') or []), *errors]))[-20:]
    run["status"] = "partial" if run.get("partial_failures") else "done"
    if artsink.current_mode() == "deliver":
        _relocate_outbox(run)
    if run.get("tmp") and not run.get("tmp_cleaned"):
        shutil.rmtree(run["tmp"], ignore_errors=True)  # 末阶段：临时产物可清
        run["tmp_cleaned"] = True
    _persist(run)
    print(f"🏁 run {run['run_id']} → {run['status']}"
          + (f" · 产物在架至 {run.get('artifacts_expire', '?')}" if run.get("outbox") else "")
          + (f"（{len(errors)} 条留痕）" if errors else ""))
    return {"run": run, "status": run["status"]}



def node_fail(state: RunState) -> dict:
    """失败终态：留痕进 run 记录；tmp 保留供排障（失活清理由服务启动钩子兜底）。"""
    run = _reload(state["run_id"], state.get("run") or {})
    run["status"] = "error"
    run["errors"] = [*(run.get("errors") or []), *(state.get("errors") or [])][-20:]
    _persist(run)
    print(f"💥 run {run['run_id']} 失败：" + "；".join((state.get("errors") or ["?"])[-3:]))
    return {"run": run, "status": "error"}



# ---------------------------------------------------------------- 路由

def _route_ok(state: RunState) -> str:
    return "fail" if state.get("status") == "error" else "ok"



def _route_note(state: RunState) -> str:
    if state.get("status") == "error":
        return "fail"
    run = state.get("run") or {}
    return "loop" if int(state.get("entry_idx") or 1) <= int(run.get("total") or 1) else "done"



# ---------------------------------------------------------------- 建图

def _eval_enabled() -> bool:
    """在线逐条评测开关（V2PDF_EVAL=1）。建图时读取——是否插 eval_entry 是拓扑级决策。"""
    v = os.environ["V2PDF_EVAL"] if "V2PDF_EVAL" in os.environ else ENV.get("V2PDF_EVAL", "")
    return (v or "").strip().lower() in ("1", "true", "on")



def build_graph(checkpointer=None):
    """编译 StateGraph。checkpointer=None → 纯内存（单测）；生产传 SqliteSaver。"""
    g = StateGraph(RunState)
    g.add_node("resolve", node_resolve)
    for s in ("download", "transcribe", "frames", "ocr"):
        g.add_node(s, _stage_node(s))
    g.add_node("select_frames_llm", node_select_frames)
    g.add_node("select", _stage_node("select",
               lambda st: {**(st.get("select_payload") or {}),
                           "max_frames": st.get("max_frames") or 60}))
    g.add_node("clips", _stage_node("clips"))
    g.add_node("note_entry", node_note_entry)
    g.add_node("finalize", node_finalize)
    g.add_node("fail", node_fail)

    g.add_edge(START, "resolve")
    g.add_conditional_edges("resolve", _route_ok, {"ok": "download", "fail": "fail"})
    prev = "download"
    for nxt in ("transcribe", "frames", "ocr"):
        g.add_conditional_edges(prev, _route_ok, {"ok": nxt, "fail": "fail"})
        prev = nxt
    g.add_conditional_edges("ocr", _route_ok, {"ok": "select_frames_llm", "fail": "fail"})
    # LLM failure still falls back without setting error status. A quality
    # rejection explicitly sets error status and must stop before publishing.
    g.add_conditional_edges("select_frames_llm", _route_ok, {"ok": "select", "fail": "fail"})
    g.add_conditional_edges("select", _route_ok, {"ok": "clips", "fail": "fail"})
    g.add_conditional_edges("clips", _route_ok, {"ok": "note_entry", "fail": "fail"})
    # note 逐条目回环：entry_idx 从 1 起，> total 出环
    if _eval_enabled():
        # 在线评测旁路：note_entry 后插 eval_entry（仿 select_frames_llm 的无条件边——
        # eval 失败只记 errors，路由口径与 note 回环相同，评测永不影响 run 成败）
        g.add_node("eval_entry", node_eval_entry)
        g.add_edge("note_entry", "eval_entry")
        g.add_conditional_edges("eval_entry", _route_note,
                                {"loop": "note_entry", "done": "finalize", "fail": "fail"})
    else:
        g.add_conditional_edges("note_entry", _route_note,
                                {"loop": "note_entry", "done": "finalize", "fail": "fail"})
    g.add_edge("finalize", END)
    g.add_edge("fail", END)
    return g.compile(checkpointer=checkpointer)



def make_checkpointer(path: Path = CHECKPOINT_DB):
    """SQLite checkpointer（默认形态）。调用方持有返回的 saver 保活（连接随 saver 生命周期）。

    裁剪策略在 run_graph：run 完成（done/partial）即 delete_thread——完成的 run
    不需要步进历史（重跑靠 state.json 幂等，不靠 checkpoint）；失败 run 的
    checkpoint 保留备查（量小）。
    """
    from langgraph.checkpoint.sqlite import SqliteSaver

    path.parent.mkdir(parents=True, exist_ok=True)
    saver = SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))
    saver.setup()  # 幂等建表
    return saver


def make_checkpointer_from_env(env: dict | None = None) -> tuple:
    """按 env 装配 checkpointer：``(saver, closer)``，closer 在进程/服务退出时调用。

    V2PDF_CHECKPOINTER=sqlite（默认，零外部服务，本地/e2e/契约测试都走它）| postgres。
    postgres 分支：V2PDF_PG_DSN 必填；图跑在 asyncio.to_thread 工作线程里、多家并发共享
    连接，psycopg 单连接非线程安全 → 用连接池（autocommit + prepare_threshold=0 是
    langgraph PostgresSaver 官方口径）。delete_thread 裁剪语义两家通用，run_graph 不改。
    """
    env = env if env is not None else ENV
    kind = (env.get("V2PDF_CHECKPOINTER") or "sqlite").strip().lower()
    if kind in ("", "sqlite"):
        return make_checkpointer(), lambda: None
    if kind != "postgres":
        raise RuntimeError(f"未知 V2PDF_CHECKPOINTER：{kind}（支持 sqlite|postgres）")
    dsn = (env.get("V2PDF_PG_DSN") or "").strip()
    if not dsn:
        raise RuntimeError("V2PDF_CHECKPOINTER=postgres 需要配 V2PDF_PG_DSN")
    from langgraph.checkpoint.postgres import PostgresSaver

    from .pg import shared_pool
    pool = shared_pool()  # 与 P3 三存储/事件扇出共用进程级池（消双池）
    # dict 行工厂：langgraph PostgresSaver 官方口径（psycopg 默认 tuple 行不匹配其反序列化）
    saver = PostgresSaver(pool)  # type: ignore[arg-type]
    # Production API uses the DML-only app role. The migrate command creates
    # checkpoint tables with the owner role before the API starts; setup stays
    # opt-in for local/dev single-role deployments.
    if (env.get("V2MD_CHECKPOINTER_SETUP", "on") or "on").strip().lower() != "off":
        saver.setup()
    return saver, lambda: None



def run_graph(url: str, *, part_items: str = "", force: bool = False, review: bool = False,
              max_frames: int = 60, run_id: str = "", checkpointer=None,
              owner: str = "") -> dict:
    """一次 run 的入口（api.py 后台任务与 graph-run CLI 共用）。返回终态 state。

    owner = 归属内部 uid（多用户隔离）：随初始 state 流转到 resolve，做 owner 级去重；
    同时绑定个人 LLM/Colab 执行上下文。CLI/开发留空串=旧全局去重语义。
    """
    from . import deadman

    run_id = run_id or uuid.uuid4().hex[:8]
    bind_current_run(run_id)  # 并发多 run 时 current_log() 按 ContextVar 取本 run 的 log
    initial: RunState = {"run_id": run_id, "url": url, "part_items": part_items,
                         "force": force, "review": review, "max_frames": max_frames,
                         "owner": owner,
                         "entry_idx": 1, "errors": [], "status": "running"}
    existing = _load_runs().get(run_id)
    if (existing and existing.get("owner", "") == owner and existing.get("url") == url
            and existing.get("status") in ("created", "queued", "running")):
        initial["run"] = existing
    graph = build_graph(checkpointer)
    # note 回环每条目 1 个 superstep（8 + total + 2；V2PDF_EVAL 接线时每条目 2 个），
    # LangGraph 默认 recursion_limit=25 会杀掉 >15 条目的合集——放宽到 1000
    # （superstep 是真干活，不是空转，不存在烧轮次）
    # 图编排层自动插桩：节点/state 增量/路由决策 → RunLog 事件流（graph:* 前缀），
    # api 后台驱动与 graph-run CLI 两条入口自动覆盖
    config = {"recursion_limit": 1000,
              "callbacks": [GraphObsHandler(run_id, trace_name=url, user_id=owner)]}
    if checkpointer:
        config["configurable"] = {"thread_id": run_id}
    deadman.ping("start", run_id=run_id, trace_name=url)
    try:
        from .colab import session_scope
        from .pg import tenant_scope
        from .llm_settings import user_scope
        with tenant_scope(owner), user_scope(owner), session_scope(owner=owner):
            final = graph.invoke(initial, config=config)
    except Exception:
        deadman.ping("fail", run_id=run_id, trace_name=url)
        from .util import clear_current_run
        clear_current_run()
        raise
    if checkpointer and final.get("status") in ("done", "partial"):
        # checkpoint 裁剪：完成的 run 不需要步进历史（重跑靠 state.json 幂等，不靠 checkpoint）
        try:
            checkpointer.delete_thread(run_id)
        except Exception as exc:
            print(f"  ⚠ checkpoint 裁剪失败（不影响产物）：{redact(str(exc))[:120]}")
    # partial 也 ping 成功：部分失败在 trace/metrics 已留痕，死人开关只回答「跑没跑完」
    deadman.ping("fail" if final.get("status") == "error" else "ok",
                 run_id=run_id, trace_name=url)
    from .util import clear_current_run
    clear_current_run()
    return final

"""run 状态（runs.json）+ 阶段注册表 + 7 个 _stage_* + 阶段子进程入口（kb 已随 Dify 抽离摘除）。"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from datetime import datetime

from .config import ENV, MAX_TRANSCRIPT_CHARS, PROJ
from .util import RunLog, _STAGE_LOGS, current_log, fmt_hms, load_state, probe, redact, sanitize_filename, video_fingerprint
from . import observability
from .resolve import _part_selected, ensure_scheme, normalize_url, resolve_url
from .download import download_entry, download_images
from .transcribe import transcribe
from .frames import _NORM, extract_frame_candidates, frame_pool_size, manifest_of, merge_timeline, name_candidates, ocr_candidates, publish_named, select_content_frames
from .clips import cut_clip_files, insert_clip_embeds, remote_cut_clips
from .note import MIN_NOTE_TRANSCRIPT_CHARS, append_index, extract_toc, looks_like_refusal, render_note, sanitize_embeds, write_note, write_transcript











# ---------------------------------------------------------------- run 会话（LangGraph 阶段编排）
# 本地管线拆成阶段：download → transcribe → frames → ocr → select → clips → note。
# graph 的阶段节点直调 _run_stage（进程内）；LLM 在中间做「内容选帧」与「改写」决策。
# `__stage` 子命令保留手动排障入口（单阶段子进程重放）；CLI 直跑路径复用同一套函数。
RUNS_FILE = Path(ENV.get("V2MD_RUNS_FILE", str(PROJ / "runs.json")))


_RUNS_LOCK = threading.Lock()


_RUNS_LOCKFILE = RUNS_FILE.with_suffix(".lock")


STAGES = ("download", "transcribe", "frames", "ocr", "select", "clips", "note")



def _load_runs() -> dict:
    if RUNS_FILE.exists():
        try:
            return json.loads(RUNS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}



@contextlib.contextmanager
def _runs_file_lock():
    """跨进程互斥 + 读写整段原子。

    现役写方是 api 单进程（POST /runs 建 run、阶段结束落盘）；但 `__stage` 手动排障
    与 graph-run CLI 可能与 api 并发跑，跨进程语义保留。线程锁只管得住同进程；固定
    `.json.tmp` 路径下两个进程会互相截断——先 rename 的那个把对方写了一半的空文件
    发布出去，读方拿到 {} 后下一次 merge 就只写自己那一个 key，其余的 run 全丢。
    """
    with _RUNS_LOCK:
        with _RUNS_LOCKFILE.open("a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)



def _save_runs(runs: dict) -> None:
    tmp = RUNS_FILE.with_name(f"{RUNS_FILE.name}.tmp.{os.getpid()}")  # 按 pid 命名：rename 不打架
    tmp.write_text(json.dumps(runs, ensure_ascii=False, indent=1))
    tmp.replace(RUNS_FILE)



def _runs_merge(run: dict) -> dict:
    """读-改-写 runs.json（调用方须持有 _runs_file_lock，见 _run_stage）。"""
    runs = _load_runs()
    runs[run["run_id"]] = run
    _save_runs(runs)
    return runs



def create_run(url: str, part_items: str = "", force: bool = False) -> dict:
    """建 run：resolve 展开条目 + 临时目录；已按 url 幂等处理的条目标 skipped（force=True 强制全量）。

    返回 run（未落盘前）；落盘由调用方 _runs_merge。
    """
    url = ensure_scheme(url)
    entries = resolve_url(url) if url.startswith(("http://", "https://")) else []
    if not entries:
        entries = [{"url": url, "id": ""}]
    # 条目 URL 归一化（剥 spm_id_from 等跟踪参数）：state 幂等按 url 匹配，不归一会漏跳过
    for e in entries:
        if e.get("url"):
            e["url"] = normalize_url(e["url"])
    if part_items:
        entries = [e for e in entries if _part_selected(e.get("playlist_index"), part_items)]
    if not entries:
        raise RuntimeError("链接未解析出任何条目（--part-items 过滤后为空？）")
    state = load_state()
    # 幂等比对按 rstrip('/') 口径：历史 state 里躺着无尾斜杠的旧记录（归一化加 / 之前落盘的），
    # 严格相等会让已完成的视频整链重跑（实测 44min 视频重复下载+转录）
    done_keys = {str(v.get("url", "")).rstrip("/")
                 for v in state.values() if not v.get("dry_run") and v.get("url")}
    artifacts: dict[str, dict] = {}
    for e in entries:
        art = {}
        if not force and e["url"].rstrip("/") in done_keys:
            art["skipped"] = True  # 已处理过：阶段直接跳过，不重复烧 token 重写
        artifacts[e["url"]] = art
    run = {
        "run_id": uuid.uuid4().hex[:8],
        "url": url, "part_items": part_items,
        "created": datetime.now().isoformat(timespec="seconds"),
        "tmp": tempfile.mkdtemp(prefix="video2md-run-"),
        "total": len(entries),
        "entries": entries,
        "artifacts": artifacts,
        "stages": {},
        "status": "created",
    }
    if force:
        run["force"] = True
    return run



_CRITICAL_STAGE_OK = {"download": "video", "transcribe": "segs",
                      "frames": "candidates", "ocr": "ocr_raw"}



_CB_GLOBAL_PAT = re.compile(
    r"authorization code|oauth|refresh token|connecttimeout|connection.*timed out"
    r"|failed to refresh|proxy|tunnel|风控|econnrefused|403 forbidden", re.I)



def _err_sig(msg: str) -> str:
    """错误签名：抹掉易变部分（URL/长随机串/数字），让「同一根因」压成同一串供熔断计数。"""
    s = msg.lower()
    s = re.sub(r"https?://\S+|t{1,2}ps?%\S+", "<url>", s)  # redact() 可能削掉协议首字母
    s = re.sub(r"(?:/[\w.\-]+)+/?", "<path>", s)           # 文件路径（帧名/条目号各异）
    s = re.sub(r"[a-z0-9]{16,}", "<tok>", s)               # state/token 等长随机串
    s = re.sub(r"\d+", "<n>", s)
    return re.sub(r"\s+", " ", s).strip()[:120]



def _run_stage(run: dict, stage: str, args: dict) -> None:
    """对 run 内每个条目执行阶段函数；单条失败不阻断其余。幂等由各阶段产物字段保证。

    熔断：连续 N 条同一错误签名 → 判定全局故障（token 过期/断网/风控），立刻终止阶段。
    全局型错误（认证/网络类，_CB_GLOBAL_PAT）2 连即熔，其余 V2MD_CB_THRESHOLD（默认 3）连熔——
    不把 27 条各自白烧 5 分钟才发现是同一个死法。

    观测：run 级 RunLog（run_id 复用，trace 跨子进程拼合）——阶段 1 个 span、
    每条目 1 个子子 span；jsonl 双写 + OTel sink（配了 OTEL 端点/LANGFUSE 才启用）。"""
    log = RunLog(f"stage:{stage}", run_id=run["run_id"], trace_name=run.get("url", ""),
                 trace_meta={"stage": stage, "total": str(run.get("total", ""))})
    _STAGE_LOGS[run["run_id"]] = log
    t0 = time.monotonic()
    status = "completed"
    try:
        with log.span(f"stage:{stage}") as sp:  # run → stage → entry 三层 span 树
            try:
                return _run_stage_inner(run, stage, args, log)
            finally:
                # 阶段 span 收尾富化：失败留痕/熔断摘要/条目计数随 span 进 trace
                st = run.get("stages", {}).get(stage, {})
                failed_n = len(st.get("failed_entries", []))
                skipped_n = sum(1 for e in run["entries"]
                                if run["artifacts"].get(e["url"], {}).get("skipped"))
                sp["failed_entries"], sp["skipped"] = failed_n, skipped_n
                sp["processed"] = int(run.get("total", 0)) - failed_n - skipped_n
                if st.get("breaker"):
                    sp["breaker"] = str(st["breaker"])[:200]
    except (SystemExit, Exception):
        status = "failed"
        raise
    finally:
        st = run.get("stages", {}).get(stage, {})
        failed_n = len(st.get("failed_entries", []))
        skipped_n = sum(1 for e in run["entries"]
                        if run["artifacts"].get(e["url"], {}).get("skipped"))
        observability.metric_stage(stage, status, time.monotonic() - t0,
                                   ok=int(run.get("total", 0)) - failed_n - skipped_n,
                                   failed=failed_n, skipped=skipped_n)
        run.setdefault("span_log", []).extend(log.spans)  # state.json 的 spans 摘要来源
        with _runs_file_lock():
            _runs_merge(run)
        _STAGE_LOGS.pop(run["run_id"], None)
        observability.close_run(run["run_id"])
        observability.flush()  # __stage 子进程短命，退出前排空 sink 队列


def _run_stage_inner(run: dict, stage: str, args: dict, log: "RunLog") -> None:
    tmp = Path(run["tmp"])
    tmp.mkdir(parents=True, exist_ok=True)  # 目录丢了（系统清理/上轮异常中断）会让 download 直接 ENOENT
    failed: list[str] = []
    last_sig, streak, breaker = "", 0, ""
    cb_threshold = int(ENV.get("V2MD_CB_THRESHOLD", "3"))
    for idx, entry in enumerate(run["entries"], 1):
        art = run["artifacts"].setdefault(entry["url"], {})
        if art.get("skipped"):
            print(f"    ⏭ 条目 {idx}/{len(run['entries'])} 已处理过（state 幂等），跳过")
            continue
        print(f"    · 条目 {idx}/{len(run['entries'])}：{stage}")
        # 多条目：每个条目一个子目录。共享 tmp 时 `frames/`、`asr/` 的 mkdir 会撞 FileExistsError
        # （实测 17P 合集只有条目 1 能出帧，2~17 全灭），且 g_001.jpg 这类帧名会跨条目互相覆盖
        etmp = tmp if len(run["entries"]) == 1 else tmp / f"e{idx:02d}"
        etmp.mkdir(parents=True, exist_ok=True)
        err_msg = ""
        eid = entry.get("id") or entry.get("url", "")[-24:]
        with log.span(f"entry {idx}/{len(run['entries'])} {stage} {eid}") as sp:
            try:
                _STAGE_FNS[stage](run, entry, art, etmp, args)
            except SystemExit as exc:
                err_msg = str(exc)
            except Exception as exc:  # 阶段异常不炸 worker
                # 1000 字符上限：ffmpeg 等 stderr 头 300 字全是版本横幅，真错误在末尾
                err_msg = redact(str(exc))[-1000:]
            if err_msg:
                sp["force_status"] = "failed"  # span 记失败，但条目级失败不阻断其余条目
                sp["error"] = err_msg[:200]
        if not err_msg:
            streak, last_sig = 0, ""  # 成功即重置：熔断只数「连续」同因失败
            continue
        failed.append(str(idx))
        print(f"    ⚠ 条目异常：{err_msg}")
        sig = _err_sig(err_msg)
        streak, last_sig = (streak + 1, last_sig) if sig == last_sig else (1, sig)
        limit = 2 if _CB_GLOBAL_PAT.search(sig) else cb_threshold
        if streak >= limit:
            breaker = f"连续 {streak} 条同一错误，判定全局故障：{err_msg[:120]}"
            print(f"    🔌 熔断：{breaker}（剩余 {len(run['entries']) - idx} 条不再白跑）")
            log.info("circuit_breaker", stage=stage, sig=sig, streak=streak)
            observability.metric_cb(stage)
            failed += [str(i) for i in range(idx + 1, len(run["entries"]) + 1)
                       if not run["artifacts"].get(run["entries"][i - 1]["url"], {}).get("skipped")]
            break
    # 关键阶段全灭 → 终止（否则下游会拿空产物硬跑，工作流应显式看到失败）
    ok_field = _CRITICAL_STAGE_OK.get(stage)
    all_failed = False
    if ok_field:
        non_skipped = [a for e in run["entries"]
                       if not (a := run["artifacts"].get(e["url"], {})).get("skipped")]
        all_failed = bool(non_skipped) and not any(a.get(ok_field) for a in non_skipped)
    run["stages"][stage] = {"ts": datetime.now().isoformat(timespec="seconds")}
    if failed:
        # 部分失败必须留痕：job 只回 status=done 会把「少几篇」吞掉（14P 合集丢 2 集，无人知）
        run["stages"][stage]["failed_entries"] = failed
        run.setdefault("partial_failures", {})[stage] = failed
        log.info("partial_failure", stage=stage, entries=",".join(failed))
        print(f"PARTIAL-FAIL {stage} 条目 {'、'.join(failed)}")
    if breaker:
        run["stages"][stage]["breaker"] = breaker
    with _runs_file_lock():
        _runs_merge(run)  # 先落盘再报全灭：已成功条目的产物（如已下载的视频）不该随 raise 一起丢
    if breaker:
        # 熔断 = 全局故障：哪怕前面有成功条目也终止（同因错误继续跑下游只会出垃圾产物）
        raise SystemExit(f"阶段 {stage} 熔断：{breaker}")
    if all_failed:
        raise SystemExit(f"阶段 {stage} 全部条目失败，终止后续阶段")



def cmd_stage(run_id: str, stage: str, args_json: str) -> None:
    """单阶段手动排障入口：`python -m video2md __stage <run_id> <stage> '<args JSON>'`。"""
    if stage not in STAGES:
        print(f"❌ 未知阶段 {stage}（可选 {','.join(STAGES)}）")
        sys.exit(1)
    run = _load_runs().get(run_id)
    if not run:
        print(f"❌ run {run_id} 不存在")
        sys.exit(1)
    try:
        args = json.loads(args_json or "{}")
    except json.JSONDecodeError:
        args = {}
    _run_stage(run, stage, args)
    print(f"✅ 阶段 {stage} 完成")



# ---- 阶段函数（图节点/CLI/__stage 手动排障共用；args 为 dict 便于子进程 JSON 传参）----
_STAGE_FNS: dict[str, callable] = {}




def _art_info(**fields) -> None:
    """阶段产物计数 → trace info 点（挂当前条目 span 下；无活跃 RunLog 时静默）。"""
    log = current_log()
    if log:
        log.info("artifacts", **fields)



def _stage(fn):
    _STAGE_FNS[fn.__name__.removeprefix("_stage_")] = fn
    return fn



@_stage
def _stage_download(run, entry, art, tmp, args):
    if art.get("video") or art.get("images"):
        print("    ⏭ 已下载，跳过")
        return
    if entry.get("media_type") == "image_post" or entry.get("images"):
        # 图文帖：没有视频文件，产物是图集；后续各阶段对 images 分支处理
        imgs = download_images(entry, tmp)
        art["images"] = [str(p) for p in imgs]
        art["uploader"] = entry.get("uploader", "")
        art["duration"] = float(entry.get("duration") or 0.0)
        art["fp"] = hashlib.sha256(entry["url"].encode()).hexdigest()[:16]  # 无文件可指纹，用 URL 合成
        print(f"    🖼 图文 {len(imgs)} 张图（无视频，走图集管线）")
        return
    url = ensure_scheme(entry["url"])  # 防御：runs.json 里可能躺着补 scheme 前建的历史 run
    if url.startswith(("http://", "https://")):
        video, uploader = download_entry(entry, tmp)
        art["uploader"] = uploader
    else:
        video = Path(url).expanduser().resolve()
        if not video.exists():
            raise SystemExit(f"文件不存在: {video}")
        art["uploader"] = ""
    info = probe(video)
    art.update(video=str(video), duration=info["duration"], width=info["width"],
               height=info["height"], fp=video_fingerprint(video))
    _art_info(stage="download", duration_s=int(info["duration"]))
    print(f"    ▶ {video.name} · {fmt_hms(info['duration'])} · {info['width']}x{info['height']}")



@_stage
def _stage_transcribe(run, entry, art, tmp, args):
    if art.get("segs"):
        print("    ⏭ 已转写，跳过")
        return
    if art.get("images"):
        # 图文帖无音频：占位一条空 seg 过下游门禁（merge_timeline 忽略空 text），
        # 转录本在 OCR 阶段由画面文字重建
        art["segs"] = [{"start": 0.0, "end": 0.0, "text": ""}]
        art["conf"], art["dropped"] = 0.0, 0
        art["transcript"] = ""
        print("    ⏭ 图文帖无音频，跳过转写（转录本将由 OCR 合成）")
        return
    if not art.get("video"):
        # 正常路径 download 失败即 fail 节点终止（到不了这里）；保留防御：幂等重入/
        # __stage 手动乱序执行时给出人话而不是 KeyError 'video'
        raise SystemExit("无视频产物（download 未成功），跳过转写")
    segs, conf, dropped = transcribe(Path(art["video"]), args.get("model") or ENV.get("V2MD_WHISPER_MODEL") or "turbo",
                                     args.get("language") or "zh", tmp,
                                     args.get("remote") or ENV.get("V2MD_REMOTE", ""))
    if not segs:
        raise SystemExit("转写结果为空（未识别到语音段）")
    art.update(segs=segs, conf=conf, dropped=dropped)
    # 语音通道转录本立刻物化：调用方在 ocr 阶段结束前就能读到（ocr 的
    # merge_timeline 双通道版是后期覆盖，不物化则早期读者拿到空）
    art["transcript"], _ = merge_timeline(segs, [])
    _art_info(stage="transcribe", segs=len(segs), conf=round(conf, 3),
              transcript_chars=len(art["transcript"]))
    print(f"    🎙 {len(segs)} 段（幻觉过滤剔除 {dropped}）· 置信度 {conf:.2f}")



@_stage
def _stage_frames(run, entry, art, tmp, args):
    if art.get("candidates"):
        print("    ⏭ 已抽候选帧，跳过")
        return
    if art.get("images"):
        # 图文帖：图集本身就是全部候选帧（t 用序号合成，仅作排序锚）
        cands = [(float(i), Path(p)) for i, p in enumerate(art["images"], 1)]
        art["candidates"] = [[t, str(f.relative_to(tmp))] for t, f in cands]
        art["ocr_engine"] = args.get("ocr") or ("remote" if ENV.get("V2MD_OCR_REMOTE") else "vision")
        print(f"    🎞 图文直通候选 {len(cands)} 张")
        return
    if not art.get("video"):
        raise SystemExit("无视频产物（download 未成功），跳过抽帧")
    video = Path(art["video"])
    pool = frame_pool_size(int(args.get("max_frames") or 60),
                           args.get("ocr") or ("remote" if ENV.get("V2MD_OCR_REMOTE") else "vision"))
    cands = extract_frame_candidates(video, pool, art["duration"], tmp)
    art["candidates"] = [[t, str(f.relative_to(tmp))] for t, f in cands]  # 相对路径：JSON 可序列化
    art["ocr_engine"] = args.get("ocr") or ("remote" if ENV.get("V2MD_OCR_REMOTE") else "vision")
    _art_info(stage="frames", candidates=len(cands))
    print(f"    🎞 候选帧 {len(cands)} 张（池 {pool}）")



def _cand_path(rel: str, tmp: Path) -> Path:
    """候选帧路径解析（兼容旧 run）。

    每条目子目录改造（tmp/eNN）之前建的 run，把候选帧相对路径写成相对 **run 根**
    （如 `frames/g_001.jpg`），而新代码按 `tmp/eNN/frames/...` 去找 → 全部落空。
    先按本条目子目录找，找不到再回退 run 根；两者都无则返回原路径（让调用方照旧报错）。
    """
    p = tmp / rel
    if p.exists() or not re.fullmatch(r"e\d+", tmp.name):
        return p
    legacy = tmp.parent / rel
    return legacy if legacy.exists() else p



@_stage
def _stage_ocr(run, entry, art, tmp, args):
    if art.get("ocr_raw") is not None:
        print("    ⏭ 已 OCR，跳过")
        return
    if not art.get("candidates") or not art.get("segs"):
        raise SystemExit("缺候选帧或语音段（frames/transcribe 未成功），跳过 OCR")
    cands = [(t, _cand_path(rel, tmp)) for t, rel in art["candidates"]]
    # 注意用 `or` 而不是 .get(k, 默认)：调用方未填时传的是空串，空串不走默认值，
    # 会把 ocr 静默切到 gemini（实测：整批 OCR 命中 0，因为空串 != "vision"）
    # remote：V2MD_OCR_REMOTE 指向 GPU 路由（或任意参考服务端）时默认外移
    ocr_engine = args.get("ocr") or ("remote" if ENV.get("V2MD_OCR_REMOTE") else "vision")
    ocr_items, ocr_raw = ocr_candidates(cands, ocr_engine)
    art["ocr_items"] = [[t, txt, c] for t, txt, c in ocr_items]
    art["ocr_raw"] = [[t, txt, c, err] for t, txt, c, err in ocr_raw]
    slug = sanitize_filename(Path(art["video"]).stem)[:32] if not art.get("images") \
        else sanitize_filename(entry.get("title") or entry.get("id") or "图文")[:32]
    art["cand_manifest"] = manifest_of(name_candidates(cands, slug), ocr_items)
    if art.get("images"):
        # 图文帖：转录本 = OCR 文字按图序合成（【图N】标记，不带伪时间戳）
        seen_txt: set[str] = set()
        olines: list[str] = []
        for t, txt, _c in ocr_items:
            key = _NORM(txt)
            if not txt or key in seen_txt:
                continue
            seen_txt.add(key)
            olines.append(f"【图{int(t)}】{txt.replace(chr(10), ' / ')}")
        transcript = "\n".join(olines)
    else:
        transcript, _ev = merge_timeline(art["segs"], ocr_items)
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        raise SystemExit(f"转录本 {len(transcript)} 字符超上限 {MAX_TRANSCRIPT_CHARS}")
    art["transcript"] = transcript
    _art_info(stage="ocr", ocr_hits=len(ocr_items), transcript_chars=len(transcript))
    print(f"    🔍 OCR 命中 {len(ocr_items)}/{len(cands)} · 时间线 {len(transcript)} 字符")



@_stage
def _stage_select(run, entry, art, tmp, args):
    """发布选中帧：优先 LLM 选帧结果（args.frames/selections），空则本地启发式兜底。"""
    if not art.get("candidates") or not art.get("segs"):
        raise SystemExit("缺候选帧或语音段（frames/transcribe 未成功），跳过选帧")
    cands = [(t, _cand_path(rel, tmp)) for t, rel in art["candidates"]]
    if art.get("images"):
        slug = sanitize_filename(entry.get("title") or entry.get("id") or "图文")[:32]
    else:
        slug = sanitize_filename(Path(art["video"]).stem)[:32]
    named_list = name_candidates(cands, slug)
    name_of = {p: n for _t, p, n in named_list}
    chosen = args.get("frames")
    if not chosen and args.get("selections"):
        idx = str(run["entries"].index(entry) + 1)  # 清单头「条目 N/M」的序号，LLM 按序号回选
        for s in args["selections"]:
            key = str(s.get("entry", "")).strip()
            if key in (idx, entry["url"], str(entry.get("playlist_index", ""))):
                chosen = s.get("frames")
                break
        if not chosen and len(args["selections"]) == 1 and run["total"] == 1:
            chosen = args["selections"][0].get("frames")
    picked = None
    if art.get("images"):
        picked = named_list  # 图文帖全选直通：每张图都是内容本身，无需 LLM 挑帧
    elif chosen:
        names = {str(f.get("name", "")).strip() for f in chosen if str(f.get("name", "")).strip()}
        picked = [(t, p, n) for t, p, n in named_list if n in names]
        if not picked:
            # LLM 选帧解析不出（空选/全部不在候选清单）→ 降级本地启发式兜底，而不是整段失败。
            # 实测：单条 61 分钟视频上选帧 LLM 返回空，整篇笔记无图。
            print("    ⚠ LLM 选帧结果为空或全部不在候选清单内，降级为本地启发式选帧")
    if not picked:
        sel = select_content_frames([(t, p) for t, p, _n in named_list],
                                    [tuple(x) for x in art.get("ocr_raw", [])],
                                    art["segs"], int(args.get("max_frames") or 60))
        picked = [(t, p, name_of[p]) for t, p in sel]
    picked.sort(key=lambda x: x[0])
    manifest, published = publish_named(picked, [tuple(x) for x in art.get("ocr_items", [])])
    art.update(selected=[{"name": n, "t": t} for t, _p, n in picked],
               manifest=manifest, published=[list(x) for x in published])
    _art_info(stage="select", selected=len(picked))
    print(f"    🖼 选中 {len(picked)} 帧入库 attachments/")



@_stage
def _stage_clips(run, entry, art, tmp, args):
    if art.get("images"):
        # 图文帖无视频可剪：clips 置空但标记完成（note 阶段按 images 分支跳过节段嵌入）
        art["clips"] = {}
        art["clips_done"] = True
        print("    ⏭ 图文帖无视频，跳过节段剪辑")
        return
    published = [tuple(x) for x in art.get("published", [])]
    if not published:
        print("    ⏭ 无选中帧，跳过节段")
        art["clips_done"] = True
        return
    if (ENV.get("V2MD_CLIPS_REMOTE") or "").lower() in ("colab", "colab-cli"):
        raise SystemExit("V2MD_CLIPS_REMOTE=colab 已抽离（2026-09-21）：Colab 归 GPU 算力池接管——"
                         "起 ~/Desktop/开发项目/gpu_router 后改 V2MD_CLIPS_REMOTE=http://127.0.0.1:9700")
    if (ENV.get("V2MD_CLIPS_REMOTE") or "").startswith(("http://", "https://")):
        # GPU 算力池/参考服务端：远端失败即本条目失败，异常上抛、不回退本地——
        # 本地 CPU 重剪是整条管线最大头（实测占 97min/条），静默回退会把批次拖垮；
        # 故障转移由池子在 provider 间完成，全局故障由本层熔断收口。
        clips = remote_cut_clips(published, Path(art["video"]), art["duration"],
                                 segs=art.get("segs"), tmp=tmp,
                                 base=ENV["V2MD_CLIPS_REMOTE"],
                                 direct_url=entry.get("direct_url") or "")
    else:
        clips = cut_clip_files(published, Path(art["video"]), art["duration"],
                               segs=art.get("segs"))
    art["clips"] = clips
    art["clips_done"] = True
    ok = sum(1 for c in clips.values() if c)
    _art_info(stage="clips", ok=ok, total=len(clips))
    print(f"    🎬 {ok}/{len(clips)} 段节段入库 attachments/")



@_stage
def _stage_note(run, entry, art, tmp, args):
    """接收 LLM 整理正文 + 元数据 → 校图链/插节段/写盘/索引/转录本。

    图内路径由 graph 逐条目传 body/meta；`args["entry"]`/`args["notes"]` 是
    `__stage` 手动排障时的分发形态（指定目标条目/一次多正文），其余条目直接跳过
    （不是失败、不写盘）。
    """
    want = str(args.get("entry", "")).strip()
    if want:
        idx = str(run["entries"].index(entry) + 1)
        if want not in (idx, entry.get("url", ""), str(entry.get("playlist_index", ""))):
            return  # 本条目不归本 job
    if art.get("note") and not args.get("force_note"):
        print("    ⏭ 已写盘，跳过")
        return
    body, meta = args.get("body"), args.get("meta")
    if not art.get("segs"):
        # 硬门禁：无转录本绝不写盘，杜绝「空转录本」类废稿再次入库
        # （正常路径 transcribe 失败即 fail 终止；防御幂等重入/手动乱序执行）
        raise SystemExit("无转录本（transcribe 未成功），拒绝写盘")
    if not body and args.get("notes"):
        idx = str(run["entries"].index(entry) + 1)  # __stage 手动路径按条目序号分发正文（多条目合集各写一篇）
        for n in args["notes"]:
            key = str(n.get("entry", "")).strip()
            if key in (idx, entry["url"], str(entry.get("playlist_index", ""))):
                body, meta = n.get("body"), n.get("meta")
                break
        if not body and len(args["notes"]) == 1 and run["total"] == 1:
            body, meta = args["notes"][0].get("body"), args["notes"][0].get("meta")
    if not body:
        raise SystemExit("note 阶段缺 body（LLM 整理正文未产出？）")
    # 质量门禁：拒答/空壳绝不写盘（否则 vault 会被污染）
    if looks_like_refusal(body):
        raise SystemExit(f"正文疑似 LLM 拒答/空壳（{len(body.strip())} 字），拒绝写盘：{body.strip()[:60]}")
    tr_len = len((art.get("transcript") or "").strip())
    if tr_len < MIN_NOTE_TRANSCRIPT_CHARS:
        raise SystemExit(f"转录本过短（{tr_len} 字 < {MIN_NOTE_TRANSCRIPT_CHARS}），疑似片头/片尾，跳过写盘")
    meta = meta if isinstance(meta, dict) else {}
    # 归一化：逐条目链路里 meta 直接来自 LLM JSON，缺 tags/summary 会让 render_note 抛 KeyError
    meta = {"title": str(meta.get("title") or "")[:100],
            "tags": [str(t).strip() for t in (meta.get("tags") or []) if str(t).strip()][:8],
            "summary": str(meta.get("summary") or "")[:300]}
    published = [tuple(x) for x in art.get("published", [])]
    valid_embeds = {n for n, _t in published}
    body = sanitize_embeds(body, valid_embeds)
    if not art.get("images") and not args.get("no_clips"):
        clips = art.get("clips")
        if clips is None:
            if published and (ENV.get("V2MD_CLIPS_REMOTE") or "").strip():
                # 远端剪辑开启时本地补剪等同回退 CPU 重活——拒绝静默烧，显式失败让人修根因
                raise SystemExit("clips 阶段无节段产物且已禁用本地回退（V2MD_CLIPS_REMOTE 已配远端），"
                                 "拒绝写盘前本地补剪：请先重跑 clips 阶段")
            clips = cut_clip_files(published, Path(art["video"]),
                                   art["duration"], segs=art.get("segs"))
        if clips:
            body = insert_clip_embeds(body, clips)
            art["clips"] = clips
    toc = extract_toc(body)
    video = Path(art["video"]) if art.get("video") else Path(art["images"][0])  # 图文帖：首图作命名锚
    dur_str = fmt_hms(art.get("duration") or 0.0)
    title = (meta.get("title") or "").strip() or (entry.get("title") or video.stem)
    tpath = write_transcript(title, art["transcript"])
    entry_url = entry.get("url", "")
    if entry_url.startswith(("http://", "https://")):
        link_text = video.stem if art.get("video") else (entry.get("title") or "抖音图文")
        source_line = (f"[{link_text}]({entry_url})"
                       + (f"（{art.get('uploader', '')}）" if art.get("uploader") else "")
                       + (f" · P{entry.get('playlist_index')}" if entry.get("playlist_index") else ""))
    else:
        source_line = f"`{video.name}`（本地文件）"
    md = render_note(body, meta, {
        "video": video, "dur_str": dur_str, "conf": art.get("conf", 0.0),
            "ocr_engine": art.get("ocr_engine", "vision"),
            "n_frames": len(published), "n_ocr": len(art.get("ocr_items", [])),
            "model": args.get("model") or "turbo", "transcript_name": tpath.name,
            "source_line": source_line, "source_url": entry_url,
            "playlist_title": entry.get("playlist_title", ""),
            "playlist_index": entry.get("playlist_index", None),
            "is_image_post": bool(art.get("images")),
        }, toc)
    note = write_note(md, title, entry.get("playlist_title", ""))
    append_index(title, dur_str, (meta.get("summary") or "")[:120], entry.get("playlist_title", ""))
    art.update(note=str(note), title=title, md=md, transcript_path=str(tpath))
    _art_info(stage="note", note_bytes=len(md), title=title[:60])
    print(f"    📝 {note}")




def _run_outputs(run: dict, which: str) -> str:
    """LLM 节点取数的聚合文本：transcript / candidates（多条目用「===== 条目 N/M：url =====」分节，LLM 靠它分篇）。"""
    key = {"transcript": "transcript", "candidates": "cand_manifest"}[which]
    blocks = [f"===== 条目 {idx}/{run['total']}：{e['url']} =====\n{txt}"
              for idx, e in enumerate(run["entries"], 1)
              if (txt := run["artifacts"].get(e["url"], {}).get(key))]
    return "\n\n".join(blocks)

"""帧抽取/去模糊/去重 + vision OCR + 内容选帧 + 双通道时间线。"""
from __future__ import annotations

import math
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import artsink
from .config import BLUR_SHARP_MIN, ENV, FRAME_DEDUP_EPS, FRAME_MAE_MAX, LOW_CONF
from .util import fmt_ts


# ---------------------------------------------------------------- ②b frames
def _frame_mae(a, b) -> float:
    """两帧缩到 160x90 灰度后的平均绝对差：判连续重复帧（文字级差异能保留）。"""
    la = a.convert("L").resize((160, 90)).tobytes()
    lb = b.convert("L").resize((160, 90)).tobytes()
    return sum(abs(x - y) for x, y in zip(la, lb)) / len(la)



def _sharpness(im) -> float:
    from PIL import ImageFilter, ImageStat
    w, h = im.size
    center = im.convert("L").crop((int(w * .2), int(h * .2), int(w * .8), int(h * .8)))
    return ImageStat.Stat(center.filter(ImageFilter.FIND_EDGES)).mean[0]



def frame_pool_size(max_frames: int, ocr: str) -> int:
    """候选帧池大小：Vision 本地免费 / 远端 GPU 批量 → max_frames×3（上限 120）；
    付费按次引擎 → max_frames；V2MD_FRAME_POOL 显式覆盖。池越大选帧信息越多。"""
    env = int(ENV.get("V2MD_FRAME_POOL", "0") or "0")
    if env > 0:
        return min(200, env)
    return min(120, max_frames * (3 if ocr in ("vision", "remote") else 1))



def extract_frame_candidates(video: Path, pool_size: int, dur: float, tmp: Path) -> list[tuple[float, Path]]:
    """候选帧池：均匀网格（全程覆盖底线）∪ 场景切变（内容切换处增密）。

    池大小 pool_size（见 frame_pool_size），网格+场景两路合计不超池上限。
    注意：这里**不做**内容无关的时间去重——场景检测对「画面出现/消失」两个切边
    都触发，消失边往往是无内容的过渡帧（纯背景），去重会误杀内容帧；内容挑选
    （文字/清晰度评分 + MAE 去重）留给 OCR 之后的 select_content_frames。
    只剔模糊；同一时刻被两路重复采到（<0.4s）才去重，避免双份 OCR 白烧。
    """
    from PIL import Image
    fdir = tmp / "frames"
    fdir.mkdir(parents=True, exist_ok=True)  # 复用已有目录（重跑/多条目时不该炸）
    grid_n = max(8, pool_size * 2 // 3)   # 均匀网格：动画再密也有全程兜底
    scene_n = pool_size - grid_n          # 场景切变：翻页/动画切换处增密
    # 均匀网格
    grid: list[tuple[float, Path]] = []
    fps = min(1.0, grid_n / max(dur, 1))
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(video), "-vf", f"fps={fps:.4f},format=yuvj420p",
         "-vsync", "vfr", "-frames:v", str(grid_n), "-q:v", "2", str(fdir / "g_%03d.jpg")],
        capture_output=True, text=True)
    if p.returncode == 0:
        files = sorted(fdir.glob("g_*.jpg"))
        grid = [(i / fps, f) for i, f in enumerate(files)]
    # 场景切变
    scene: list[tuple[float, Path]] = []
    if scene_n >= 1:
        # format=yuvj420p：ffmpeg ≥7.1 的 mjpeg 编码器要求 full-range，否则直接拒启
        vf = "select='gt(scene,0.25)',format=yuvj420p,showinfo"
        p2: subprocess.CompletedProcess[str] | None = None
        for vsync in (("-fps_mode", "vfr"), ("-vsync", "vfr")):  # 新旧 ffmpeg 语法回退
            p2 = subprocess.run(
                ["ffmpeg", "-hide_banner", "-i", str(video), "-vf", vf, *vsync,
                 "-frames:v", str(scene_n), "-q:v", "2", str(fdir / "s_%03d.jpg")],
                capture_output=True, text=True)
            if p2.returncode == 0:
                break
        assert p2 is not None  # 回退循环至少执行一次
        times = [float(m) for m in re.findall(r"pts_time:([\d.]+)", p2.stderr)]
        files = sorted(fdir.glob("s_*.jpg"))
        if times and files:
            scene = list(zip(times[:len(files)], files))
    # 合并：只去同一时刻的重复采样（网格/场景两路落在同一瞬间），其余全留
    cands = sorted(scene + grid, key=lambda x: x[0])
    merged: list[tuple[float, Path]] = []
    for t, f in cands:
        if not merged or t - merged[-1][0] >= FRAME_DEDUP_EPS:
            merged.append((t, f))
    # 质量门禁：剔模糊（全被剔时保底留最清晰 5 张）
    scored = [(t, f, _sharpness(Image.open(f))) for t, f in merged]
    sharp_enough = [x for x in scored if x[2] >= BLUR_SHARP_MIN]
    pool = sharp_enough if len(sharp_enough) >= 3 else \
        sorted(scored, key=lambda x: -x[2])[:5]
    pool.sort(key=lambda x: x[0])
    print(f"  🎞 候选帧 {len(pool)} 张（网格 {len(grid)} + 场景 {len(scene)}，"
          f"剔模糊/去重 {len(merged) - len(pool)}）")
    return [(t, f) for t, f, _s in pool]



# ---------------------------------------------------------------- ②c OCR
def ocr_vision(path: Path) -> tuple[str, float]:
    import Foundation
    import Vision
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setRecognitionLanguages_(["zh-Hans", "en-US"])
    req.setUsesLanguageCorrection_(True)
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
        Foundation.NSURL.fileURLWithPath_(str(path)), None)
    ok, _err = handler.performRequests_error_([req], None)
    if not ok:
        return "", 0.0
    texts, confs = [], []
    for obs in (req.results() or []):
        cands = obs.topCandidates_(1)
        if cands:
            texts.append(cands[0].string())
            confs.append(cands[0].confidence())
    if not texts:
        return "", 0.0
    return "\n".join(texts), sum(confs) / len(confs)





# ---------------------------------------------------------------- ③ merge
_NORM_RE = re.compile(r"[\s，。、；：？！“”‘’()（）\[\]【】《》.,;:?!\-—·]+")


def _norm(s: str) -> str:
    return _NORM_RE.sub("", s)



def select_content_frames(cands: list[tuple[float, Path]], ocr_raw: list,
                          segs: list[dict], max_frames: int) -> list[tuple[float, Path]]:
    """按「画面文字 + 语音段」双信号从候选池选出 ≤ max_frames 帧（内容选帧）。

    CLI 直跑路径用（工作流路径由 LLM 对照转录本选帧，这里是离线兜底）：
    - 画面文字（OCR 非空且置信达标）= 主信号：幻灯片/公式/图表文字即「内容帧」
    - 按 whisper 语音段分桶 → 每个话题至少 1 帧配额（保证各章都有配图）
    - 桶内评分 = 有文字(3) + 文字量(≤1) + 语音段对齐(≤1) + 清晰度(≤0.5)
    - 跨桶 MAE 去重：同一张幻灯片跨多段只留一张
    - 内容帧不足 → 无文字清晰帧（纯图/动画）按评分补位
    """
    from PIL import Image
    if not cands:
        return []
    if not segs:
        segs = [{"start": -1e9, "end": 1e9, "text": ""}]
    ocr_by_t: dict[int, tuple[str, float]] = {}
    for t, txt, conf, _e in ocr_raw:
        ocr_by_t[round(t)] = ((txt or "").strip(), float(conf or 0.0))

    def seg_of(t: float):
        for s in segs:
            if s["start"] <= t <= s["end"]:
                return s
        return None

    scored = []
    for t, path in cands:
        txt, conf = ocr_by_t.get(round(t), ("", 0.0))
        txt = re.sub(r"\s+", " ", txt).strip()
        has_text = bool(txt) and conf >= LOW_CONF
        s = seg_of(t)
        align = 1.0 if (s and (s.get("text") or "").strip()) else (0.3 if s else 0.0)
        sharp = min(1.0, _sharpness(Image.open(path)) / 5.0)
        score = (3.0 if has_text else 0.0) + min(1.0, len(_norm(txt)) / 80.0) + align + 0.5 * sharp
        scored.append({"t": t, "path": path, "txt": txt, "has_text": has_text,
                       "score": score, "seg": s})

    text_frames = [x for x in scored if x["has_text"]]
    no_text = sorted([x for x in scored if not x["has_text"]], key=lambda x: -x["score"])
    picked: list[dict] = []
    seen: list = []

    def try_pick(x: dict) -> bool:
        im = Image.open(x["path"])
        if any(_frame_mae(im, y) < FRAME_MAE_MAX for y in seen):
            return False
        picked.append(x)
        seen.append(im)
        return True

    # 有文字帧按语音段分桶 → 每桶配额（每个话题至少一张）
    buckets: dict[int, list] = {}
    for x in text_frames:
        buckets.setdefault(id(x["seg"]), []).append(x)
    if buckets:
        quota = max(1, math.ceil(max_frames / len(buckets)))
        for bucket in buckets.values():
            for x in sorted(bucket, key=lambda z: -z["score"])[:quota]:
                try_pick(x)
            if len(picked) >= max_frames:
                break
    # 无文字清晰帧补位（纯图/公式图/动画关键帧）
    if len(picked) < max_frames:
        for x in no_text:
            if len(picked) >= max_frames:
                break
            try_pick(x)
    # 极端去重兜底：仍为空则按评分直接取
    if not picked:
        for x in sorted(scored, key=lambda z: -z["score"]):
            if len(picked) >= max_frames:
                break
            try_pick(x)
    picked.sort(key=lambda x: x["t"])
    return [(x["t"], x["path"]) for x in picked]



def merge_timeline(segs: list[dict], ocr_items: list[tuple[float, str, float]]) -> tuple[str, list]:
    events = [("voice", s["start"], s["text"], s["conf"]) for s in segs if s["text"]]
    events += [("frame", t, txt, c) for t, txt, c in ocr_items if txt]
    events.sort(key=lambda e: e[1])
    seen_ocr: set[str] = set()
    lines: list[str] = []
    for kind, t, txt, conf in events:
        if kind == "frame":
            key = _norm(txt)
            if not key or key in seen_ocr:
                continue
            seen_ocr.add(key)
            lines.append(f"[{fmt_ts(t)}] 【画面】{txt.replace(chr(10), ' / ')}")
        else:
            mark = " ⚠" if conf < LOW_CONF else ""
            lines.append(f"[{fmt_ts(t)}] 【语音】{txt}{mark}")
    return "\n".join(lines), events



# ---------------------------------------------------------------- ③b 帧落库 + 正文清理
def name_candidates(cands: list[tuple[float, Path]], slug: str) -> list[tuple[float, Path, str]]:
    """候选帧分配 vault 文件名 `<slug>-fNN-<mmss>.jpg`（时间序，时间戳可溯源）。

    命名在 OCR 阶段就定死：工作流 LLM 选帧节点引用的是同一套名字，
    select 只挑子集，发布时按名拷贝，不重命名。
    """
    named = []
    for i, (t, fpath) in enumerate(sorted(cands, key=lambda x: x[0]), 1):
        named.append((t, fpath, f"{slug}-f{i:02d}-{fmt_ts(t).replace(':', '')}.jpg"))
    return named



def manifest_of(named: list[tuple[float, Path, str]], ocr_items: list) -> str:
    """帧清单文本：`文件名 [mm:ss] OCR: …`（喂 LLM 选帧/改写用；时间戳可溯源）。"""
    ocr_by_t = {round(t): re.sub(r"\s+", " ", txt) for t, txt, _c in ocr_items}
    return "\n".join(f"{name} [{fmt_ts(t)}] OCR: {ocr_by_t.get(round(t), '—')[:80]}"
                     for t, _p, name in named)



def publish_named(named: list[tuple[float, Path, str]], ocr_items: list) -> tuple[str, list]:
    """把选中帧（已带名字）拷入 vault attachments，生成帧清单（喂给 LLM 选图）。

    已存在则跳过（幂等）；返回 (manifest 文本, [(文件名, mm:ss)])。
    """
    dest_dir = artsink.attach_dir()  # vault=ATTACH_DIR 常量；deliver=本 run outbox/attachments
    dest_dir.mkdir(parents=True, exist_ok=True)
    published = []
    for t, fpath, name in named:
        dest = dest_dir / name
        if not dest.exists():
            shutil.copyfile(fpath, dest)
        published.append((name, fmt_ts(t)))
    return manifest_of(named, ocr_items), published



# ---------------------------------------------------------------- dry-run mock


# ---------------------------------------------------------------- main process
def ocr_remote(frames: list[tuple[float, Path]], base: str, client=None) -> list[list]:
    """整条目候选帧一次 multipart 批量 POST 给 GPU 路由 /ocr（参考服务端 RapidOCR），
    返回 ocr_raw 同形 [[t, txt, conf, err]]。整体失败抛异常（路由已做过跨家故障转移，
    这里不再兜底），与「不静默回退本地」政策一致。"""
    import httpx

    opened = [p.open("rb") for _t, p in frames]
    http = client or httpx.Client(timeout=httpx.Timeout(30.0, read=1800.0))
    try:
        files = [("images", (p.name, fh, "image/jpeg")) for (_t, p), fh in zip(frames, opened)]
        print(f"  ☁️ 远端 GPU OCR → {base.rsplit('//', 1)[-1]}（{len(frames)} 帧一批）")
        import time as _time
        _t0 = _time.monotonic()
        r = http.post(f"{base.rstrip('/')}/ocr", files=files)
        from .runs import bump_usage  # P2-2：远程调用墙钟（上界口径）
        bump_usage(gpu_s=_time.monotonic() - _t0)
        if r.status_code != 200:
            raise RuntimeError(f"远程 OCR 失败 HTTP {r.status_code}: {r.text[:300]}")
        payload = r.json()
    finally:
        for fh in opened:
            fh.close()
        if client is None:
            http.close()
    by_name = {str(x.get("name", "")): x for x in payload.get("results", [])}
    raw: list[list] = []
    for t, p in frames:
        hit = by_name.get(p.name)
        if hit is None:
            raw.append([t, "", 0.0, "远端结果缺此帧"])
        else:
            raw.append([t, str(hit.get("text") or ""), float(hit.get("conf") or 0.0),
                        (str(hit.get("err"))[:200] if hit.get("err") else None)])
    if payload.get("provider"):
        print(f"  ✅ 远端 OCR 完成（provider={payload['provider']}）")
    return raw



def ocr_candidates(frames: list[tuple[float, Path]], ocr: str,
                   base: str = "", client=None) -> tuple[list, list]:
    """候选帧 OCR：返回 (ocr_items 达标项, ocr_raw 全量含失败/空)。

    ocr_raw 喂给内容选帧（需要知道哪些候选没文字），ocr_items 进时间线/清单。
    ocr="remote" 走 GPU 路由批量接口（V2MD_OCR_REMOTE 或显式 base）；其余值固定本地
    Vision（gemini 路径已摘除，入参保留兼容）。"""
    if ocr == "remote":
        base = base or ENV.get("V2MD_OCR_REMOTE", "")
        if not base:
            raise SystemExit("ocr=remote 但 V2MD_OCR_REMOTE 未配置")
        raw = ocr_remote(frames, base, client=client)
        ocr_items = [(t, txt, c) for t, txt, c, _e in raw if txt and c >= LOW_CONF]
        return ocr_items, raw
    raw = []
    ocr_fn = ocr_vision

    def one(item):
        t, fpath = item
        try:
            txt, c = ocr_fn(fpath)
            return t, txt, c, None
        except Exception as e:
            return t, "", 0.0, e

    with ThreadPoolExecutor(max_workers=min(6, max(1, len(frames)))) as pool:
        results = list(pool.map(one, frames))
    for i, (t, txt, c, err) in enumerate(results, 1):
        if err:
            print(f"    ⚠ 帧 {i} OCR 失败: {err}")
        raw.append([t, txt, c, str(err)[:200] if err else None])
        if i % 10 == 0:
            print(f"    … OCR {i}/{len(frames)}")
    ocr_items = [(t, txt, c) for t, txt, c, _e in raw if txt and c >= LOW_CONF]
    return ocr_items, raw

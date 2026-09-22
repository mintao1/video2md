"""视频节段剪辑：本地 ffmpeg（CPU 兜底）或远端 HTTP（GPU 算力池/参考服务端）。

2026-09-21 起 Colab 直联（会话剪辑 worker）已抽离到 GPU 算力池项目
（~/Desktop/开发项目/gpu_router 的 colab_machinery.py）——本仓只认 /clips HTTP 契约。
"""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from .config import ATTACH_DIR, CLIP_SPAN, ENV



def _clip_window(t: float, dur: float, segs: list[dict] | None) -> tuple[float, float]:
    """节段窗口 = 帧 ±CLIP_SPAN ∪ 所在 whisper 语音段 ±1s（单边 clamp ±CLIP_SPAN×2）；
    语音段太短/帧落在空白 → 回退纯 ±CLIP_SPAN。本地与远端剪辑共用同一窗口算法。"""
    s0, s1 = max(0, t - CLIP_SPAN), min(dur, t + CLIP_SPAN)
    for s in segs or []:
        if s["start"] <= t <= s["end"]:
            s0 = min(s0, max(0, s["start"] - 1.0))
            s1 = max(s1, min(dur, s["end"] + 1.0))
            break
    s0, s1 = max(s0, t - CLIP_SPAN * 2), min(s1, t + CLIP_SPAN * 2)
    if s1 - s0 < 4:
        s0, s1 = max(0, t - CLIP_SPAN), min(dur, t + CLIP_SPAN)
    return s0, s1



def _clip_tasks(published: list, dur: float, segs: list[dict] | None) -> dict[str, tuple[str, Path, float, float] | str]:
    """{帧名: (节段名, 目标路径, s0, s1) | 已存在的节段名}——节段幂等（已存在直接引用）。"""
    ts_by_name = {}
    for name, mmss in published:
        mm, ss = mmss.split(":")
        ts_by_name[name] = int(mm) * 60 + int(ss)
    tasks: dict[str, tuple[str, Path, float, float] | str] = {}
    for fname, t in ts_by_name.items():
        clip = fname.replace("-f", "-c").replace(".jpg", ".mp4")
        dest = ATTACH_DIR / clip
        if dest.exists():
            tasks[fname] = clip
            continue
        s0, s1 = _clip_window(t, dur, segs)
        if s1 - s0 < 2:
            continue
        tasks[fname] = (clip, dest, s0, s1)
    return tasks



def remote_cut_clips(published: list, video: Path, dur: float,
                     segs: list[dict] | None, tmp: Path, base: str,
                     direct_url: str = "", client=None) -> dict[str, str | None]:
    """剪节段走 GPU 算力池/参考服务端（POST {base}/clips）：本地算 spec → 上传源片或
    direct_url → 收回 tar（含 _result.json）→ 成功段入 attachments。

    单段失败丢该段（None），整体失败抛异常、不回退本地（本地 CPU 重剪是整条管线
    最大头，实测占 97min/条；静默回退会把批次拖垮——全局故障由熔断收口）。
    """
    import httpx  # 延迟导入：CLI 直跑纯本地路径时不起 httpx 连接

    tasks = _clip_tasks(published, dur, segs)
    result: dict[str, str | None] = {k: v for k, v in tasks.items() if isinstance(v, str)}
    todo = [(k, v) for k, v in tasks.items() if not isinstance(v, str)]
    if not todo:
        return result
    prefer_remote = bool(direct_url) and (ENV.get("V2MD_CLIPS_SOURCE") or "").lower() == "remote"
    specs = [{"out": v[0], "s0": v[2], "s1": v[3]} for _k, v in todo]
    data = {"spec": json.dumps({"clips": specs}),
            "direct_url": direct_url if prefer_remote else ""}
    http = client
    own = False
    if http is None:
        http = httpx.Client(timeout=httpx.Timeout(30.0, read=3600.0))
        own = True
    try:
        url = f"{base.rstrip('/')}/clips"
        if prefer_remote:
            print("    ☁️ 源片由远端直连 CDN 下载（省本地上行）…")
            r = http.post(url, data=data)
        else:
            size_mb = video.stat().st_size / 1e6
            with video.open("rb") as fh:
                r = http.post(url, data=data,
                              files={"video": (video.name, fh, "video/mp4")},
                              timeout=httpx.Timeout(30.0, read=3600.0,
                                                    write=max(600.0, size_mb * 8)))
        if r.status_code != 200:
            raise RuntimeError(f"远程剪辑失败 HTTP {r.status_code}: {r.text[:300]}")
    finally:
        if own:
            http.close()
    in_dir = tmp / "clips_http_in"
    in_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, bool] = {}
    with tarfile.open(fileobj=io.BytesIO(r.content)) as tf:
        for m in tf.getmembers():
            if m.name == "_result.json":
                results = {k: bool(v) for k, v in
                           json.loads(tf.extractfile(m).read().decode()).items()}
            elif m.isfile() and "/" not in m.name and m.name.endswith(".mp4"):
                with (in_dir / m.name).open("wb") as fh:
                    fh.write(tf.extractfile(m).read())
    ok_n = 0
    for fname, spec in todo:
        src = in_dir / spec[0]
        if src.exists() and src.stat().st_size > 0:
            shutil.move(str(src), str(ATTACH_DIR / spec[0]))
            result[fname] = spec[0]
            ok_n += 1
        else:
            result[fname] = None
    print(f"  ✅ 远程剪辑（{sum(1 for v in result.values() if v)}/{len(tasks)} 段入库）")
    return result



def cut_clip_files(published: list, video: Path, dur: float,
                   segs: list[dict] | None = None) -> dict[str, str | None]:
    """对每个已发布帧，从原视频剪节段（含音频）入 attachments；返回 {帧名: 节段名}（失败为 None）。

    节段窗口 = 帧 ±CLIP_SPAN ∪ 所在 whisper 语音段（单边 clamp ±CLIP_SPAN×2；
    语音段太短/落在空白时回退 ±CLIP_SPAN）——音频恰好完整讲完该画面内容
    （「节段按文字内容」；本地剪辑与远端剪辑共用此窗口语义）。

    剪辑失败只丢节段不丢帧；节段幂等（已存在直接引用）。
    """
    ts_by_name = {}
    for name, mmss in published:
        mm, ss = mmss.split(":")
        ts_by_name[name] = int(mm) * 60 + int(ss)

    def clip_window(t: float) -> tuple[float, float]:
        return _clip_window(t, dur, segs)

    # 先并行切出所有缺失节段，再按正文顺序插回 embed；避免逐个 ffmpeg 串行等待
    tasks = {}
    for fname, t in ts_by_name.items():
        clip = fname.replace("-f", "-c").replace(".jpg", ".mp4")
        dest = ATTACH_DIR / clip
        if dest.exists():
            tasks[fname] = clip
            continue
        s0, s1 = clip_window(t)
        if s1 - s0 < 2:
            continue
        tasks[fname] = (clip, dest, s0, s1)

    def cut_one(item):
        fname, spec = item
        if isinstance(spec, str):
            return fname, spec
        clip, dest, s0, s1 = spec
        p = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-ss", f"{s0:.2f}", "-t", f"{s1 - s0:.2f}", "-i", str(video),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-c:a", "aac", "-movflags", "+faststart", str(dest)],
            capture_output=True, text=True)
        if p.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            return fname, None
        return fname, clip

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(tasks)))) as pool:
        for fname, clip in pool.map(cut_one, list(tasks.items())):
            tasks[fname] = clip
    return {fname: tasks.get(fname) for fname in ts_by_name}



def insert_clip_embeds(body: str, clips_map: dict[str, str | None]) -> str:
    """把每个帧嵌入行下插入配套视频节段嵌入行；返回插入后的正文（计数打印由调用方做）。"""
    out = []
    for line in body.split("\n"):
        out.append(line)
        m = re.fullmatch(r"!\[\[attachments/([^\]]+\.jpg)\]\]", line.strip())
        if not m:
            continue
        clip = clips_map.get(m.group(1))
        if clip:
            out.append(f"![[attachments/{clip}]]")
    return "\n".join(out)

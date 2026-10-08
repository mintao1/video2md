"""kaggle provider 的 v2md 能力实现：三段内核模板 + 产物解析（业务归本插件）。

推送/轮询机械已上收 gpu_router tasking 契约（providers/kaggle.py 的四原语 +
run_task 状态机）——本模块只剩「业务三件套」：
- 模板：_KERNEL_ASR / _KERNEL_OCR / _KERNEL_CLIPS（产物约定
  /kaggle/working/out.json + status.json）
- 组输入：files dict（bytes 或 Path——视频传 Path 流式，不进内存）
- 解析：_read_out / clips.tar 解包回填

命名连续性：模板硬编码 IN = "/kaggle/input/v2md-io"，与数据集 slug 尾段联动——
v2md-io / v2md-job 由 _bind 在装载期钉回（内核壳默认已改通用 gpu-router-io/job）。
"""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path


def _bind(p) -> None:
    """装载期钉回 v2md 命名（内核壳默认是通用的 gpu-router-io/job）。

    凭证缺失时静默跳过——health 会报缺凭证、provider 不进候选，路由照常起；
    不能在装载期炸掉整个路由（与内核「配置错误响亮早失败」的边界：缺凭证是
    运行态问题，不是配置错误）。
    """
    try:
        user = p._creds_user()
    except RuntimeError:
        return
    p.dataset = p.dataset or f"{user}/v2md-io"
    p.kernel = p.kernel or f"{user}/v2md-job"


def _spec(script: str) -> dict:
    """tasking spec：内核脚本全文 + 平台资源标志（schema 归壳，见 providers/kaggle.py）。"""
    return {"script": script, "gpu": True, "internet": True}


_WANT_STD = ("out.json", "status.json")
_WANT_CLIPS = ("clips.tar", "clips_result.json", "status.json")


def _read_out(arts: dict[str, bytes]) -> dict:
    st = arts.get("status.json")
    if st:
        s = json.loads(st)
        if s.get("state") == "error":
            raise RuntimeError(f"Kaggle 内核报错: {str(s.get('error'))[:300]}")
    out = arts.get("out.json")
    if out is None:
        raise RuntimeError("Kaggle 产物缺 out.json（内核可能没跑完）")
    return json.loads(out)


# ---------------------------------------------------------------- 内核脚本模板
# 三模板共享产物约定：/kaggle/working/out.json（结果）+ status.json（done/error）
_K_HEADER = '''
import json
import os
import subprocess
import sys

def _pip(*pkgs):
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", *pkgs], check=True)

def _status(**kw):
    json.dump(kw, open("/kaggle/working/status.json", "w"))

IN = "/kaggle/input/v2md-io"
OUT = "/kaggle/working"
'''

_KERNEL_ASR = _K_HEADER + '''
try:
    _pip("faster-whisper>=1.1")
    from faster_whisper import WhisperModel

    args = json.load(open(f"{IN}/v2md_args.json"))
    m = WhisperModel(args.get("model") or "turbo", device="cuda", compute_type="float16")
    kw = dict(language=None if args.get("language") == "auto" else args.get("language"),
              condition_on_previous_text=False, vad_filter=True,
              initial_prompt="\\u4ee5\\u4e0b\\u662f\\u666e\\u901a\\u8bdd\\u7684\\u7b80\\u4f53\\u4e2d\\u6587\\u5185\\u5bb9\\u3002"
                  if (args.get("language") or "").startswith("zh") else None)
    try:
        from faster_whisper import BatchedInferencePipeline
        segs_iter, info = BatchedInferencePipeline(model=m).transcribe(
            f"{IN}/in.m4a", batch_size=16, **kw)
    except Exception:
        segs_iter, info = m.transcribe(f"{IN}/in.m4a", **kw)
    segments = [{"start": s.start, "end": s.end, "text": s.text.strip(),
                 "no_speech_prob": s.no_speech_prob, "compression_ratio": s.compression_ratio,
                 "avg_logprob": s.avg_logprob} for s in segs_iter]
    json.dump({"model": args.get("model") or "turbo", "language": info.language,
               "duration": info.duration, "segments": segments}, open(f"{OUT}/out.json", "w"))
    _status(state="done", segments=len(segments))
except Exception as exc:
    _status(state="error", error=str(exc)[:500])
'''

_KERNEL_OCR = _K_HEADER + '''
try:
    _pip("rapidocr")
    import tarfile
    from rapidocr import RapidOCR

    with tarfile.open(f"{IN}/frames.tar") as tf:
        tf.extractall(f"{OUT}/frames_in")
    ocr = RapidOCR()
    results = []
    for name in sorted(os.listdir(f"{OUT}/frames_in")):
        try:
            out = ocr(os.path.join(f"{OUT}/frames_in", name))
            if hasattr(out, "txts"):
                txts = list(out.txts or [])
                scores = [float(s) for s in (out.scores or [])]
            else:
                items = (out[0] if isinstance(out, tuple) else out) or []
                txts = [str(it[1]) for it in items]
                scores = [float(it[2]) for it in items]
            results.append({"name": name, "text": "\\n".join(txts),
                            "conf": (sum(scores) / len(scores) if scores else 0.0)})
        except Exception as exc:
            results.append({"name": name, "text": "", "conf": 0.0, "err": str(exc)[:200]})
    json.dump({"results": results}, open(f"{OUT}/out.json", "w"))
    _status(state="done", n=len(results))
except Exception as exc:
    _status(state="error", error=str(exc)[:500])
'''

_KERNEL_CLIPS = _K_HEADER + '''
try:
    import glob
    import shutil
    import tarfile
    import urllib.request
    import concurrent.futures as cf

    job = json.load(open(f"{IN}/clip_job.json"))
    clips = job["clips"]
    _status(state="running", note="prep")

    src = f"{OUT}/clip-src.mp4"
    up = f"{IN}/clip-src.mp4"
    if os.path.exists(up):
        shutil.copy(up, src)
    elif job.get("direct_url"):
        _status(state="running", note="cdn-download")
        req = urllib.request.Request(job["direct_url"], headers={
            "User-Agent": "Mozilla/5.0", "Referer": "https://www.douyin.com/"})
        with urllib.request.urlopen(req, timeout=120) as r, open(src, "wb") as f:
            shutil.copyfileobj(r, f)
    if not os.path.exists(src) or os.path.getsize(src) == 0:
        raise RuntimeError("源视频缺失")

    ff = shutil.which("ffmpeg")
    if not ff:
        urllib.request.urlretrieve(
            "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
            f"{OUT}/ff.tar.xz")
        subprocess.run(["tar", "-xf", f"{OUT}/ff.tar.xz", "-C", OUT],
                       check=True, capture_output=True)
        ff = glob.glob(f"{OUT}/ffmpeg-*-amd64-static/ffmpeg")[0]
        os.chmod(ff, 0o755)

    def probe(enc):
        q = subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i", "color=black:s=320x240:d=1:r=25",
                            "-f", "lavfi", "-i", "sine=frequency=440:d=1",
                            *enc, "-c:a", "aac", "/tmp/probe.mp4"],
                           capture_output=True, text=True)
        return q.returncode == 0 and os.path.exists("/tmp/probe.mp4")

    enc = None
    for cand in (["-c:v", "h264_nvenc", "-preset", "p4", "-qp", "26"],
                 ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]):
        if probe(cand):
            enc = cand
            break
    if not enc:
        raise RuntimeError("无可用 H.264 编码器")

    os.makedirs(f"{OUT}/clips_out", exist_ok=True)
    results = {}

    def cut(c):
        dest = f"{OUT}/clips_out/" + c["out"]
        q = subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                            "-ss", "%.2f" % c["s0"], "-t", "%.2f" % (c["s1"] - c["s0"]),
                            "-i", src, *enc, "-c:a", "aac", "-movflags", "+faststart", dest],
                           capture_output=True, text=True)
        ok = q.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0
        if not ok and os.path.exists(dest):
            os.remove(dest)
        return c["out"], ok

    with cf.ThreadPoolExecutor(max_workers=2) as pool:
        for name, ok in pool.map(cut, clips):
            results[name] = ok

    with tarfile.open(f"{OUT}/clips.tar", "w") as tf:
        for name, ok in results.items():
            if ok:
                tf.add(f"{OUT}/clips_out/" + name, arcname=name)
    json.dump(results, open(f"{OUT}/clips_result.json", "w"))
    _status(state="done", ok=sum(1 for v in results.values() if v), total=len(clips))
except Exception as exc:
    _status(state="error", error=str(exc)[:500])
'''


def transcribe(provider, audio: Path, model: str, language: str, tmp: Path) -> dict:
    with provider.lock:
        files = {
            "in.m4a": audio,   # Path：流式进数据集，不整读进内存
            "v2md_args.json": json.dumps({"model": model, "language": language}).encode(),
        }
        _tid, arts = provider.run_task(files, _spec(_KERNEL_ASR), list(_WANT_STD),
                                       max_wait_s=provider.max_wait, poll_interval_s=20)
        return _read_out(arts)


def ocr(provider, images: list[Path], tmp: Path) -> list[dict]:
    with provider.lock:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for p in images:
                tf.add(p, arcname=p.name)
        _tid, arts = provider.run_task({"frames.tar": buf.getvalue()},
                                       _spec(_KERNEL_OCR), list(_WANT_STD),
                                       max_wait_s=provider.max_wait, poll_interval_s=20)
        return _read_out(arts).get("results", [])


def cut_clips(provider, video: Path | None, direct_url: str, clips: list[dict],
              tmp: Path) -> tuple[dict[str, bool], Path]:
    with provider.lock:
        job = {"direct_url": direct_url if video is None else "", "clips": clips}
        files: dict[str, Path | bytes] = {
            "clip_job.json": json.dumps(job).encode()}
        if video is not None:
            files["clip-src.mp4"] = video   # Path：流式进数据集
        _tid, arts = provider.run_task(files, _spec(_KERNEL_CLIPS), list(_WANT_CLIPS),
                                       max_wait_s=provider.max_wait, poll_interval_s=20)
        st = arts.get("status.json")
        if st:
            s = json.loads(st)
            if s.get("state") == "error":
                raise RuntimeError(f"Kaggle 剪辑内核报错: {str(s.get('error'))[:300]}")
        results = {}
        if arts.get("clips_result.json"):
            results = {k: bool(v) for k, v in json.loads(arts["clips_result.json"]).items()}
        od = tmp / "clips-kaggle-out"
        od.mkdir(exist_ok=True)
        if arts.get("clips.tar"):
            with tarfile.open(fileobj=io.BytesIO(arts["clips.tar"])) as tf:
                tf.extractall(od)
        for c in clips:
            results.setdefault(c["out"], (od / c["out"]).exists())
        return results, od

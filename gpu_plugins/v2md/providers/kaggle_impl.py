"""kaggle provider 的 v2md 能力实现：三段内核模板 + 推送机械 + transcribe/cut_clips/ocr。

原 gpu_router 内核的内核模板、数据集推送、内核轮询、产物读取全部迁入（业务归
video2md）。gpu_router 侧 KaggleProvider 只留凭证/kg CLI/健康/配额语义；数据集与
内核默认名 v2md-io / v2md-job 归本插件，provider extra 的 dataset/kernel 可覆盖。
"""
from __future__ import annotations

import json
import shutil
import tarfile
from pathlib import Path



# ---------------------------------------------------------------- 推送机械（原内核方法迁入）
def _ensure_dataset(p, user: str) -> str:
    slug = p.dataset or f"{user}/v2md-io"
    if p._dataset_ready:
        return slug
    out = p._kg(["datasets", "list", "-m", "-s", "v2md-io"], timeout=120)
    p._dataset_exists = slug in out
    p._dataset_ready = True
    return slug


def _push_inputs(p, tmp: Path, files: list[Path]) -> str:
    """输入打成滚动数据集新版本（整包替换 = 数据集不随任务数膨胀）。返回 slug。"""
    import json as _json
    import shutil as _sh
    user = p._creds_user()
    slug = _ensure_dataset(p, user)
    stage = tmp / "dataset"
    stage.mkdir(exist_ok=True)
    for f in files:
        _sh.copy(f, stage / f.name)
    (stage / "dataset-metadata.json").write_text(_json.dumps({
        "title": slug.split("/", 1)[1], "id": slug,
        "licenses": [{"name": "CC0-1.0"}]}))
    if getattr(p, "_dataset_exists", False):
        p._kg(["datasets", "version", "-p", str(stage), "-m", "v2md job",
               "--dir-mode", "skip"], timeout=900)
    else:
        p._kg(["datasets", "create", "-p", str(stage), "--dir-mode", "skip"],
              timeout=900)
        p._dataset_exists = True
    return slug


def _run_kernel(p, tmp: Path, script: str, dataset_slug: str) -> Path:
    """push 内核 → 轮询 → 拉产物目录。返回产物目录路径。"""
    import json as _json
    import time as _time
    user = p._creds_user()
    slug = p.kernel or f"{user}/v2md-job"
    kdir = tmp / "kernel"
    kdir.mkdir(exist_ok=True)
    (kdir / "script.py").write_text(script)
    (kdir / "kernel-metadata.json").write_text(_json.dumps({
        "id": slug, "title": slug.split("/", 1)[1], "code_file": "script.py",
        "language": "python", "kernel_type": "script", "is_private": "true",
        "enable_gpu": "true", "enable_internet": "true",
        "dataset_sources": [dataset_slug],
        "competition_sources": [], "kernel_sources": []}))
    p._kg(["kernels", "push", "-p", str(kdir)], timeout=300)
    deadline = _time.time() + p.max_wait
    while _time.time() < deadline:
        _time.sleep(20)
        out = p._kg(["kernels", "status", slug], timeout=60)
        low = out.lower()
        if "complete" in low:
            out_dir = tmp / "kernel-out"
            out_dir.mkdir(exist_ok=True)
            p._kg(["kernels", "output", slug, "-p", str(out_dir)], timeout=900)
            return out_dir
        if "error" in low or "cancel" in low:
            raise RuntimeError(f"Kaggle 内核失败: {out.strip()[:200]}")
    raise RuntimeError(f"Kaggle 内核超时（>{int(p.max_wait)}s）")


def _read_out(out_dir: Path) -> dict:
    st_file = out_dir / "status.json"
    if st_file.exists():
        st = json.loads(st_file.read_text())
        if st.get("state") == "error":
            raise RuntimeError(f"Kaggle 内核报错: {str(st.get('error'))[:300]}")
    out_file = out_dir / "out.json"
    if not out_file.exists():
        raise RuntimeError("Kaggle 产物缺 out.json（内核可能没跑完）")
    return json.loads(out_file.read_text())


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
        args = tmp / "v2md_args.json"
        args.write_text(json.dumps({"model": model, "language": language}))
        slug = _push_inputs(provider, tmp, [audio, args])
        out_dir = _run_kernel(provider, tmp, _KERNEL_ASR, slug)
        return _read_out(out_dir)


def ocr(provider, images: list[Path], tmp: Path) -> list[dict]:
    with provider.lock:
        tar = tmp / "frames.tar"
        with tarfile.open(tar, "w") as tf:
            for p in images:
                tf.add(p, arcname=p.name)
        slug = _push_inputs(provider, tmp, [tar])
        out_dir = _run_kernel(provider, tmp, _KERNEL_OCR, slug)
        return _read_out(out_dir).get("results", [])


def cut_clips(provider, video: Path | None, direct_url: str, clips: list[dict],
              tmp: Path) -> tuple[dict[str, bool], Path]:
    with provider.lock:
        spec = tmp / "clip_job.json"
        spec.write_text(json.dumps(
            {"direct_url": direct_url if video is None else "", "clips": clips}))
        inputs = [spec]
        if video is not None:
            up = tmp / "clip-src.mp4"
            if video.resolve() != up.resolve():
                shutil.copy(video, up)
            inputs.append(up)
        slug = _push_inputs(provider, tmp, inputs)
        out_dir = _run_kernel(provider, tmp, _KERNEL_CLIPS, slug)
        st_file = out_dir / "status.json"
        if st_file.exists():
            st = json.loads(st_file.read_text())
            if st.get("state") == "error":
                raise RuntimeError(f"Kaggle 剪辑内核报错: {str(st.get('error'))[:300]}")
        results = {}
        res_file = out_dir / "clips_result.json"
        if res_file.exists():
            results = {k: bool(v) for k, v in
                       json.loads(res_file.read_text()).items()}
        od = tmp / "clips-kaggle-out"
        od.mkdir(exist_ok=True)
        tar = out_dir / "clips.tar"
        if tar.exists():
            with tarfile.open(tar) as tf:
                tf.extractall(od)
        for c in clips:
            results.setdefault(c["out"], (od / c["out"]).exists())
        return results, od

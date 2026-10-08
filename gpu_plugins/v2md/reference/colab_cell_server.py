"""
video2md 远程 GPU 服务（ASR + 剪辑 + OCR）—— 粘贴到 Google Colab 一个 cell 里整段运行。

前置：Colab 右上角运行时 → 更改运行时类型 → 选 T4 GPU。
运行结束（约 1–2 分钟装环境+载模型）会在输出里打印 🌍 公网地址，把它填到本地：
  直接当远程服务：.env 写 V2MD_REMOTE=https://xxxx.trycloudflare.com
  或交给路由调度：gpu_router.json 加一家 {"name":"colab-tunnel","kind":"http","url":"https://…"}
  （该地址每次重启 cell 都会变；Colab 免费档有时长限制，过期就重跑本 cell）
注意：地址是公开的（quick tunnel 无鉴权），不要提交到 git；只跑媒体计算，无敏感数据。

本文件是同包 app.py 的单文件粘贴版 + cloudflared 隧道——两边逻辑有意保持一致：
改任何一边记得同步另一边。（本文件原居 dify-video2md/colab/，已随池子抽离至此）
"""

# 1) 依赖（faster-whisper 需 ≥1.1 才有 turbo；rapidocr 是 ONNX 中文 OCR；cloudflared 最新 linux-amd64）
import subprocess
import sys

subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                "faster-whisper>=1.1", "rapidocr", "fastapi", "uvicorn",
                "python-multipart", "nest-asyncio"], check=True)
subprocess.run("curl -sL -o /usr/local/bin/cloudflared "
               "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 "
               "&& chmod +x /usr/local/bin/cloudflared", shell=True, check=True)

# 2) 模型（T4 显存 16G：turbo float16 ≈ 3G，余量充足；要更准改 "large-v3"）
MODEL_NAME = "turbo"
from faster_whisper import WhisperModel

model = WhisperModel(MODEL_NAME, device="cuda", compute_type="float16")
from rapidocr import RapidOCR

ocr_engine = RapidOCR()
print(f"✅ 模型就绪：{MODEL_NAME}（GPU）+ RapidOCR")

# 3) 服务
import glob
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import threading
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import nest_asyncio
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

app = FastAPI()

_OUT_NAME_RE = re.compile(r"^[\w.\-]+\.mp4$")

@app.get("/ping")
def ping():
    return {"ok": True, "model": MODEL_NAME, "ocr": "rapidocr"}

@app.post("/transcribe")
async def transcribe(audio: UploadFile = File(...), model: str = Form(""),
                     model_name: str = Form(""), language: str = Form("zh")):
    p = f"/tmp/{uuid.uuid4().hex}{audio.filename or '.m4a'}"
    open(p, "wb").write(await audio.read())
    try:
        segs_iter, info = model.transcribe(
            p, language=None if language == "auto" else language,
            condition_on_previous_text=False, vad_filter=True,
            initial_prompt="以下是普通话的简体中文内容。" if (language or "").startswith("zh") else None)
        segments = [{"start": s.start, "end": s.end, "text": s.text.strip(),
                     "no_speech_prob": s.no_speech_prob, "compression_ratio": s.compression_ratio,
                     "avg_logprob": s.avg_logprob} for s in segs_iter]
        return {"model": MODEL_NAME, "language": info.language,
                "duration": info.duration, "segments": segments}
    finally:
        os.unlink(p)

def _ffmpeg(workdir: Path) -> str:
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    cached = glob.glob("/content/ffmpeg-*-amd64-static/ffmpeg")
    if cached:
        return cached[0]
    urllib.request.urlretrieve(
        "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
        workdir / "ff.tar.xz")
    subprocess.run(["tar", "-xf", str(workdir / "ff.tar.xz"), "-C", "/content/"],
                   check=True, capture_output=True)
    ff = glob.glob("/content/ffmpeg-*-amd64-static/ffmpeg")[0]
    os.chmod(ff, 0o755)
    return ff

def _probe_encoder(ff: str) -> list:
    for enc in (["-c:v", "h264_nvenc", "-preset", "p4", "-qp", "26"],
                ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]):
        q = subprocess.run([ff, "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i", "color=black:s=320x240:d=1:r=25",
                            "-f", "lavfi", "-i", "sine=frequency=440:d=1",
                            *enc, "-c:a", "aac", "/tmp/_probe.mp4"],
                           capture_output=True, text=True)
        if q.returncode == 0 and os.path.exists("/tmp/_probe.mp4"):
            return enc
    raise RuntimeError("无可用 H.264 编码器（nvenc/x264 探测全灭）")

@app.post("/clips")
async def clips(spec: str = Form(...), direct_url: str = Form(""),
                video: UploadFile | None = File(None)):
    try:
        specs = json.loads(spec).get("clips", [])
    except (json.JSONDecodeError, AttributeError):
        raise HTTPException(400, "spec 不是合法 JSON")
    bad = [c.get("out", "") for c in specs if not _OUT_NAME_RE.match(str(c.get("out", "")))]
    if bad:
        raise HTTPException(400, f"非法产物名: {bad[:3]}")
    if video is None and not direct_url:
        raise HTTPException(400, "video 与 direct_url 至少给一样")
    workdir = Path(tempfile.mkdtemp(prefix="v2md-clips-"))
    try:
        src = workdir / "clip-src.mp4"
        if video is not None:
            open(src, "wb").write(await video.read())
        else:
            req = urllib.request.Request(direct_url, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://www.douyin.com/"})
            with urllib.request.urlopen(req, timeout=120) as r, open(src, "wb") as f:
                shutil.copyfileobj(r, f)
        if not src.exists() or src.stat().st_size == 0:
            raise RuntimeError("源视频缺失/下载失败")
        ff = _ffmpeg(workdir)
        enc = _probe_encoder(ff)
        out_dir = workdir / "clips_out"
        out_dir.mkdir(exist_ok=True)
        results = {}

        def cut(c):
            dest = out_dir / c["out"]
            q = subprocess.run(
                [ff, "-y", "-hide_banner", "-loglevel", "error",
                 "-ss", "%.2f" % c["s0"], "-t", "%.2f" % (c["s1"] - c["s0"]),
                 "-i", str(src), *enc, "-c:a", "aac", "-movflags", "+faststart", str(dest)],
                capture_output=True, text=True)
            ok = q.returncode == 0 and dest.exists() and dest.stat().st_size > 0
            if not ok:
                try:
                    os.remove(dest)
                except OSError:
                    pass
            return c["out"], ok

        with ThreadPoolExecutor(max_workers=2) as pool:
            for name, ok in pool.map(cut, specs):
                results[name] = ok
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for name, ok in results.items():
                p = out_dir / name
                if ok and p.exists():
                    tf.add(p, arcname=name)
            payload = json.dumps(results).encode()
            info = tarfile.TarInfo("_result.json")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        return Response(content=buf.getvalue(), media_type="application/x-tar")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

@app.post("/ocr")
async def ocr(images: list[UploadFile] = File(...)):
    if not images:
        raise HTTPException(400, "images 为空")

    def one(up):
        name = Path(up.filename or "f.jpg").name
        p = f"/tmp/{uuid.uuid4().hex}-{name}"
        try:
            open(p, "wb").write(up.file.read())
            out = ocr_engine(p)
            if hasattr(out, "txts"):
                txts = list(out.txts or [])
                scores = [float(s) for s in (out.scores or [])]
            else:
                items = (out[0] if isinstance(out, tuple) else out) or []
                txts = [str(it[1]) for it in items]
                scores = [float(it[2]) for it in items]
            return {"name": name, "text": "\n".join(txts),
                    "conf": (sum(scores) / len(scores) if scores else 0.0)}
        except Exception as exc:
            return {"name": name, "text": "", "conf": 0.0, "err": str(exc)[:200]}
        finally:
            try:
                os.unlink(p)
            except OSError:
                pass

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, images))
    return {"results": results}

def _tunnel():
    """cloudflared quick tunnel：无需账号，从输出里抓 trycloudflare.com 地址。"""
    p = subprocess.Popen(["/usr/local/bin/cloudflared", "tunnel", "--url",
                          "http://localhost:8321", "--no-autoupdate"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in p.stdout:
        if "trycloudflare.com" in line:
            url = line.strip().split()[-1]
            print(f"\n🌍 公网地址（复制回本地）：{url}\n", flush=True)

nest_asyncio.apply()
threading.Thread(target=_tunnel, daemon=True).start()
uvicorn.run(app, host="0.0.0.0", port=8321)

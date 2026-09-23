"""参考 GPU 服务端：/ping /transcribe /clips /ocr —— 部署到任何能跑 Python+GPU 的平台，
把它的 URL 填进 gpu_router.json 的 http provider 即接入路由。

部署形态：
- Modal：用同目录 modal_app.py 包装（模型 Volume 缓存，T4）
- 国内云（阿里云 PAI-EAS/函数计算 GPU、百度、超算容器）：
    pip install fastapi "uvicorn[standard]" python-multipart faster-whisper>=1.1 rapidocr
    python app.py            # 或 uvicorn app:app --host 0.0.0.0 --port 8321
- Colab：用同包 colab_cell_server.py（同逻辑的单文件粘贴版 + cloudflared 隧道；
  两边逻辑有意保持同步——改任何一边记得同步另一边）

设计要点：
- 模型懒加载 + 进程级缓存；有 CUDA 走 float16，没有退 cpu/int8（本地调试用）。
- OCR 用 RapidOCR（ONNX，PP-OCR 同源中文模型，免装 paddle 重型依赖，CPU 就够）。
- /clips 编码器实测探测（nvenc → x264 回退），与 video2md 的 Colab worker 同逻辑；
  剪辑产物名白名单校验，无路径注入面。
"""
from __future__ import annotations

import glob
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response



MODEL_NAME = os.environ.get("V2MD_REF_MODEL", "turbo")  # T4 16G：turbo float16 ≈3G 余量充足

_WHISPER = None

_OCR = None

_OUT_NAME_RE = re.compile(r"^[\w.\-]+\.mp4$")



def _get_whisper():
    global _WHISPER
    if _WHISPER is None:
        from faster_whisper import WhisperModel
        try:
            _WHISPER = WhisperModel(MODEL_NAME, device="cuda", compute_type="float16")
        except Exception as exc:
            print(f"⚠️ CUDA 不可用，whisper 退 CPU/int8：{exc}")
            _WHISPER = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
    return _WHISPER



def _get_ocr():
    global _OCR
    if _OCR is None:
        from rapidocr import RapidOCR
        _OCR = RapidOCR()
    return _OCR



def _ffmpeg(workdir: Path) -> str:
    """系统 ffmpeg 优先；没有就拉 johnvansickle 静态构建（linux x86_64，自带 nvenc）。"""
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    cached = glob.glob(str(workdir.parent / "ffmpeg-*-amd64-static" / "ffmpeg"))
    if cached:
        return cached[0]
    urllib.request.urlretrieve(
        "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
        workdir / "ff.tar.xz")
    subprocess.run(["tar", "-xf", str(workdir / "ff.tar.xz"), "-C", str(workdir.parent)],
                   check=True, capture_output=True)
    ff = glob.glob(str(workdir.parent / "ffmpeg-*-amd64-static" / "ffmpeg"))[0]
    os.chmod(ff, 0o755)
    return ff



def _probe_encoder(ff: str) -> list[str]:
    """编码器实测探测：列表里有 ≠ 能编码（参数集随构建漂移），各拿 1s 黑帧真编码一次。"""
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



def _fetch_source(workdir: Path, direct_url: str) -> Path:
    src = workdir / "clip-src.mp4"
    req = urllib.request.Request(direct_url, headers={"User-Agent": "Mozilla/5.0",
                                                      "Referer": "https://www.douyin.com/"})
    with urllib.request.urlopen(req, timeout=120) as r, open(src, "wb") as f:
        shutil.copyfileobj(r, f)
    if not src.exists() or src.stat().st_size == 0:
        raise RuntimeError("direct_url 源片下载失败")
    return src



def _run_clips(workdir: Path, specs: list[dict]) -> tuple[dict[str, bool], Path]:
    ff = _ffmpeg(workdir)
    enc = _probe_encoder(ff)
    src = workdir / "clip-src.mp4"
    out_dir = workdir / "clips_out"
    out_dir.mkdir(exist_ok=True)
    results: dict[str, bool] = {}

    def cut(c):
        dest = out_dir / c["out"]
        q = subprocess.run(
            [ff, "-y", "-hide_banner", "-loglevel", "error",
             "-ss", "%.2f" % c["s0"], "-t", "%.2f" % (c["s1"] - c["s0"]),
             "-i", str(src), *enc, "-c:a", "aac", "-movflags", "+faststart", str(dest)],
            capture_output=True, text=True)
        ok = q.returncode == 0 and dest.exists() and dest.stat().st_size > 0
        if not ok:
            dest.unlink(missing_ok=True)
        return c["out"], ok

    with ThreadPoolExecutor(max_workers=2) as pool:  # 2 vCPU 恰好打满
        for name, ok in pool.map(cut, specs):
            results[name] = ok
    return results, out_dir



def create_app() -> FastAPI:
    app = FastAPI(title="v2md-gpu-reference", docs_url=None, redoc_url=None)



    @app.get("/ping")
    def ping():
        return {"ok": True, "model": MODEL_NAME, "ocr": "rapidocr"}



    @app.post("/transcribe")
    def transcribe(audio: UploadFile = File(...), model: str = Form(""),
                   language: str = Form("zh")):
        p = f"/tmp/{uuid.uuid4().hex}-{audio.filename or 'in.m4a'}"
        with open(p, "wb") as fh:
            shutil.copyfileobj(audio.file, fh, length=1024 * 1024)
        try:
            segs_iter, info = _get_whisper().transcribe(
                p, language=None if language == "auto" else language,
                condition_on_previous_text=False, vad_filter=True,
                initial_prompt="以下是普通话的简体中文内容。"
                    if (language or "").startswith("zh") else None)
            segments = [{"start": s.start, "end": s.end, "text": s.text.strip(),
                         "no_speech_prob": s.no_speech_prob,
                         "compression_ratio": s.compression_ratio,
                         "avg_logprob": s.avg_logprob} for s in segs_iter]
            return {"model": MODEL_NAME, "language": info.language,
                    "duration": info.duration, "segments": segments}
        finally:
            os.unlink(p)



    @app.post("/clips")
    def clips(spec: str = Form(...), direct_url: str = Form(""),
              video: UploadFile | None = File(None)):
        try:
            specs = json.loads(spec).get("clips", [])
        except (json.JSONDecodeError, AttributeError):
            raise HTTPException(400, "spec 不是合法 JSON")
        bad = [c.get("out", "") for c in specs
               if not _OUT_NAME_RE.match(str(c.get("out", "")))]
        if bad:
            raise HTTPException(400, f"非法产物名: {bad[:3]}")
        if video is None and not direct_url:
            raise HTTPException(400, "video 与 direct_url 至少给一样")
        workdir = Path(tempfile.mkdtemp(prefix="v2md-clips-"))
        try:
            if video is not None:
                with open(workdir / "clip-src.mp4", "wb") as fh:
                    shutil.copyfileobj(video.file, fh, length=1024 * 1024)
            else:
                _fetch_source(workdir, direct_url)
            results, out_dir = _run_clips(workdir, specs)
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
    def ocr(images: list[UploadFile] = File(...)):
        if not images:
            raise HTTPException(400, "images 为空")
        engine = _get_ocr()

        def one(up: UploadFile) -> dict:
            name = Path(up.filename or "f.jpg").name
            p = f"/tmp/{uuid.uuid4().hex}-{name}"
            try:
                with open(p, "wb") as fh:
                    shutil.copyfileobj(up.file, fh, length=1024 * 1024)
                out = engine(p)
                # rapidocr 版本间返回形状不一：v3 带 txts/scores，旧版 (items, elapse)
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



    return app



app = create_app()



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8321")))

"""http provider 的 v2md 能力实现：transcribe / cut_clips / ocr（原 gpu_router 内核
http_generic.py 的方法体原样迁入，业务归 video2md）。

服务端契约 = reference/app.py 的 POST /transcribe（multipart audio）、POST /clips
（spec + 源片/direct_url → tar 含 _result.json）、POST /ocr（多图 multipart）。
"""
from __future__ import annotations

import io
import json
import tarfile
import time
from pathlib import Path

import httpx



def transcribe(p, audio: Path, model: str, language: str, tmp: Path) -> dict:
    with audio.open("rb") as fh:
        r = p.client().post("/transcribe",
                            files={"audio": (audio.name, fh, "audio/mp4")},
                            data={"model": model, "language": language})
    if r.status_code != 200:
        raise RuntimeError(f"{p.name} 转录失败 HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def cut_clips(p, video: Path | None, direct_url: str, clips: list[dict],
              tmp: Path) -> tuple[dict[str, bool], Path]:
    data = {"spec": json.dumps({"clips": clips}), "direct_url": direct_url}
    if video is not None:
        size_mb = video.stat().st_size / 1e6
        timeout = httpx.Timeout(20.0, read=p.read_timeout,
                                write=max(600.0, size_mb * 8), pool=60.0)
        with video.open("rb") as fh:
            r = p.client().post("/clips", data=data,
                                files={"video": (video.name, fh, "video/mp4")},
                                timeout=timeout)
    else:
        r = p.client().post("/clips", data=data)
    if r.status_code != 200:
        raise RuntimeError(f"{p.name} 剪辑失败 HTTP {r.status_code}: {r.text[:300]}")
    out_dir = tmp / f"clips-http-{int(time.time() * 1000)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, bool] = {}
    with tarfile.open(fileobj=io.BytesIO(r.content)) as tf:
        for m in tf.getmembers():
            if m.name == "_result.json":
                results = {k: bool(v) for k, v in
                           json.loads(tf.extractfile(m).read().decode()).items()}
            elif m.isfile() and "/" not in m.name and m.name.endswith(".mp4"):
                with (out_dir / m.name).open("wb") as fh:
                    fh.write(tf.extractfile(m).read())
    for c in clips:
        results.setdefault(c["out"], (out_dir / c["out"]).exists())
    return results, out_dir


def ocr(p, images: list[Path], tmp: Path) -> list[dict]:
    opened = [q.open("rb") for q in images]
    try:
        files = [("images", (q.name, fh, "image/jpeg")) for q, fh in zip(images, opened)]
        r = p.client().post("/ocr", files=files,
                            timeout=httpx.Timeout(20.0, read=p.read_timeout))
    finally:
        for fh in opened:
            fh.close()
    if r.status_code != 200:
        raise RuntimeError(f"{p.name} OCR 失败 HTTP {r.status_code}: {r.text[:300]}")
    return r.json().get("results", [])

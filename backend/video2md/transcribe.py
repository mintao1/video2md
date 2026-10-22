"""转录：Colab CLI GPU（colab-cli）、显式 HTTP 服务或本地 whisper。"""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import httpx

from .util import fmt_hms, pool_headers, redact, run


def remote_transcribe(video: Path, tmp: Path, base: str, model: str, language: str) -> dict:
    """音频 POST 给远端 GPU 转录服务（算力池或参考服务端，契约：POST {base}/transcribe），
    返回与 whisper CLI JSON 同形的 {"segments": [...]}。失败即失败——不静默回退几十分钟本地。"""
    audio = tmp / "remote-audio.m4a"
    run(["ffmpeg", "-y", "-i", str(video), "-vn", "-c:a", "aac", "-b:a", "64k", str(audio)])
    print(f"  ☁️ 远程 GPU 转录 → {redact(base)}")
    import time as _time
    _t0 = _time.monotonic()
    try:
        r = httpx.post(f"{base.rstrip('/')}/transcribe",
                       files={"audio": (audio.name, audio.read_bytes(), "audio/mp4")},
                       data={"model": model, "language": language},
                       headers=pool_headers(),
                       # Batch GPU: upload + provider's 3600s wait + artifact fetch.
                       timeout=httpx.Timeout(20, read=5400.0))
    finally:
        from .runs import bump_usage  # Remote wall clock includes failed waits.
        bump_usage(gpu_s=_time.monotonic() - _t0)
    if r.status_code != 200:
        raise RuntimeError(f"远程转录失败 HTTP {r.status_code}: {r.text[:300]}")
    return r.json()



def transcribe(video: Path, model: str, language: str, tmp: Path, remote: str = "") -> tuple[list[dict], float, int]:
    from .colab import remote_for_user

    remote = remote_for_user(remote)
    if remote and remote.lower() in ("colab", "colab-cli"):
        from .colab import transcribe_audio
        audio = tmp / "colab-audio.m4a"
        run(["ffmpeg", "-y", "-i", str(video), "-vn", "-c:a", "aac", "-b:a", "64k", str(audio)])
        data = transcribe_audio(audio, tmp, model, language)
    elif remote:
        data = remote_transcribe(video, tmp, remote, model, language)
        print(f"  ✅ 远程模型 {data.get('model', '?')} · 音频时长 {fmt_hms(data.get('duration') or 0)}"
              + (f"（provider={data['_provider']}）" if data.get("_provider") else ""))
    else:
        whisper_bin = shutil.which("whisper") or \
            "/Library/Frameworks/Python.framework/Versions/3.14/bin/whisper"
        out_dir = tmp / "asr"
        out_dir.mkdir(parents=True, exist_ok=True)  # 同上：复用而非独占
        cmd = [whisper_bin, str(video), "--model", model, "--output_format", "json",
               "--output_dir", str(out_dir), "--condition_on_previous_text", "False",
               "--fp16", "False"]
        if language and language != "auto":
            cmd += ["--language", language]
        if (language or "").startswith("zh"):
            # 简体中文偏置，降低繁体/异体字输出
            cmd += ["--initial_prompt", "以下是普通话的简体中文内容。"]
        print(f"  ⏳ whisper {model} 转写中（CPU 约 0.3–1× 实时，首次跑会下载模型）…")
        run(cmd)
        data = json.loads((out_dir / f"{video.stem}.json").read_text())
    segs: list[dict] = []
    conf_sum = dur_sum = 0.0
    dropped = 0
    for s in data.get("segments", []):
        ns = s.get("no_speech_prob", 0.0)
        cr = s.get("compression_ratio", 1.0)
        lp = s.get("avg_logprob", -0.5)
        if (ns > 0.6 and lp < -1.0) or cr > 2.4:  # 静音幻觉 / 复读机幻觉
            dropped += 1
            continue
        conf = math.exp(min(0.0, lp))
        segs.append({"start": s["start"], "end": s["end"],
                     "text": s["text"].strip(), "conf": conf})
        conf_sum += conf * (s["end"] - s["start"])
        dur_sum += s["end"] - s["start"]
    overall = conf_sum / dur_sum if dur_sum else 0.0
    return segs, overall, dropped

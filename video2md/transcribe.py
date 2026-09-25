"""转录：本地 whisper（CPU 兜底）或远端 HTTP（GPU 算力池/任意参考服务端）。

2026-09-21 起 Colab 直联机械（google-colab-cli 会话编排）已抽离到独立项目
~/Desktop/开发项目/gpu_router——本仓不再直接碰 Colab，远端一律走 HTTP 契约：
  V2MD_REMOTE=http://127.0.0.1:9700   → GPU 算力池（多平台动态路由，推荐）
  V2MD_REMOTE=https://xxx.trycloudflare.com → 裸参考服务端（隧道等）
  V2MD_REMOTE 置空                      → 本地 CPU whisper
"""
from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

import httpx

from .util import fmt_hms, pool_headers, redact, run

_MIGRATION_HINT = (
    "V2MD_REMOTE=colab 已抽离（2026-09-21）：Colab 归 GPU 算力池统一接管。"
    "起池子：cd ~/Desktop/开发项目/gpu_router && .venv/bin/python -m gpu_router；"
    "然后本仓 .env 改 V2MD_REMOTE=http://127.0.0.1:9700（置空则走本地 CPU whisper）")



def remote_transcribe(video: Path, tmp: Path, base: str, model: str, language: str) -> dict:
    """音频 POST 给远端 GPU 转录服务（算力池或参考服务端，契约：POST {base}/transcribe），
    返回与 whisper CLI JSON 同形的 {"segments": [...]}。失败即失败——不静默回退几十分钟本地。"""
    audio = tmp / "remote-audio.m4a"
    run(["ffmpeg", "-y", "-i", str(video), "-vn", "-c:a", "aac", "-b:a", "64k", str(audio)])
    print(f"  ☁️ 远程 GPU 转录 → {redact(base)}")
    import time as _time
    _t0 = _time.monotonic()
    r = httpx.post(f"{base.rstrip('/')}/transcribe",
                   files={"audio": (audio.name, audio.read_bytes(), "audio/mp4")},
                   data={"model": model, "language": language},
                   headers=pool_headers(),
                   timeout=httpx.Timeout(20, read=900.0))
    from .runs import bump_usage  # P2-2：远程调用墙钟（上界口径，含传输/排队）
    bump_usage(gpu_s=_time.monotonic() - _t0)
    if r.status_code != 200:
        raise RuntimeError(f"远程转录失败 HTTP {r.status_code}: {r.text[:300]}")
    return r.json()



def transcribe(video: Path, model: str, language: str, tmp: Path, remote: str = "") -> tuple[list[dict], float, int]:
    if remote and remote.lower() in ("colab", "colab-cli"):
        raise SystemExit(_MIGRATION_HINT)  # 响亮失败指路，不静默退化
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

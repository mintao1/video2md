"""colab-cli provider 的 v2md 能力实现：transcribe / cut_clips / ocr。

原 gpu_router/providers/colab_cli.py 的方法体迁入（会话机械在 colab_machinery.py，
本模块只做「锁 + 机械调用」的薄壳）。gpu_router 侧 ColabCliProvider 保留 health/
锁/配额语义，能力方法经 PROVIDER_IMPL 动态装载。
"""
from __future__ import annotations

from pathlib import Path

from . import colab_machinery as M



def transcribe(provider, audio: Path, model: str, language: str, tmp: Path) -> dict:
    with provider.lock:
        return M.colab_run_asr(audio, tmp, model, language)


def cut_clips(provider, video: Path | None, direct_url: str, clips: list[dict],
              tmp: Path) -> tuple[dict[str, bool], Path]:
    with provider.lock:
        out_dir = tmp / "clips_colab_out"
        out_dir.mkdir(parents=True, exist_ok=True)
        results = M.colab_exec_clips(video, direct_url, clips, tmp, out_dir)
    return results, out_dir


def ocr(provider, images: list[Path], tmp: Path) -> list[dict]:
    with provider.lock:
        return M.colab_run_ocr(images, tmp)

"""colab-cli provider 的 v2md 能力实现：transcribe / cut_clips / ocr + health/_bind。

原 gpu_router/providers/colab_cli.py 的方法体迁入（会话机械在 colab_machinery.py，
本模块只做「锁 + 机械调用」的薄壳）。gpu_router 侧 ColabCliProvider 只留平台壳，
能力方法与 token 体检经 PROVIDER_IMPL 装载（health(p) shadow 内核 fallback）；
provider.extra → 机械配置的注入走 _bind 钩子（原内核 _bind_impl 的 colab 特例迁入）。
"""
from __future__ import annotations

from pathlib import Path

from . import colab_machinery as M


def _bind(p) -> None:
    """装载钩子：provider.extra 透传给会话机械的模块级配置（gpu_router 无感知）。"""
    extra = getattr(p, "extra", None) or {}
    if extra.get("session"):
        M.SESSION = str(extra["session"])
    if extra.get("lease_file"):
        M.LEASE_FILE = str(extra["lease_file"])
    if extra.get("queue_url"):
        M.QUEUE_URL = str(extra["queue_url"])
    if extra.get("max_wait"):
        M.MAX_WAIT_S = int(extra["max_wait"])
    if extra.get("autostop_idle"):
        M.AUTOSTOP_IDLE_S = int(extra["autostop_idle"])


def health(p) -> tuple[bool, str]:
    """token 体检（绑定后 shadow 内核 ColabCliProvider.health 的兜底实现）。"""
    return M._colab_token_check()



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

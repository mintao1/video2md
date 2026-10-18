"""Direct Colab CLI execution for ASR, OCR and clips (no GPU router required)."""
from __future__ import annotations

import contextlib
import contextvars
import fcntl
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from gpu_plugins.v2md.providers import colab_machinery as machinery

from .config import ENV, PROJ

_LOCK = threading.RLock()
@dataclass
class _SessionUse:
    # LangGraph copies context into node threads. The object must be shared:
    # setting a boolean ContextVar inside a node never updates the caller's context.
    used: bool = False


_USE: contextvars.ContextVar[_SessionUse | None] = contextvars.ContextVar("colab_session_use", default=None)


def configured() -> bool:
    return any((ENV.get(key) or "").strip().lower() in ("colab", "colab-cli")
               for key in ("V2MD_REMOTE", "V2MD_OCR_REMOTE", "V2MD_CLIPS_REMOTE"))


@contextlib.contextmanager
def _session(wait: bool = True):
    path = Path(ENV.get("V2MD_COLAB_LOCK") or PROJ / "data" / "colab.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not _LOCK.acquire(blocking=wait):
        raise BlockingIOError("Colab busy")
    try:
        with path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
            _configure()
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        _LOCK.release()


def _configure():
    machinery.SESSION = ENV.get("V2MD_COLAB_SESSION") or "v2md-whisper"
    machinery.BIN_OVERRIDE = ENV.get("V2MD_COLAB_BIN") or ""
    machinery.MAX_WAIT_S = int(ENV.get("V2MD_COLAB_MAX_WAIT_S") or 2700)
    machinery.QUEUE_URL = ""
    machinery.KEEP_SESSION = True  # This adapter owns shutdown, including standalone calls.
    machinery.ALLOW_CPU_FALLBACK = (ENV.get("V2MD_COLAB_CPU_FALLBACK") or "off") == "on"

@contextlib.contextmanager
def session_scope():
    """Reuse one session across a run's stages, releasing it even on failure."""
    if _USE.get() is not None:
        yield
        return
    token = _USE.set(_SessionUse())
    try:
        yield
    finally:
        try:
            release_session("run 结束")
        finally:
            _USE.reset(token)


def release_session(reason: str = "GPU 阶段结束") -> None:
    """Stop only a session used by this scope; retry on scope exit if shutdown fails."""
    use = _USE.get()
    if use is None or not use.used:
        return
    failing = sys.exc_info()[0] is not None
    with _session():
        stopped = machinery._colab_stop_session(reason)
        if stopped:
            use.used = False
    if not stopped:
        message = "Colab 关机失败，请运行 colab sessions 检查并重试 colab stop"
        if not failing:
            raise RuntimeError(message)
        print(f"⚠ {message}", flush=True)


def health() -> tuple[bool, str]:
    try:
        with _session(wait=False):
            import shutil
            binary = machinery._colab_bin()
            if not shutil.which(binary):
                return False, "未安装 colab CLI：uv sync 后重试"
            if not (Path.home() / ".config/colab-cli/token.json").is_file():
                return False, "Colab CLI 未授权：先运行 colab sessions 完成登录"
            machinery._colab(["sessions"], timeout=30, retries=0)
            return True, "Colab CLI 已授权（会话按需创建）"
    except BlockingIOError:
        return True, "Colab 会话正在执行任务"
    except Exception:
        return False, "Colab CLI 登录或网络检查失败，请运行 colab sessions 排查"


def _work(fn, *args):
    if _USE.get() is None:
        # Own the scope even when a stage is called outside run_graph. The scope
        # finally also covers failures during session creation/dependency install.
        with session_scope():
            return _work(fn, *args)
    with _session():
        use = _USE.get()
        assert use is not None
        use.used = True
        start = time.monotonic()
        try:
            return fn(*args)
        finally:
            from .runs import bump_usage
            bump_usage(gpu_s=time.monotonic() - start)


def transcribe_audio(audio: Path, tmp: Path, model: str, language: str) -> dict:
    return _work(machinery.colab_run_asr, audio, tmp, model, language)


def ocr_images(images: list[Path], tmp: Path) -> list[dict]:
    return _work(machinery.colab_run_ocr, images, tmp)


def cut_specs(video: Path | None, direct_url: str, specs: list[dict], tmp: Path) -> tuple[dict, Path]:
    out = tmp / "clips_colab_out"
    out.mkdir(parents=True, exist_ok=True)
    return _work(machinery.colab_exec_clips, video, direct_url, specs, tmp, out), out

"""Direct Colab CLI execution for ASR, OCR and clips (no GPU router required)."""
from __future__ import annotations

import contextlib
import contextvars
import fcntl
import hashlib
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
    personal: bool = False


_USE: contextvars.ContextVar[_SessionUse | None] = contextvars.ContextVar("colab_session_use", default=None)
_OWNER: contextvars.ContextVar[str] = contextvars.ContextVar("colab_owner", default="")


def tenant_home(uid: str) -> Path:
    if not uid:
        raise ValueError("Colab tenant uid is required")
    root = Path(ENV.get("V2PDF_COLAB_USERS_DIR") or PROJ / "data" / "colab-users")
    return root / hashlib.sha256(uid.encode()).hexdigest()


def credential_directory(uid: str) -> Path:
    return tenant_home(uid) / ".config" / "colab-cli"


def remote_for_user(default: str) -> str:
    """A connected tenant uses its own account for all GPU stages of this run."""
    use = _USE.get()
    return "colab-cli" if use is not None and use.personal else default


def configured() -> bool:
    return any((ENV.get(key) or "").strip().lower() in ("colab", "colab-cli")
               for key in ("V2PDF_REMOTE", "V2PDF_OCR_REMOTE", "V2PDF_CLIPS_REMOTE"))


@contextlib.contextmanager
def _session(wait: bool = True, *, owner: str | None = None):
    owner_token = _OWNER.set(owner) if owner is not None else None
    try:
        uid = _OWNER.get()
        path = (tenant_home(uid) / "execution.lock" if uid else
                Path(ENV.get("V2PDF_COLAB_LOCK") or PROJ / "data" / "colab.lock"))
        path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        if not _LOCK.acquire(blocking=wait):
            raise BlockingIOError("Colab busy")
        previous = {}
        try:
            fields = ("SESSION", "BIN_OVERRIDE", "CLI_HOME", "MAX_WAIT_S", "QUEUE_URL", "KEEP_SESSION",
                      "ALLOW_CPU_FALLBACK")
            previous = {name: getattr(machinery, name) for name in fields}
            with path.open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
                try:
                    _configure()
                    yield
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            for name, value in previous.items():
                setattr(machinery, name, value)
            _LOCK.release()
    finally:
        if owner_token is not None:
            _OWNER.reset(owner_token)


def _configure():
    uid = _OWNER.get()
    name = ENV.get("V2PDF_COLAB_SESSION") or "v2pdf-whisper"
    machinery.SESSION = name + "-" + hashlib.sha256(uid.encode()).hexdigest()[:16] if uid else name
    machinery.CLI_HOME = str(tenant_home(uid)) if uid else ""
    machinery.BIN_OVERRIDE = ENV.get("V2PDF_COLAB_BIN") or ""
    machinery.MAX_WAIT_S = int(ENV.get("V2PDF_COLAB_MAX_WAIT_S") or 2700)
    machinery.QUEUE_URL = ""
    machinery.KEEP_SESSION = True  # This adapter owns shutdown, including standalone calls.
    machinery.ALLOW_CPU_FALLBACK = (ENV.get("V2PDF_COLAB_CPU_FALLBACK") or "off") == "on"

@contextlib.contextmanager
def session_scope(owner: str | None = None, *, suppress_cleanup_errors: bool = False):
    """Reuse one session across a run's stages, releasing it even on failure.

    A graph can return a terminal ``error`` state without raising.  In that
    case, cleanup is best-effort: a failed ``colab stop`` must not replace the
    graph's stage error with a framework-level exception.  Standalone callers
    keep the strict default so cleanup failures remain visible to them.
    """
    if _USE.get() is not None:
        if owner is not None and owner != _OWNER.get():
            raise RuntimeError("Colab 任务执行期间不能切换租户")
        yield
        return
    owner_token = _OWNER.set(owner) if owner is not None else None
    uid = _OWNER.get()
    token = _USE.set(_SessionUse(personal=bool(uid and (credential_directory(uid) / "token.json").is_file())))
    try:
        yield
    finally:
        original_error = sys.exc_info()[1]
        try:
            release_session("run 结束")
        except Exception as cleanup_error:
            if not suppress_cleanup_errors and original_error is None:
                raise
            print(f"⚠ Colab 会话清理失败（不覆盖原始任务结果）：{cleanup_error}", flush=True)
        finally:
            _USE.reset(token)
            if owner_token is not None:
                _OWNER.reset(owner_token)


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
            if (ENV.get("V2PDF_AUTH") or "off").lower() not in ("off", "none", ""):
                return True, "Colab CLI 可用；每个用户需连接自己的 Colab 账号"
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
        uid = _OWNER.get()
        if uid and not (credential_directory(uid) / "token.json").is_file():
            raise RuntimeError("当前用户尚未连接 Colab，请在“我的 → Colab 授权”完成授权")
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

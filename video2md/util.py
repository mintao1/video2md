"""通用工具：子进程/格式化/指纹/state 读写/脱敏/RunLog 事件流。"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import subprocess
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path

from .config import LOG_DIR, STATE_FILE



# ---------------------------------------------------------------- utils
def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if p.returncode != 0:
        raise RuntimeError(f"命令失败 ({' '.join(cmd[:3])}…):\n{p.stderr[-1500:]}")
    return p



def fmt_hms(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"



def fmt_ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60:02d}:{sec % 60:02d}"



def sanitize_filename(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|#^\[\]]', "", s).strip().rstrip(".")
    return s[:80] or "未命名"



def video_fingerprint(video: Path) -> str:
    st = video.stat()
    return hashlib.sha256(f"{video.name}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:16]



def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}



def save_state(s: dict) -> None:
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2))



# ---------------------------------------------------------------- run log（借鉴 ZCode 遥测模式）
_SECRET_RE = re.compile(r"(sk-|app-|dataset-)[A-Za-z0-9_\-]{6,}")



def redact(s: str) -> str:
    """日志层统一脱敏：任何 key 形态的串都打码，不依赖调用方自觉。"""
    return _SECRET_RE.sub(r"\1***", str(s))



# run_id → 进行中的 RunLog（runs._run_stage 注册；transcribe/clips 等深层函数据此
# 取当前 log 打细粒度子 span，不用层层透传。放在 util 是为了避免 transcribe→runs 循环 import）
_STAGE_LOGS: dict[str, "RunLog"] = {}


def current_log() -> "RunLog | None":
    """当前活跃的阶段 RunLog（api 串行驱动阶段，任意时刻最多一个）。"""
    return next(iter(_STAGE_LOGS.values()), None)


class RunLog:
    """一次运行 = 一个 run_id；span 进入写 started、退出写 completed/failed/exited。

    借鉴 ZCode CLI 遥测：span 事件对（日志里悬空的 started 即崩溃点）、
    spanId 配对、ISO 时间戳、durationMs/status 字段纪律、日志层脱敏。
    """

    def __init__(self, label: str, run_id: str = "", trace_name: str = "",
                 trace_meta: dict | None = None):
        # run_id 可由调用方指定（__stage 子进程复用同一 run_id，跨进程拼出同一条 trace）；
        # trace_name/trace_meta 供 Langfuse 侧建 trace（name=url，metadata 带 stage/total）
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.label = label
        self.trace_name = trace_name
        self.trace_meta = trace_meta or {}
        self.spans: list[dict] = []
        self._stack: list[str] = []  # span 嵌套栈：Langfuse 父子关系（entry 挂 stage）
        self._lock = threading.Lock()  # 并行阶段多线程写 jsonl 串行化
        LOG_DIR.mkdir(exist_ok=True)
        self.path = LOG_DIR / f"run-{date.today().isoformat()}.jsonl"

    def _write(self, rec: dict) -> None:
        out = {"ts": datetime.now().isoformat(timespec="seconds"),
               "run_id": self.run_id}
        out.update({k: v for k, v in rec.items() if k != "ts_mono"})
        with self._lock:  # 并行阶段（剪节段/OCR）多线程写同一 jsonl
            with self.path.open("a") as f:
                f.write(json.dumps(out, ensure_ascii=False) + "\n")
        try:
            from . import observability  # 惰性导入：未配 OTel 端点/Langfuse 时零开销
            observability.record(out, run_id=self.run_id, label=self.label,
                                 trace_name=self.trace_name, trace_metadata=self.trace_meta)
        except Exception:
            pass  # sink 任何异常都不许影响主管线

    @contextlib.contextmanager
    def span(self, event: str):
        span_id = uuid.uuid4().hex[:8]
        parent = self._stack[-1] if self._stack else None
        self._write({"spanId": span_id, "event": event, "status": "started",
                     "parentSpanId": parent} if parent else
                    {"spanId": span_id, "event": event, "status": "started"})
        self._stack.append(span_id)
        rec = {"event": event, "spanId": span_id, "ts_mono": time.monotonic()}
        if parent:
            rec["parentSpanId"] = parent
        t0 = time.monotonic()
        try:
            yield rec
            # 调用方可置 force_status（如条目失败但不阻断：span 记 failed 而流程继续）
            rec.update(status=rec.pop("force_status", "completed"),
                       durationMs=round((time.monotonic() - t0) * 1000))
        except SystemExit:
            rec.update(status="exited", durationMs=round((time.monotonic() - t0) * 1000))
            raise
        except Exception as e:
            rec.update(status="failed", durationMs=round((time.monotonic() - t0) * 1000),
                       error=redact(str(e))[:300])
            raise
        finally:
            self._stack.pop()
            rec.pop("ts_mono", None)
            self.spans.append(rec)
            self._write(rec)

    def info(self, event: str, **fields) -> None:
        # 字符串过脱敏；数值/布尔原样保留（OTel attribute 类型不丢，jsonl 也可序列化）
        self._write({"event": event,
                     **{k: redact(v) if isinstance(v, str) else v for k, v in fields.items()}})



# ---------------------------------------------------------------- ① probe
def probe(video: Path) -> dict:
    p = run(["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(video)])
    info = json.loads(p.stdout)
    dur = float(info.get("format", {}).get("duration") or
                next((s.get("duration", 0) for s in info.get("streams", [])
                      if s.get("codec_type") == "video"), 0))
    vs = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    return {"duration": dur, "width": vs.get("width", 0), "height": vs.get("height", 0)}

"""通用工具：子进程/格式化/指纹/state 读写/脱敏/RunLog 事件流。"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import re
import subprocess
import threading
import time
import uuid
from datetime import date, datetime
from pathlib import Path

from . import events  # 进程内 SSE 总线（_write 的第三下游；events 无本地依赖，不成环）
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



_STATE_LOCK = threading.RLock()  # state.json 读改写串行化（RLock：锁内可再调 save_state）


def save_state(s: dict) -> None:
    """原子写（per-thread tmp + rename，锁内串行）——并发 run 下不撕裂、不互相截断。"""
    with _STATE_LOCK:
        tmp = STATE_FILE.parent / f"{STATE_FILE.name}.tmp.{threading.get_ident()}"
        tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2))
        tmp.replace(STATE_FILE)


@contextlib.contextmanager
def state_file_lock():
    """「load → 改 → save」整段持锁（node_note_entry 的幂等落盘），防并发丢更新。"""
    with _STATE_LOCK:
        yield



# ---------------------------------------------------------------- run log（借鉴 ZCode 遥测模式）
_SECRET_RE = re.compile(r"(sk-|app-|dataset-)[A-Za-z0-9_\-]{6,}")



def redact(s: str) -> str:
    """日志层统一脱敏：任何 key 形态的串都打码，不依赖调用方自觉。"""
    return _SECRET_RE.sub(r"\1***", str(s))



# run_id → 进行中的 RunLog（runs._run_stage / graph_obs 注册；transcribe/clips 等
# 深层函数据此取当前 log 打细粒度子 span，不用层层透传。放在 util 是为了避免 transcribe→runs 循环 import）
_STAGE_LOGS: dict[str, "RunLog"] = {}

# 当前执行上下文的 run_id（run_graph 入口 set；asyncio.to_thread 每次拷贝 context，
# 并发多 run 时各自线程看到各自的值）——current_log 的并发正确性来源。
_CURRENT_RUN_ID: contextvars.ContextVar[str] = contextvars.ContextVar("v2md_current_run_id",
                                                                      default="")


def bind_current_run(run_id: str) -> None:
    """把当前执行线程绑定到 run（run_graph 入口调一次，覆盖整张图的所有深层调用）。"""
    _CURRENT_RUN_ID.set(run_id)


def current_log() -> "RunLog | None":
    """当前活跃的阶段 RunLog：并发多 run 时按 ContextVar 的 run_id 取本 run 的 log；
    未绑定（__stage 子进程入口/旧单跑路径）回退「dict 第一个」。"""
    rid = _CURRENT_RUN_ID.get()
    if rid and rid in _STAGE_LOGS:
        return _STAGE_LOGS[rid]
    return next(iter(_STAGE_LOGS.values()), None)


class RunLog:
    """一次运行 = 一个 run_id；span 进入写 started、退出写 completed/failed/exited。

    借鉴 ZCode CLI 遥测：span 事件对（日志里悬空的 started 即崩溃点）、
    spanId 配对、ISO 时间戳、durationMs/status 字段纪律、日志层脱敏。
    """

    def __init__(self, label: str, run_id: str = "", trace_name: str = "",
                 trace_meta: dict | None = None, user_id: str = ""):
        # run_id 可由调用方指定（__stage 子进程复用同一 run_id，跨进程拼出同一条 trace）；
        # trace_name/trace_meta 供 Langfuse 侧建 trace（name=url，metadata 带 stage/total）；
        # user_id = run 归属内部 uid → Langfuse userId（按用户过滤/成本归因的前提）
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.label = label
        self.trace_name = trace_name
        self.trace_meta = trace_meta or {}
        self.user_id = user_id
        self.spans: list[dict] = []
        self._stack: list[str] = []  # span 嵌套栈：Langfuse 父子关系（entry 挂 stage）
        self._lock = threading.Lock()  # 并行阶段多线程写 jsonl 串行化
        LOG_DIR.mkdir(exist_ok=True)
        self.path = LOG_DIR / f"run-{date.today().isoformat()}.jsonl"

    def _write(self, rec: dict) -> None:
        out = {"ts": datetime.now().isoformat(timespec="seconds"),
               "run_id": self.run_id}
        if self.user_id:
            out["user_id"] = self.user_id
        out.update({k: v for k, v in rec.items() if k not in ("ts_mono", "_otel")})
        with self._lock:  # 并行阶段（剪节段/OCR）多线程写同一 jsonl
            with self.path.open("a") as f:
                f.write(json.dumps(out, ensure_ascii=False) + "\n")
        try:
            from . import watchdog  # 惰性导入：心跳打点（看门狗判定「run 活着」的最细粒度信号）
            watchdog.touch(self.run_id)
        except Exception:
            pass  # 下游 sink 任何异常都不许影响主管线
        try:
            events.publish(self.run_id, out)  # SSE 总线（工作线程安全投递，见 events）
        except Exception:
            pass  # 下游 sink 任何异常都不许影响主管线
        try:
            from . import observability  # 惰性导入：未配 OTel 端点/Langfuse 时零开销
            # rec["_otel"]（如 LLM prompt/completion 全文）只喂 OTel sink：
            # 不进 jsonl/SSE——SSE 撑不起百 KB 级负载，jsonl 也不该养正文副本
            observability.record(out, run_id=self.run_id, label=self.label,
                                 trace_name=self.trace_name, trace_metadata=self.trace_meta,
                                 otel_attrs=rec.get("_otel"))
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
            self._write(rec)
            rec.pop("_otel", None)  # 全文只给 OTel sink；span_log/state.json 不养副本
            self.spans.append(rec)

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
    vs: dict = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    return {"duration": dur, "width": vs.get("width", 0), "height": vs.get("height", 0)}

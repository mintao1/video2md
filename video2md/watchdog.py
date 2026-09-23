"""run 卡死看门狗：心跳超时判定 → 标记 error / 进程退出自愈。

缺口（2026-09-23）：graph.invoke 无超时，ffmpeg/yt-dlp 挂死或死锁时工作线程永远占着
执行信号量——队列饿死且**零检测**。死人开关只回答「进程死了没有」（run_graph 终态才
ping），回答不了「run 活着但卡住」。

心跳口径（取「最近活动距今」的最小龄，任何一层动了就算活着）：
  1. RunLog._write 每条事件 → touch(run_id)（graph:*/stage:*/llm:* 事件流，最细粒度）
  2. _run_stage_inner 每条目循环开头 → touch（覆盖日志稀疏的长阶段，如 3h 转录）
  3. 兜底：_TASKS[run_id]["started"] 与 runs.json 最近 stages[*].ts（进程重启后仍有据可查）

判定：api 进程内 asyncio 周期任务，对内存任务表里 status=="running" 的 run，
最近活动龄 > V2MD_WATCHDOG_TIMEOUT_S（默认 7200s）→ 卡死；三层全无信号则放过
（无法判定时不误杀，api 路径 started 恒有值，此态只出现在异常注入里）。

动作（V2MD_WATCHDOG_ACTION，默认 mark）：
  mark  = run 置 error（runs.json + 内存任务表同步翻转，SSE/轮询面自动生效）+ 死人开关
          /fail + 指标 v2md_watchdog_trips_total。注意：卡死线程无法强杀，信号量仍被占
          ——需人工重启恢复队列（告警会响）。
  exit  = 同上标记后 os._exit(70)：容器 restart: unless-stopped 秒级自愈；重启后 lifespan
          只重投 runs.json 里 status=="running" 的 run，已标 error 的不会死循环。

纪律对齐 deadman.py：看门狗自身任何异常吞掉（stderr 一次），绝不反压主管线。
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime

from . import deadman, observability
from .config import WATCHDOG_ACTION, WATCHDOG_TIMEOUT_S
from .util import redact

# run_id → 最近心跳（time.monotonic()）。工作线程写、asyncio 循环读，GIL 下 dict 读写原子。
_HEARTBEATS: dict[str, float] = {}

_NOTED = False  # 看门狗自身异常只 stderr 一次，后续静默


def touch(run_id: str) -> None:
    """心跳打点：任何管线异常都不许外抛（看门狗绝不能成为新的故障面）。"""
    try:
        _HEARTBEATS[run_id] = time.monotonic()
    except Exception:
        pass


def forget(run_id: str) -> None:
    """终态清理：防 _HEARTBEATS 随 run 数无限增长。"""
    _HEARTBEATS.pop(run_id, None)


def _iso_to_epoch(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _last_stage_epoch(run: dict) -> float:
    """runs.json 里最近一次阶段落盘时间（epoch 秒）；无阶段记录返回 0。"""
    last = 0.0
    for s in (run.get("stages") or {}).values():
        ts = (s or {}).get("ts")
        if not ts:
            continue
        last = max(last, _iso_to_epoch(str(ts)))
    return last


def _age_seconds(run_id: str, task: dict | None, run: dict | None, *,
                 now_mono: float | None = None, now_wall: float | None = None) -> float | None:
    """最近活动距今的秒数（三层取最小龄）；None = 三层全无信号。

    负龄（时间戳在未来，时钟偏移）钳到 0——按「刚刚活动」算，宁可放过不误杀。
    """
    mono = time.monotonic() if now_mono is None else now_mono
    wall = time.time() if now_wall is None else now_wall
    ages: list[float] = []
    hb = _HEARTBEATS.get(run_id)
    if hb:
        ages.append(max(mono - hb, 0.0))
    started = (task or {}).get("started") or ""
    e = _iso_to_epoch(started) if started else 0.0
    if e:
        ages.append(max(wall - e, 0.0))
    if run:
        st = _last_stage_epoch(run)
        if st:
            ages.append(max(wall - st, 0.0))
    return min(ages) if ages else None


def check_run(run_id: str, task: dict, run: dict | None, *, timeout_s: float | None = None,
              now_mono: float | None = None, now_wall: float | None = None) -> bool:
    """判定单个 running run 是否卡死。纯函数化便于离线测试。

    task = 内存任务表条目（含 started）；run = runs.json 记录（可能 None）。
    返回 True = 判定卡死（调用方负责动作）。
    """
    timeout = WATCHDOG_TIMEOUT_S if timeout_s is None else timeout_s
    age = _age_seconds(run_id, task, run, now_mono=now_mono, now_wall=now_wall)
    if age is None:
        return False  # 无任何信号：无法判定，不误杀
    return age > timeout


def trip(run_id: str, task: dict, run: dict | None) -> str:
    """卡死处置：翻转状态 + 留痕 + 告警信号，返回执行的动作（"mark"|"exit"）。

    runs.json 与内存任务表同步翻转——SSE（_run_current_status 优先读内存表）与轮询
    GET /runs/{id} 两个面自动看到终态，无需额外通知管线。
    """
    from .runs import _load_runs, _runs_file_lock, _runs_merge  # 惰性：避免 import 环

    msg = (f"watchdog: 心跳超时 {WATCHDOG_TIMEOUT_S:.0f}s 判定卡死"
           f"（action={WATCHDOG_ACTION}；最近活动见 stages[*].ts / 日志 jsonl）")
    print(f"🐕 看门狗：run {run_id} {msg}")
    fresh = (run if run is not None else _load_runs().get(run_id)) or {}
    fresh["run_id"] = run_id
    fresh["status"] = "error"
    fresh["errors"] = [*(fresh.get("errors") or []), msg][-20:]  # 对齐 node_fail 的 20 条留痕
    with _runs_file_lock():
        _runs_merge(fresh)
    task["status"] = "error"
    task["finished"] = datetime.now().isoformat(timespec="seconds")
    task.setdefault("errors", []).append(redact(msg)[:300])
    observability.metric_watchdog()
    deadman.ping("fail", run_id=run_id)
    forget(run_id)
    return WATCHDOG_ACTION


def check_once(tasks: dict, *, load_runs=None, timeout_s: float | None = None,
               now_mono: float | None = None, now_wall: float | None = None) -> list[str]:
    """扫一遍内存任务表，对卡死 run 逐个 trip。返回处置过的 run_id 列表。

    load_runs 注入点供测试；生产传 None 走 runs.json 真读。
    """
    load = load_runs or _default_load_runs
    tripped: list[str] = []
    for rid, task in list(tasks.items()):
        if task.get("status") != "running":
            continue
        try:
            run = load().get(rid)
        except Exception as exc:  # runs.json 读炸不该炸看门狗
            _note(f"runs.json 读取异常：{redact(str(exc))[:120]}")
            run = None
        try:
            if check_run(rid, task, run, timeout_s=timeout_s,
                         now_mono=now_mono, now_wall=now_wall):
                trip(rid, task, run)
                tripped.append(rid)
                if WATCHDOG_ACTION == "exit":
                    _exit_self(rid)  # 不返回：os._exit 直接终止进程
        except Exception as exc:
            _note(f"run {rid} 处置异常：{redact(str(exc))[:160]}")
    return tripped


def _default_load_runs():
    from .runs import _load_runs
    return _load_runs()


def _exit_self(run_id: str) -> None:
    """exit 动作：标记已同步落盘（jsonl/SSE 在 _write 里已写完），硬退。

    os._exit 不跑 atexit/flush——observability 的批导出会丢最后一批 trace，可接受
    （trace 是观测数据，状态翻转已落 runs.json）。退出码 70（EX_SOFTWARE 语义：
    内部软件错误），restart: unless-stopped 拉起。
    """
    print(f"🐕 看门狗：exit 自愈——进程退出（码 70），容器将重启并恢复队列（触发 run={run_id}）")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(70)


def _note(msg: str) -> None:
    global _NOTED
    if not _NOTED:
        print(f"⚠ 看门狗异常（后续静默，不影响管线）：{msg}", file=sys.stderr)
        _NOTED = True


async def loop(tasks: dict, period_s: float, *, load_runs=None) -> None:
    """api lifespan 常驻周期任务：每 period_s 扫一遍。取消时安静退出。"""
    import asyncio
    while True:
        await asyncio.sleep(period_s)
        try:
            check_once(tasks, load_runs=load_runs)
        except Exception as exc:  # 双保险：check_once 内部已兜，这里防未来改动漏兜
            _note(f"巡检异常：{redact(str(exc))[:160]}")


def _reset_for_tests() -> None:
    """测试隔离：重置模块级状态（心跳表/静默标志）。"""
    _HEARTBEATS.clear()
    global _NOTED
    _NOTED = False

"""死人开关：run 终态 ping 外部监控 URL（healthchecks.io 兼容），证明「任务活着/死了」。

调研依据（docs/research/2026-09-21-script-observability.md）：脚本层的第一层观测不是
trace 平台，而是死人开关——且「不能依赖自己的基础设施报自己的警」。trace/metrics
都在机房内，机器/容器整体挂掉时它们一起沉默；外部 ping 服务在「该来的 ping 没来」
或收到 /fail 时才告警，与自有 infra 无关。

协议（healthchecks.io 语义，同类服务通用）：
    GET {url}/start   run 开始（供对端算运行时长/超时）
    GET {url}         终态 done/partial（成功；partial 也 ping 成功并附 note——
                      部分失败在 Langfuse/Grafana 里已有留痕，ping 只回答「跑没跑完」）
    GET {url}/fail    终态 error/异常

配置：V2MD_DEADMAN_PING_URL，未配置 = 完全 no-op。3s 超时 fire-and-forget，
任何异常吞掉（stderr 一次）——监控通道绝不能反压/炸掉主管线。
"""
from __future__ import annotations

import os
import sys

import httpx

from .config import ENV
from .util import RunLog

_TIMEOUT_S = 3.0
_NOTED = False


def ping(state: str, *, run_id: str = "", trace_name: str = "") -> None:
    """state: "start" | "ok" | "fail"。未配 URL 即静默返回。
    run_id/trace_name 给了就把 ping 事件打上 run 的 trace（自建一次性 RunLog）。"""
    if "V2MD_DEADMAN_PING_URL" in os.environ:
        raw = os.environ["V2MD_DEADMAN_PING_URL"]   # 显式空串 = 显式关闭
    else:
        raw = ENV.get("V2MD_DEADMAN_PING_URL", "")
    base = raw.strip().rstrip("/")
    if not base:
        return
    global _NOTED
    suffix = {"start": "/start", "fail": "/fail"}.get(state, "")
    try:
        httpx.get(f"{base}{suffix}", timeout=_TIMEOUT_S)
    except Exception as exc:
        if not _NOTED:
            print(f"⚠️ 死人开关 ping 失败（{str(exc)[:80]}；后续静默，不影响管线）",
                  file=sys.stderr)
            _NOTED = True
        return
    if run_id:
        from . import observability
        log = RunLog("deadman", run_id=run_id, trace_name=trace_name)
        log.info("deadman", state=state)
        observability.close_run(run_id)
        observability.flush()

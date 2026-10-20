"""Colab 会话机械单测：队列保温判定 / lease 心跳 / CLI 重试 / 看门狗决策。
不联网、不碰真 Colab（全部桩掉）。从 dify-video2md test_jobs.py 的会话用例迁移而来。

跑法：python3 tests/test_colab_machinery.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gpu_plugins.v2md.providers import colab_machinery as M

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


_TMP = Path(tempfile.mkdtemp(prefix="gpu-router-machinery-"))

# ---- _more_jobs_waiting：队列非空/见底/服务不在/未配置 四种形态
print("== 队列保温判定 ==")
M.QUEUE_URL = "http://fake/queue"
M.httpx.get = lambda *a, **kw: type("R", (), {"json": staticmethod(
    lambda: {"queue_len": 1, "running": 0})})()
check("队列非空 → 保留会话", M._more_jobs_waiting() is True)
M.httpx.get = lambda *a, **kw: type("R", (), {"json": staticmethod(
    lambda: {"queue_len": 0, "running": 0})})()
check("队列见底且无在跑 → 关会话", M._more_jobs_waiting() is False)
M.httpx.get = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("服务不在"))
check("服务不在 → 关会话", M._more_jobs_waiting() is False)
M.QUEUE_URL = ""
check("未配置 queue_url → 用完即关", M._more_jobs_waiting() is False)

# ---- lease 心跳
print("== lease 心跳 ==")
M.LEASE_FILE = str(_TMP / "lease.json")
M._lease_release()
check("未摸 → 不新鲜", M._lease_fresh() is False)
M._lease_touch()
check("摸过 → 新鲜", M._lease_fresh() is True)
M._lease_release()
check("释放 → 不新鲜", M._lease_fresh() is False)
M.LEASE_FILE = ""
check("未配置 lease → 恒不新鲜（看门狗只按队列/在途判断）", M._lease_fresh() is False)

# ---- _colab CLI 重试
print("== CLI 重试 ==")
calls = {"n": 0}


class _Proc:
    def __init__(self, rc, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def fake_run_ok2nd(*a, **kw):
    calls["n"] += 1
    return _Proc(1 if calls["n"] == 1 else 0, "out-ok")


_orig_run = M.subprocess.run
M.subprocess.run = fake_run_ok2nd
try:
    out = M._colab(["sessions"], timeout=5, retries=1)
    check("首次失败重试一次后成功", out == "out-ok" and calls["n"] == 2)
finally:
    M.subprocess.run = _orig_run

# ---- 看门狗决策：空闲 + lease 不新鲜 + 会话活着 → 关；任一条件否 → 不关
print("== 看门狗决策 ==")
stopped: list[str] = []
_orig_stop_session = M._colab_stop_session
_orig_session_active = M._colab_session_active
_orig_more_jobs = M._more_jobs_waiting
_orig_lease_fresh = M._lease_fresh
M._colab_stop_session = lambda reason="": stopped.append(reason) or True
M._colab_session_active = lambda *a, **kw: True
M._more_jobs_waiting = lambda: False
M._lease_fresh = lambda *a, **kw: False


class _BreakLoop(Exception):
    pass


_sleep_calls = {"n": 0}


def fake_sleep(_s):
    _sleep_calls["n"] += 1
    if _sleep_calls["n"] >= 2:
        raise _BreakLoop


_orig_sleep = M.time.sleep
M.time.sleep = fake_sleep
try:
    _sleep_calls["n"] = 0
    stopped.clear()
    try:
        M.autostop_watchdog(lambda: False, idle_s=0)   # 全闲 → 应关
    except _BreakLoop:
        pass
    check("全闲 → 自动关会话", len(stopped) == 1, str(stopped))

    _sleep_calls["n"] = 0
    stopped.clear()
    try:
        M.autostop_watchdog(lambda: True, idle_s=0)    # 路由在途 → 不关
    except _BreakLoop:
        pass
    check("路由在途 → 不关", stopped == [])

    M._lease_fresh = lambda *a, **kw: True
    _sleep_calls["n"] = 0
    stopped.clear()
    try:
        M.autostop_watchdog(lambda: False, idle_s=0)   # lease 新鲜（有任务在跑）→ 不关
    except _BreakLoop:
        pass
    check("lease 新鲜 → 不关", stopped == [])
finally:
    M.time.sleep = _orig_sleep
    M._colab_stop_session = _orig_stop_session
    M._colab_session_active = _orig_session_active
    M._more_jobs_waiting = _orig_more_jobs
    M._lease_fresh = _orig_lease_fresh

print()
if FAILED:
    print(f"❌ {len(FAILED)} 项失败：" + "; ".join(FAILED))
    sys.exit(1)
print("✅ 全部通过")

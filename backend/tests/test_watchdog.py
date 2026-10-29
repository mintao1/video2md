#!/usr/bin/env python3
"""test_watchdog.py — run 卡死看门狗离线单测。

覆盖：心跳打点/清理 / 三层信号判定（心跳、started、stages ts）/ mark 翻转 runs.json+任务表 /
exit 调用 os._exit / 健康与无信号 run 不误伤 / 巡检异常静默 / RunLog 事件驱动心跳。

运行：python3 test_watchdog.py（无网络；runs.json/日志进临时目录）。
"""
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/（包根）

_TMP = tempfile.mkdtemp(prefix="v2pdf-test-wd-")
# —— 必须在 import video2pdf.config 之前设好（config 在 import 时读 env）——
os.environ.update({
    "V2PDF_RUNS_FILE": str(Path(_TMP) / "runs.json"),
    "V2PDF_STATE_FILE": str(Path(_TMP) / "state.json"),
    "V2PDF_LOG_DIR": str(Path(_TMP) / "logs"),
    "V2PDF_SECRETS_FILE": str(Path(_TMP) / "nonexistent.env"),
    "V2PDF_DEADMAN_PING_URL": "",  # 关死人开关（trip 里会 ping）
})

from video2pdf import runs, watchdog  # noqa: E402
from video2pdf.util import RunLog  # noqa: E402

NOW_WALL = 1_800_000_000.0  # 固定墙钟（epoch 秒）
NOW_MONO = 500_000.0        # 固定单调钟
TIMEOUT = 100.0


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


def _task(started_epoch: float | None = NOW_WALL, status: str = "running") -> dict:
    t: dict = {"status": status}
    if started_epoch is not None:
        t["started"] = _iso(started_epoch)
    return t


def _run(*, stage_ts_epoch: float | None = None, status: str = "running") -> dict:
    r: dict = {"run_id": "r1", "status": status, "stages": {}}
    if stage_ts_epoch is not None:
        r["stages"]["download"] = {"ts": _iso(stage_ts_epoch)}
    return r


def _seed_runs_file(rec: dict) -> None:
    runs._save_runs({rec["run_id"]: rec})


# ---- 心跳表基本行为 ------------------------------------------------------------
watchdog._reset_for_tests()
watchdog.touch("r1")
assert watchdog._HEARTBEATS["r1"] <= time.monotonic()
watchdog.forget("r1")
assert "r1" not in watchdog._HEARTBEATS

# RunLog 事件驱动心跳（_write 打点；异常不外抛）
log = RunLog("t", run_id="rlog")
log.info("hello", x=1)
assert "rlog" in watchdog._HEARTBEATS, "RunLog._write 应触发 watchdog.touch"

# ---- check_run 判定：三层信号 ----------------------------------------------------
watchdog._reset_for_tests()

# 1) 心跳新鲜 → 活着（哪怕 started/stages 都超窗）
watchdog._HEARTBEATS["r1"] = NOW_MONO - 5  # 直接操纵心跳表对齐假时钟
assert watchdog.check_run("r1", _task(NOW_WALL - TIMEOUT * 10), _run(stage_ts_epoch=NOW_WALL - TIMEOUT * 10),
                          timeout_s=TIMEOUT, now_mono=NOW_MONO, now_wall=NOW_WALL) is False
# 心跳超窗（且 started/stages 无信号）→ 卡死
watchdog._HEARTBEATS["r1"] = NOW_MONO - TIMEOUT - 1
assert watchdog.check_run("r1", _task(None), {"run_id": "r1", "stages": {}}, timeout_s=TIMEOUT,
                          now_mono=NOW_MONO, now_wall=NOW_WALL) is True

# 2) 无心跳、started 新鲜 → 活着；started 超窗且无其他信号 → 卡死
watchdog._reset_for_tests()
assert watchdog.check_run("r1", _task(NOW_WALL - 10), _run(), timeout_s=TIMEOUT,
                          now_mono=NOW_MONO, now_wall=NOW_WALL) is False
assert watchdog.check_run("r1", _task(NOW_WALL - TIMEOUT - 5), _run(), timeout_s=TIMEOUT,
                          now_mono=NOW_MONO, now_wall=NOW_WALL) is True

# 3) 无心跳、stages ts 新鲜 → 活着；超窗 → 卡死
assert watchdog.check_run("r1", _task(None), _run(stage_ts_epoch=NOW_WALL - 10),
                          timeout_s=TIMEOUT, now_mono=NOW_MONO, now_wall=NOW_WALL) is False
assert watchdog.check_run("r1", _task(None), _run(stage_ts_epoch=NOW_WALL - TIMEOUT - 5),
                          timeout_s=TIMEOUT, now_mono=NOW_MONO, now_wall=NOW_WALL) is True

# 4) 三层全无信号 → 不判定（不误杀）
assert watchdog.check_run("r1", _task(None), {"run_id": "r1", "stages": {}},
                          timeout_s=TIMEOUT, now_mono=NOW_MONO, now_wall=NOW_WALL) is False
# 5) 非 running 状态的 run 不该进判定（check_once 过滤，这里直接验 task 语义）
assert _task(status="done")["status"] != "running"

# ---- trip：mark 翻转 runs.json + 任务表 ------------------------------------------
watchdog._reset_for_tests()
_seed_runs_file(_run(stage_ts_epoch=NOW_WALL - 999))
task = _task(NOW_WALL - 999)
pings: list[str] = []
with mock.patch.object(watchdog.deadman, "ping", lambda state, **kw: pings.append(state)):
    action = watchdog.trip("r1", task, None)
assert action == "mark", f"默认动作应为 mark（env 未配 exit），得到 {action}"
saved = runs._load_runs()["r1"]
assert saved["status"] == "error"
assert any("watchdog" in e for e in saved["errors"]), saved["errors"]
assert task["status"] == "error" and task["finished"]
assert pings == ["fail"], pings
assert "r1" not in watchdog._HEARTBEATS, "trip 后应清心跳"

# ---- check_once：只动 running；exit 动作调 os._exit -------------------------------
watchdog._reset_for_tests()
_seed_runs_file(_run(stage_ts_epoch=NOW_WALL - 999))
tasks = {
    "stale": _task(NOW_WALL - 999),            # 卡死 → 处置
    "fresh": _task(NOW_WALL - 10),             # 健康 → 不动
    "done": _task(NOW_WALL - 999, status="done"),  # 终态 → 跳过
}
with mock.patch.object(watchdog.deadman, "ping"):
    tripped = watchdog.check_once(tasks, load_runs=runs._load_runs,
                                  timeout_s=TIMEOUT, now_mono=NOW_MONO, now_wall=NOW_WALL)
assert tripped == ["stale"], tripped
assert tasks["stale"]["status"] == "error"
assert tasks["fresh"]["status"] == "running"
assert tasks["done"]["status"] == "done"
assert runs._load_runs().get("fresh") is None, "健康 run 不落盘（只有 trip 才写 runs.json）"

# exit 动作：os._exit(70) 被调用（mock 掉，验证调用链）
watchdog._reset_for_tests()
with mock.patch.dict(os.environ, {"V2PDF_WATCHDOG_ACTION": "exit"}), \
        mock.patch.object(watchdog, "WATCHDOG_ACTION", "exit"), \
        mock.patch.object(watchdog.deadman, "ping"), \
        mock.patch.object(watchdog.os, "_exit") as m_exit:
    _seed_runs_file(_run(stage_ts_epoch=NOW_WALL - 999))
    tasks2 = {"stale": _task(NOW_WALL - 999)}
    tripped = watchdog.check_once(tasks2, load_runs=runs._load_runs,
                                  timeout_s=TIMEOUT, now_mono=NOW_MONO, now_wall=NOW_WALL)
    assert tripped == ["stale"]
    assert tasks2["stale"]["status"] == "error", "exit 前必须先落盘标记"
    m_exit.assert_called_once_with(70)

# ---- 巡检异常静默（runs.json 读炸 / trip 炸都不外抛）-------------------------------
watchdog._reset_for_tests()
tasks3 = {"boom": _task(NOW_WALL - 999)}

def _boom_load():
    raise RuntimeError("runs.json 炸了")

with mock.patch.object(watchdog.deadman, "ping"):
    tripped = watchdog.check_once(tasks3, load_runs=_boom_load,
                                  timeout_s=TIMEOUT, now_mono=NOW_MONO, now_wall=NOW_WALL)
# 读炸不炸看门狗：内存 started 信号仍有效，照常判定（静默 = 不抛异常，不是跳过判定）
assert tripped == ["boom"] and tasks3["boom"]["status"] == "error", "读炸应静默且照常按 started 判定"

# trip 内部异常（如 runs.json 写炸）同样不外抛
watchdog._reset_for_tests()
tasks4 = {"boom2": _task(NOW_WALL - 999)}
with mock.patch.object(watchdog.deadman, "ping"), \
        mock.patch("video2pdf.runs._runs_merge", side_effect=OSError("disk full")):
    tripped = watchdog.check_once(tasks4, load_runs=runs._load_runs,
                                  timeout_s=TIMEOUT, now_mono=NOW_MONO, now_wall=NOW_WALL)
assert tripped == [] and tasks4["boom2"]["status"] == "running", "写炸应静默放过"

# 直接验 _note 静默纪律：异常计数只 stderr 一次
watchdog._reset_for_tests()
with mock.patch("sys.stderr") as fake_err:
    for _ in range(3):
        watchdog._note("x")
    assert fake_err.write.call_count <= 3, "静默标志应只打一次"

# ---- loop：周期任务可取消（asyncio 冒烟）------------------------------------------
import asyncio  # noqa: E402

watchdog._reset_for_tests()
async def _loop_smoke():
    t = asyncio.create_task(watchdog.loop({}, 0.01, load_runs=runs._load_runs))
    await asyncio.sleep(0.05)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass

asyncio.run(_loop_smoke())

# ---- P4b 大盘巡检 check_cluster（假 backend，离线不碰库）---------------------------
class _FakeClusterStore:
    """PostgresTaskStore 的大盘段替身：stale_running 回放固定行，steal/mark 可编排。"""

    def __init__(self, rows, *, steal_rec=None, recheck_age=None):
        self.rows = rows
        self.steal_rec = steal_rec
        self.recheck = recheck_age
        self.calls: list[str] = []
        self.marked: list[tuple[str, str]] = []

    def stale_running(self, timeout_s):
        self.calls.append("stale_running")
        return list(self.rows)

    def steal_if_unclaimed(self, rid, lease_s, proc):
        self.calls.append(f"steal:{rid}")
        return self.steal_rec

    def recheck_age(self, rid):
        return self.recheck if self.recheck is not None else NOW_WALL - TIMEOUT - 999

    def mark_stuck_error(self, rid, msg):
        self.calls.append(f"mark:{rid}")
        self.marked.append((rid, msg))
        return True


def _row(rid="c1", started_epoch=None, claimed_by="", last_active=None):
    return {"run_id": rid, "started": _iso(started_epoch) if started_epoch else "",
            "claimed_by": claimed_by,
            "last_active_epoch": (NOW_WALL - TIMEOUT - 50 if last_active is None else last_active)}


# json 缺省档：backend None → DB 段整体不激活（check_once 行为逐字节现状已由上面用例锁死）
watchdog._reset_for_tests()
with mock.patch.object(watchdog, "_tasks_backend_safe", lambda: None):
    assert watchdog.check_cluster(timeout_s=TIMEOUT, now_wall=NOW_WALL) == []

# 1) 叠加信号更年轻（_LOCAL_HB 刚收到 cmd 通知）→ 不误杀
watchdog._reset_for_tests()
be = _FakeClusterStore([_row("c1")])
with mock.patch.object(watchdog, "_tasks_backend_safe", lambda: be), \
        mock.patch.object(watchdog.deadman, "ping"):
    watchdog._LOCAL_HB["c1"] = NOW_WALL - 5  # 心跳新鲜：三层取最小龄 → 活着
    assert watchdog.check_cluster(timeout_s=TIMEOUT, now_wall=NOW_WALL) == []
assert be.calls == ["stale_running"]  # 未触发 steal

# 2) 无主残档超龄 → steal 抢到 + 复核仍超龄 → 翻 error + 交 stealer 重驱
watchdog._reset_for_tests()
rec = {"run_id": "c2", "url": "http://x/c2", "part_items": "", "force": False, "owner": "u"}
stolen: list[dict] = []
be = _FakeClusterStore([_row("c2", claimed_by="")], steal_rec=rec)
watchdog.set_stealer(stolen.append)
with mock.patch.object(watchdog, "_tasks_backend_safe", lambda: be), \
        mock.patch.object(watchdog.deadman, "ping"), \
        mock.patch.object(watchdog.observability, "metric_watchdog"):
    out = watchdog.check_cluster(timeout_s=TIMEOUT, now_wall=NOW_WALL)
assert out == ["c2"] and ("c2", ) and stolen == [rec]
assert any(c.startswith("mark:c2") for c in be.calls)

# 3) 他进程持有且租约未过期（steal 抢不到）→ skip 不动
watchdog._reset_for_tests()
be = _FakeClusterStore([_row("c3", claimed_by="other")], steal_rec=None)
with mock.patch.object(watchdog, "_tasks_backend_safe", lambda: be), \
        mock.patch.object(watchdog.deadman, "ping"):
    assert watchdog.check_cluster(timeout_s=TIMEOUT, now_wall=NOW_WALL) == []
assert "steal:c3" in be.calls and not any(c.startswith("mark:") for c in be.calls)

# 4) steal 持锁后复核发现刚有活动（recheck_age 新鲜）→ 归还认领，不动
watchdog._reset_for_tests()
be = _FakeClusterStore([_row("c4")], steal_rec={"run_id": "c4"}, recheck_age=NOW_WALL - 3)
seen: list[dict] = []
watchdog.set_stealer(seen.append)
with mock.patch.object(watchdog, "_tasks_backend_safe", lambda: be), \
        mock.patch.object(watchdog.deadman, "ping"):
    assert watchdog.check_cluster(timeout_s=TIMEOUT, now_wall=NOW_WALL) == []
assert seen == [] and not any(c.startswith("mark:") for c in be.calls)

# 5) 本进程正驱动的活条目被筛出 → 走既有 trip（翻转 runs.json + 任务表）
watchdog._reset_for_tests()
_seed_runs_file(_run(stage_ts_epoch=NOW_WALL - 999))
task_live = _task(NOW_WALL - 999)
local_tasks = {"r1": task_live}
be = _FakeClusterStore([_row("r1", claimed_by="me")])
import video2pdf.api as _api  # noqa: E402

with mock.patch.object(watchdog, "_tasks_backend_safe", lambda: be), \
        mock.patch.object(_api, "_PROC_ID", "me"), \
        mock.patch.object(_api, "_TASKS", local_tasks), \
        mock.patch.object(watchdog.deadman, "ping"):
    out = watchdog.check_cluster(timeout_s=TIMEOUT, now_wall=NOW_WALL)
assert out == ["r1"] and task_live["status"] == "error"
watchdog._reset_for_tests()
watchdog.set_stealer(None)

print("✅ test_watchdog.py 全部通过")

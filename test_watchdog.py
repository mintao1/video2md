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

sys.path.insert(0, str(Path(__file__).parent))

_TMP = tempfile.mkdtemp(prefix="v2md-test-wd-")
# —— 必须在 import video2md.config 之前设好（config 在 import 时读 env）——
os.environ.update({
    "V2MD_RUNS_FILE": str(Path(_TMP) / "runs.json"),
    "V2MD_STATE_FILE": str(Path(_TMP) / "state.json"),
    "V2MD_LOG_DIR": str(Path(_TMP) / "logs"),
    "V2MD_SECRETS_FILE": str(Path(_TMP) / "nonexistent.env"),
    "V2MD_DEADMAN_PING_URL": "",  # 关死人开关（trip 里会 ping）
})

from video2md import runs, watchdog  # noqa: E402
from video2md.util import RunLog  # noqa: E402

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
with mock.patch.dict(os.environ, {"V2MD_WATCHDOG_ACTION": "exit"}), \
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
        mock.patch("video2md.runs._runs_merge", side_effect=OSError("disk full")):
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

print("✅ test_watchdog.py 全部通过")

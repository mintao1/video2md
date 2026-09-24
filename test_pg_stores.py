#!/usr/bin/env python3
"""test_pg_stores.py — P3/P4a PG 档真库测试（需要 V2MD_TEST_PG_DSN；未配则整文件 skip）。

覆盖：
- PostgresRunsStore：merge/load 往返、并发 merge 不丢、函数级分派（_load_runs/_runs_merge/
  _runs_file_lock no-op/_save_runs 禁用）、json 缺省档回归
- PostgresStateStore：逐 fp upsert（键只增不删）、函数级分派、json 缺省档回归
- PostgresIdentityStore：首登开户（is_new）、二次登录同 uid + email 回填持久化、
  put_identity 归并、并发首登同 uid 且无孤儿账号行
- events PG 扇出：跨「进程」NOTIFY → 本地订阅收到（双实例互通）、自身回声不双投

运行：V2MD_TEST_PG_DSN=postgresql://v2md:v2md@127.0.0.1:5432/v2md python3 test_pg_stores.py
（CI 由 services: postgres 提供；本地 docker run 临时库即可——测试自带 TRUNCATE，别指生产库）
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

DSN = os.environ.get("V2MD_TEST_PG_DSN") or ""
if not DSN:
    print("SKIP：未配 V2MD_TEST_PG_DSN（PG 真库测试只在有库的环境跑）")
    raise SystemExit(0)

# 在 import video2md 之前定档：config.ENV 是 import 时快照
os.environ.update({
    "V2MD_PG_DSN": DSN,
    "V2MD_RUNS_FILE": str(Path(tempfile.mkdtemp(prefix="v2md-pgt-runs-")) / "unused.json"),
    "V2MD_STATE_FILE": str(Path(tempfile.mkdtemp(prefix="v2md-pgt-state-")) / "unused.json"),
    "V2MD_USERS_FILE": str(Path(tempfile.mkdtemp(prefix="v2md-pgt-users-")) / "unused.json"),
    "V2MD_SECRETS_FILE": "/nonexistent",  # 隔离宿主凭证文件
    "V2MD_FAKE_LLM": "1",
})
sys.path.insert(0, str(Path(__file__).parent))

from acctkit.models import Principal  # noqa: E402
from video2md import events, pg, pgstore, runs, util  # noqa: E402
from video2md.auth.pg_store import PostgresIdentityStore  # noqa: E402
from video2md.config import ENV  # noqa: E402

pool = pg.make_pool(DSN)  # 独立池（绕开 shared 单例，便于收尾 close）


def _truncate(*tables: str) -> None:
    with pool.connection() as conn:
        for t in tables:
            conn.execute(f"TRUNCATE {t}")


# ---- 1. PostgresRunsStore -----------------------------------------------------------
st_runs = pgstore.PostgresRunsStore(pool)  # 构造即幂等建表
_truncate("v2md_runs")
st_runs.merge({"run_id": "r1", "owner": "u_a", "status": "created",
               "created": "2026-01-01T00:00:00", "entries": [1, 2]})
st_runs.merge({"run_id": "r2", "owner": "u_b", "status": "done", "created": "2026-01-02"})
loaded = st_runs.load()
assert set(loaded) == {"r1", "r2"} and loaded["r1"]["entries"] == [1, 2]

st_runs.merge({"run_id": "r1", "owner": "u_a", "status": "running",
               "created": "2026-01-01T00:00:00"})
assert st_runs.load()["r1"]["status"] == "running"  # upsert 覆盖

# 并发 merge 不丢（行级原子）
def _w(i: int) -> None:
    st_runs.merge({"run_id": f"cc{i}", "owner": "u", "status": "done", "created": ""})

ts = [threading.Thread(target=_w, args=(i,)) for i in range(8)]
for t in ts:
    t.start()
for t in ts:
    t.join()
assert len(st_runs.load()) == 2 + 8

# 函数级分派：runs._load_runs/_runs_merge 走 PG
runs._RUNS_PG = st_runs
runs._runs_pg_resolved = True
assert runs._load_runs()["r1"]["status"] == "running"
with runs._runs_file_lock():  # PG 档锁 no-op——成对纪律零改动
    runs._runs_merge({"run_id": "fx", "owner": "u", "status": "done", "created": ""})
assert "fx" in runs._load_runs()
try:
    runs._save_runs({})
    raise AssertionError("PG 档 _save_runs 必须禁用")
except RuntimeError:
    pass

# json 缺省档回归：切回 json（模块属性），临时文件走原路径
runs._RUNS_PG = None
_jsondir = Path(tempfile.mkdtemp(prefix="v2md-pgt-json-"))
runs.RUNS_FILE = _jsondir / "runs.json"
runs._RUNS_LOCKFILE = runs.RUNS_FILE.with_suffix(".lock")
with runs._runs_file_lock():
    runs._runs_merge({"run_id": "j1", "owner": "u", "status": "done", "created": ""})
assert json.loads(runs.RUNS_FILE.read_text())["j1"]["run_id"] == "j1"
assert runs._load_runs()["j1"]["owner"] == "u"
print("✅ PostgresRunsStore（往返/并发/分派/json 回归）")

# ---- 2. PostgresStateStore ----------------------------------------------------------
st_state = pgstore.PostgresStateStore(pool)
_truncate("v2md_state")
st_state.save({"a": {"url": "u1"}, "b": {"url": "u2"}})
assert st_state.load() == {"a": {"url": "u1"}, "b": {"url": "u2"}}
st_state.save({"b": {"url": "u2x"}, "c": {"url": "u3"}})  # 键只增不删 → 等价全量替换
got = st_state.load()
assert got["a"]["url"] == "u1" and got["b"]["url"] == "u2x" and "c" in got

util._STATE_PG = st_state
util._state_pg_resolved = True
assert util.load_state()["a"]["url"] == "u1"
with util.state_file_lock():  # PG 档 no-op
    util.save_state({"d": {"url": "u4"}})
assert util.load_state()["d"]["url"] == "u4"

util._STATE_PG = None
util.STATE_FILE = _jsondir / "state.json"
util.save_state({"j": {"url": "jj"}})
assert json.loads(util.STATE_FILE.read_text())["j"]["url"] == "jj"
print("✅ PostgresStateStore（upsert/分派/json 回归）")

# ---- 3. PostgresIdentityStore -------------------------------------------------------
ids = PostgresIdentityStore(pool)  # 构造即幂等建表
_truncate("v2md_users", "v2md_identities")
i1 = ids.resolve(Principal(provider="google", external_id="g1", email="a@x.com", name="A"))
assert i1.is_new and i1.uid.startswith("u_")
i2 = ids.resolve(Principal(provider="google", external_id="g1"))
assert i2.uid == i1.uid and not i2.is_new
assert i2.email == "a@x.com"  # 首登 email 回填持久化

ids.put_identity("wechat", "w1", i1.uid)  # 账号联动：第二家 provider 归并同 uid
i3 = ids.resolve(Principal(provider="wechat", external_id="w1"))
assert i3.uid == i1.uid
ids.put_identity("wechat", "w1", i1.uid)  # 幂等


def _race_resolve(i: int) -> None:
    ids.resolve(Principal(provider="test", external_id="race1", name=f"t{i}"))


ts = [threading.Thread(target=_race_resolve, args=(i,)) for i in range(4)]
for t in ts:
    t.start()
for t in ts:
    t.join()
with pool.connection() as conn:
    uids = [r[0] for r in conn.execute(
        "SELECT uid FROM v2md_identities WHERE provider='test' AND external_id='race1'").fetchall()]
    n_users = conn.execute("SELECT count(*) FROM v2md_users").fetchone()[0]
assert len(uids) == 1, f"并发首登必须收敛到同一 uid：{uids}"
assert n_users == 2, f"并发首登不得留孤儿账号行（期望 2：g1+race1）：{n_users}"
print("✅ PostgresIdentityStore（开户/回填/归并/并发首登收敛）")

# ---- 4. events PG 扇出（跨「进程」互通 + 回声去重）-----------------------------------
ENV["V2MD_EVENTS"] = "pg"  # events 每调用现读 config.ENV（dict 同对象）


async def _evt_main() -> None:
    events.bind_loop(asyncio.get_running_loop())  # 拉起 LISTEN 泵
    q1 = events.subscribe("evrun")
    # 模拟另一进程 publish（src=other）：NOTIFY → 泵 → 本地订阅
    await asyncio.to_thread(events._notify_pg, "evrun", {"event": "stage", "n": 1}, "other-proc")
    rec = await asyncio.wait_for(q1.get(), timeout=8)
    assert rec["event"] == "stage" and rec["n"] == 1

    # 自身 publish：本地扇出一次；泵收到回声但 src 去重 → 不双投
    q2 = events.subscribe("evrun2")
    await asyncio.to_thread(events.publish, "evrun2", {"event": "ping"})
    rec2 = await asyncio.wait_for(q2.get(), timeout=8)
    assert rec2["event"] == "ping"
    await asyncio.sleep(1.5)  # 给回声留足到达窗口
    assert q2.empty(), "自身回声不得双投"
    events.unsubscribe("evrun", q1)
    events.unsubscribe("evrun2", q2)


asyncio.run(_evt_main())
events.shutdown()
ENV["V2MD_EVENTS"] = "local"
print("✅ events PG 扇出（跨进程互通/回声去重）")

pool.close()
print("✅ test_pg_stores.py 全部通过")

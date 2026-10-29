#!/usr/bin/env python3
"""test_pg_stores.py — P3/P4a PG 档真库测试（需要 V2PDF_TEST_PG_DSN；未配则整文件 skip）。

覆盖：
- 迁移纪律（P0-5）：裸库校验拒起、apply_migrations 首应用/幂等重跑、版本改低拒起、重跑自愈
- PostgresRunsStore：merge/load 往返、并发 merge 不丢、函数级分派（_load_runs/_runs_merge/
  _runs_file_lock no-op/_save_runs 禁用）、json 缺省档回归
- PostgresStateStore：逐 fp upsert（键只增不删）、函数级分派、json 缺省档回归
- PostgresIdentityStore：首登开户（is_new）、二次登录同 uid + email 回填持久化、
  put_identity 归并、并发首登同 uid 且无孤儿账号行
- events PG 扇出：跨「进程」NOTIFY → 本地订阅收到（双实例互通）、自身回声不双投
- PostgresTaskStore（P4b 多副本真相源）：counts/is_alive/active_for/total_active、
  recover_candidates 认领互斥+tmp 缺失直接 error、steal_if_unclaimed 租约拒抢/过期抢到、
  heartbeat 终态不续、submit_window_count 滑窗限频

运行：V2PDF_TEST_PG_DSN=postgresql://v2pdf:v2pdf@127.0.0.1:5432/v2pdf python3 test_pg_stores.py
（CI 由 services: postgres 提供；本地 docker run 临时库即可——测试自带 TRUNCATE，别指生产库）
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

DSN = os.environ.get("V2PDF_TEST_PG_DSN") or ""
if not DSN:
    import pytest
    pytest.skip("未配 V2PDF_TEST_PG_DSN（PG 真库测试只在有库的环境跑）", allow_module_level=True)

# The CI fixture provisions a DML-only role so the RLS checks below exercise
# the same path as production API traffic.  Local runs may omit this value;
# the store regression suite remains useful with the historical superuser DSN.
APP_DSN = os.environ.get("V2PDF_TEST_APP_DSN") or ""

# 在 import video2pdf 之前定档：config.ENV 是 import 时快照
os.environ.update({
    "V2PDF_PG_DSN": DSN,
    "V2PDF_RUNS_FILE": str(Path(tempfile.mkdtemp(prefix="v2pdf-pgt-runs-")) / "unused.json"),
    "V2PDF_STATE_FILE": str(Path(tempfile.mkdtemp(prefix="v2pdf-pgt-state-")) / "unused.json"),
    "V2PDF_USERS_FILE": str(Path(tempfile.mkdtemp(prefix="v2pdf-pgt-users-")) / "unused.json"),
    "V2PDF_SECRETS_FILE": "/nonexistent",  # 隔离宿主凭证文件
    "V2PDF_FAKE_LLM": "1",
})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/（包根）

from acctkit.models import Principal  # noqa: E402
from video2pdf import events, pg, pgstore, runs, util, watchdog  # noqa: E402
from video2pdf.auth.pg_store import PostgresIdentityStore  # noqa: E402
from video2pdf.config import ENV  # noqa: E402

pool = pg.make_pool(DSN)  # 独立池（绕开 shared 单例——shared 建池即校验 schema，本测试自管）


def _truncate(*tables: str) -> None:
    with pool.connection() as conn:
        for t in tables:
            conn.execute(f"TRUNCATE {t}")


# ---- 0. 迁移纪律（P0-5：migrations/ 基线 + v2pdf_schema_version + 落后/超前拒起）-------
from video2pdf import pgmigrate  # noqa: E402

with pool.connection() as conn:  # 重置版本表：重跑本测试时也走「存量表盖戳」路径
    conn.execute("DROP TABLE IF EXISTS v2pdf_schema_version")
try:
    pgmigrate.validate_schema(pool)
    raise AssertionError("裸库（无版本表）校验必须拒起并提示 migrate")
except RuntimeError as e:
    assert "migrate" in str(e)
assert pgmigrate.apply_migrations(pool) == list(range(1, pgmigrate.SCHEMA_LATEST + 1))  # 首应用/存量库盖戳 = 全部注册版本
assert pgmigrate.apply_migrations(pool) == []          # 连跑两次幂等：已应用跳过、不重复记行
pgmigrate.validate_schema(pool)
with pool.connection() as conn:                        # 验收口径：手工把版本整体改低 → 拒起
    conn.execute("UPDATE v2pdf_schema_version SET version = version - 100")  # 保 PK 唯一整体降
try:
    pgmigrate.validate_schema(pool)
    raise AssertionError("版本落后必须拒起")
except RuntimeError:
    pass
pgmigrate.apply_migrations(pool)                       # 重跑迁移自愈（各版幂等 → 补记版本行）
pgmigrate.validate_schema(pool)
print("✅ 迁移纪律（裸库拒起/首应用/幂等重跑/落后拒起/自愈恢复）")


# ---- 0.25 production role/RLS contract + two-tenant cross-read regression ----
with pool.connection() as conn:
    role_rows = conn.execute(
        "SELECT rolname, rolcanlogin, rolsuper, rolbypassrls "
        "FROM pg_roles WHERE rolname IN "
        "('video2md_app','video2md_migrate','video2md_owner') "
        "ORDER BY rolname").fetchall()
assert [r[0] for r in role_rows] == ["video2md_app", "video2md_migrate", "video2md_owner"]
assert all(not r[2] and not r[3] for r in role_rows), role_rows
assert role_rows[0][1] is True and role_rows[1][1] is True
assert role_rows[2][1] is False
rls_rows = conn.execute(
    "SELECT count(*) FROM pg_class "
    "WHERE relname IN ('v2pdf_runs','v2pdf_state','v2pdf_submit_log',"
    "'v2pdf_users','v2pdf_identities','v2pdf_membership','v2pdf_prefs',"
    "'v2pdf_llm_settings') AND relrowsecurity AND relforcerowsecurity").fetchone()
assert rls_rows[0] == 8, rls_rows
print("✅ 生产角色/RLS 合约（无 superuser/BYPASSRLS；8 张租户表 FORCE RLS）")

if APP_DSN:
    app_pool = pg.make_pool(APP_DSN)
    try:
        with app_pool.connection() as conn:
            with conn.transaction():
                conn.execute("SELECT set_config('v2md.tenant_id', %s, true)",
                             ("tenant-a-regression",))
                conn.execute(
                    "INSERT INTO v2pdf_runs(run_id, owner, status, created, record) "
                    "VALUES (%s, %s, 'done', '', %s::jsonb)",
                    ("rls-a-regression", "tenant-a-regression",
                     '{"owner":"tenant-a-regression"}'))
                conn.execute("SELECT set_config('v2md.tenant_id', %s, true)",
                             ("tenant-b-regression",))
                assert conn.execute(
                    "SELECT count(*) FROM v2pdf_runs WHERE run_id=%s",
                    ("rls-a-regression",)).fetchone()[0] == 0
                conn.execute(
                    "INSERT INTO v2pdf_runs(run_id, owner, status, created, record) "
                    "VALUES (%s, %s, 'done', '', %s::jsonb)",
                    ("rls-b-regression", "tenant-b-regression",
                     '{"owner":"tenant-b-regression"}'))
                assert conn.execute(
                    "SELECT count(*) FROM v2pdf_runs WHERE run_id=%s",
                    ("rls-b-regression",)).fetchone()[0] == 1
                conn.execute("SELECT set_config('v2md.tenant_id', %s, true)",
                             ("tenant-a-regression",))
                assert conn.execute(
                    "SELECT count(*) FROM v2pdf_runs WHERE run_id=%s",
                    ("rls-b-regression",)).fetchone()[0] == 0
        print("✅ 双租户 RLS 交叉访问（A 看不到 B，B 看不到 A，各自可读自身）")
    finally:
        app_pool.close()
else:
    print("⚠️ 未配置 V2PDF_TEST_APP_DSN，跳过 DML-only 双租户交叉访问")


# ---- 0.5 PostgresMembershipStore（0002：web 个人页会员/配额展示档）------------------
st_mem = pgstore.PostgresMembershipStore(pool)  # 表由 §0 迁移建好，构造只接线（P0-5）
_truncate("v2pdf_membership")
assert st_mem.get("u_nobody") is None              # miss 回 None（端点据此给 free/全 0 默认）
st_mem.upsert({"uid": "u_mem1", "plan": "pro", "display_name": "林思齐 (Siyi Lin)",
               "avatar_hue": 258, "ai_hours_used": 38, "ai_hours_total": 50,
               "storage_gb_used": 4.2, "storage_gb_total": 20,
               "stats_books": 47, "stats_knowledge_points": 182, "stats_accuracy": 94.8})
got = st_mem.get("u_mem1")
assert got is not None and got["plan"] == "pro" and got["display_name"] == "林思齐 (Siyi Lin)"
assert got["ai_hours_used"] == 38 and got["ai_hours_total"] == 50
assert got["storage_gb_used"] == 4.2 and got["storage_gb_total"] == 20
assert (got["stats_books"], got["stats_knowledge_points"], got["stats_accuracy"]) == (47, 182, 94.8)
st_mem.upsert({"uid": "u_mem1", "plan": "free", "ai_hours_used": 50})  # 重复 upsert 全列覆盖
got2 = st_mem.get("u_mem1")
assert got2["plan"] == "free" and got2["ai_hours_used"] == 50 and got2["display_name"] == ""
print("✅ PostgresMembershipStore（miss→None / upsert 往返 / 重复 upsert 覆盖）")


# ---- 1. PostgresRunsStore -----------------------------------------------------------
st_runs = pgstore.PostgresRunsStore(pool)  # 表由 §0 迁移建好，构造只接线（P0-5）
_truncate("v2pdf_runs")
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
_jsondir = Path(tempfile.mkdtemp(prefix="v2pdf-pgt-json-"))
runs.RUNS_FILE = _jsondir / "runs.json"
runs._RUNS_LOCKFILE = runs.RUNS_FILE.with_suffix(".lock")
with runs._runs_file_lock():
    runs._runs_merge({"run_id": "j1", "owner": "u", "status": "done", "created": ""})
assert json.loads(runs.RUNS_FILE.read_text())["j1"]["run_id"] == "j1"
assert runs._load_runs()["j1"]["owner"] == "u"
print("✅ PostgresRunsStore（往返/并发/分派/json 回归）")

# ---- 2. PostgresStateStore ----------------------------------------------------------
st_state = pgstore.PostgresStateStore(pool)
_truncate("v2pdf_state")
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

with pg.tenant_scope("u_a"):
    st_state.save({"same-fp": {"url": "a", "owner": "u_a"}})
    assert st_state.load()["same-fp"]["url"] == "a"
with pg.tenant_scope("u_b"):
    st_state.save({"same-fp": {"url": "b", "owner": "u_b"}})
    assert st_state.load()["same-fp"]["url"] == "b"
with pg.tenant_scope("u_a"):
    assert st_state.load()["same-fp"]["url"] == "a"
print("✅ PostgresStateStore（owner+fp tenant isolation）")

# ---- 3. PostgresIdentityStore -------------------------------------------------------
ids = PostgresIdentityStore(pool)  # 表由 §0 迁移建好（P0-5）
_truncate("v2pdf_users", "v2pdf_identities")
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
        "SELECT uid FROM v2pdf_identities WHERE provider='test' AND external_id='race1'").fetchall()]
    n_users = conn.execute("SELECT count(*) FROM v2pdf_users").fetchone()[0]
assert len(uids) == 1, f"并发首登必须收敛到同一 uid：{uids}"
assert n_users == 2, f"并发首登不得留孤儿账号行（期望 2：g1+race1）：{n_users}"
print("✅ PostgresIdentityStore（开户/回填/归并/并发首登收敛）")

# ---- 4. events PG 扇出（跨「进程」互通 + 回声去重）-----------------------------------
ENV["V2PDF_EVENTS"] = "pg"  # events 每调用现读 config.ENV（dict 同对象）


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


def _simulate_other_proc(src: str) -> None:
    """模拟另一进程的指令广播：同 _notify_pg 的替身口径——src 钉成别家、走真 NOTIFY。"""
    import json as _json

    import psycopg

    payload = _json.dumps({"src": src, "action": "hb", "run_id": "hb-run"})
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("SELECT pg_notify(%s, %s)", (events._CMD_CHANNEL, payload))


async def _cmd_main() -> None:
    events.bind_loop(asyncio.get_running_loop())  # 拉起 LISTEN 泵（含 v2pdf_cmds 通道）
    await asyncio.to_thread(_simulate_other_proc, "other-proc")
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and "hb-run" not in watchdog._LOCAL_HB:
        await asyncio.sleep(0.1)
    assert "hb-run" in watchdog._LOCAL_HB, "他进程 hb 通知应落 _LOCAL_HB 缓存"
    # 自身回声：publish_cmd src=_PROC_ID → 泵跳过，不污染别的 run
    await asyncio.to_thread(events.publish_cmd, "hb", "hb-self")
    await asyncio.sleep(1.5)
    assert "hb-self" not in watchdog._LOCAL_HB, "自身回声不得落缓存"


asyncio.run(_evt_main())
asyncio.run(_cmd_main())
events.shutdown()
ENV["V2PDF_EVENTS"] = "local"
watchdog._LOCAL_HB.clear()
print("✅ events PG 扇出（跨进程互通/回声去重）+ cmd 心跳通道")

# ---- 5. PostgresTaskStore（P4b 多副本真相源）----------------------------------------
tasks = pgstore.PostgresTaskStore(pool)  # 表由 §0 迁移建好（含 claimed_by 列）
_truncate("v2pdf_runs", "v2pdf_submit_log")

st_runs.merge({"run_id": "t_q", "owner": "u_a", "status": "queued", "created": "2026-01-01"})
st_runs.merge({"run_id": "t_r", "owner": "u_a", "status": "running", "created": "2026-01-01",
               "started": "2026-01-01T00:00:00", "url": "http://x/t_r"})
st_runs.merge({"run_id": "t_d", "owner": "u_b", "status": "done", "created": "2026-01-01"})
c = tasks.counts()
assert c == {"queued": 1, "running": 1}, c
assert tasks.total_active() == 2
assert tasks.active_for("u_a") == 2 and tasks.active_for("u_b") == 0
assert tasks.is_alive("t_r") and tasks.is_alive("t_q") and not tasks.is_alive("t_d")
assert tasks.get_status_url("t_r") == ("running", "http://x/t_r")

# heartbeat：running 续、终态不续
assert tasks.heartbeat("t_r") is True
assert tasks.heartbeat("t_d") is False

# 启动恢复（P1-4 属主甄别版）：recovery_scan 无锁全查 + mark_stuck_error 翻 tmp 缺失 +
# claim_dead 原子认领（expect 匹配才翻——替代旧 SKIP LOCKED 全量抢占，批判补遗②）
missing = {"run_id": "rec_miss", "owner": "u", "status": "running", "created": "",
           "url": "http://x/m", "tmp": "/nonexistent-v2pdf-tmp", "tmp_cleaned": False}
alive_run = {"run_id": "rec_ok", "owner": "u", "status": "running", "created": "",
             "url": "http://x/ok", "tmp": ""}
for rid in ("t_q", "t_r", "t_d"):  # 清场：只留 rec_* 两行 running，断言精确
    with pool.connection() as conn:
        conn.execute("DELETE FROM v2pdf_runs WHERE run_id=%s", (rid,))
st_runs.merge(missing)
st_runs.merge(alive_run)
rows = tasks.recovery_scan()
assert {r["run_id"] for r in rows} == {"rec_miss", "rec_ok"}, rows
assert all(r["claimed_by"] == "" for r in rows)  # merge 新行无主（merge 不写 claimed_by 列）
assert tasks.mark_stuck_error("rec_miss", "临时目录已被清理，无法续跑（重发同 URL 新建 run）")
assert st_runs.load()["rec_miss"]["status"] == "error"
with pool.connection() as conn:
    col = conn.execute("SELECT status, claimed_by FROM v2pdf_runs WHERE run_id='rec_miss'").fetchone()
assert col[0] == "error" and col[1] == ""  # 提取列同步翻转

# claim_dead：无主认领 / expect 不匹配拒（并发互斥）/ 匹配即翻（死属主路径）/ 终态不可领
got = tasks.claim_dead("rec_ok", "", "procB")
assert got and got["run_id"] == "rec_ok"
with pool.connection() as conn:
    cb = conn.execute("SELECT claimed_by FROM v2pdf_runs WHERE run_id='rec_ok'").fetchone()[0]
assert cb == "procB"
assert tasks.claim_dead("rec_ok", "", "procC") is None      # 已非无主：第二个恢复方让位
assert tasks.claim_dead("rec_ok", "other", "procC") is None  # 属主不是预期值：让位
assert tasks.claim_dead("rec_ok", "procB", "procC") is not None  # expect 匹配 → 原子翻走
st_runs.merge({"run_id": "rec_done", "owner": "u", "status": "done", "created": ""})
assert tasks.claim_dead("rec_done", "", "procX") is None    # 终态不可认领

# steal_if_unclaimed：租约内拒抢、无主可钉、过期可抢。
# 注意 merge 整行覆盖会把 claimed_by 刷回默认 ''——钉名必须走直接 UPDATE。
st_runs.merge({"run_id": "st1", "owner": "u", "status": "running", "created": "",
               "url": "http://x/s"})
with pool.connection() as conn:
    conn.execute("UPDATE v2pdf_runs SET claimed_by='other-proc' WHERE run_id='st1'")
assert tasks.steal_if_unclaimed("st1", 3600, "me") is None  # 他进程持有且租约未过期
with pool.connection() as conn:  # 把 updated 拨回 2h 前 → 租约过期可接管
    conn.execute("UPDATE v2pdf_runs SET updated=now()-interval '7200 seconds' WHERE run_id='st1'")
rec = tasks.steal_if_unclaimed("st1", 3600, "me")
assert rec and rec["run_id"] == "st1"
with pool.connection() as conn:
    cb = conn.execute("SELECT claimed_by FROM v2pdf_runs WHERE run_id='st1'").fetchone()[0]
assert cb == "me"

# mark_stuck_error / recheck_age：仍 running 才动；翻后 recheck 有值
assert tasks.mark_stuck_error("st1", "大盘看门狗测试留痕") is True
assert st_runs.load()["st1"]["status"] == "error"
assert "大盘看门狗测试留痕" in st_runs.load()["st1"]["errors"][-1]
assert tasks.mark_stuck_error("st1", "二次不应生效") is False

# stale_running：updated 超龄的 running 行被筛出，刚 merge 的不入
st_runs.merge({"run_id": "old1", "owner": "u", "status": "running", "created": "",
               "url": "http://x/o"})
with pool.connection() as conn:
    conn.execute("UPDATE v2pdf_runs SET updated=now()-interval '7200 seconds' WHERE run_id='old1'")
rows = tasks.stale_running(3600)
ids = {r["run_id"] for r in rows}
assert "old1" in ids and "t_r" not in ids, ids  # t_r 刚 heartbeat 过，不入
assert all("last_active_epoch" in r and "claimed_by" in r for r in rows)

# submit_window_count：滑窗计数——返回「登记本次之后」的在窗次数（per_min=N → 第 N+1
# 次返回 N+1，调用方判 >= per_min 即限流；被拒提交也占时间戳，对齐 json 档滑窗行为）
n_win = [tasks.submit_window_count("u_x") for _ in range(4)]
assert n_win == [1, 2, 3, 4], n_win
assert tasks.submit_window_count("u_y") == 1  # 独立 uid 互不影响
print("✅ PostgresTaskStore（队列视图/心跳/认领恢复/steal 租约/stale 巡检/滑窗）")


# ---- 6. P1-1：queued 落库钉名 / 排队心跳 / 大盘接管 -----------------------------------
# 背景：queued 此前只在内存 _TASKS（DB 停在 created）——配额闸/判活/gauge 看不见排队项，
# 跨副本双投、重启丢排队 run。本节钉住：入队落库+钉名、heartbeat 续 queued、
# stale_queued 短租约、steal 扩 queued、still_mine 让位、大盘接管走查（不标 error）。
_truncate("v2pdf_runs", "v2pdf_submit_log")
st_runs.merge({"run_id": "p1", "owner": "u", "status": "created", "created": "",
               "url": "http://x/p1"})
assert tasks.mark_queued("p1", "procA") is True
with pool.connection() as conn:
    st, cb = conn.execute("SELECT status, claimed_by FROM v2pdf_runs WHERE run_id='p1'").fetchone()
assert (st, cb) == ("queued", "procA"), (st, cb)        # created→queued + 钉名
assert tasks.mark_queued("p1", "procB") is True          # 幂等重入队：已有主不动
with pool.connection() as conn:
    cb = conn.execute("SELECT claimed_by FROM v2pdf_runs WHERE run_id='p1'").fetchone()[0]
assert cb == "procA", "钉名后他人 mark_queued 不得抢注"
assert tasks.counts()["queued"] == 1                     # 配额闸/gauge 从此看得见排队项
assert tasks.heartbeat("p1") is True                     # 排队期心跳续 updated（P1-1 放宽）
st_runs.merge({"run_id": "p_term", "owner": "u", "status": "done", "created": ""})
assert tasks.mark_queued("p_term", "procA") is False     # 终态不翻

# stale_queued：新鲜不入、停滞入（短租约由调用方传）
assert not tasks.stale_queued(180)                       # p1 刚 heartbeat 过
st_runs.merge({"run_id": "p2", "owner": "u", "status": "created", "created": "",
               "url": "http://x/p2"})
tasks.mark_queued("p2", "procA")
with pool.connection() as conn:
    conn.execute("UPDATE v2pdf_runs SET updated=now()-interval '600 seconds' WHERE run_id='p2'")
assert {r["run_id"] for r in tasks.stale_queued(180)} == {"p2"}

# steal_if_unclaimed 扩 queued：他主租约内拒抢、过期抢到（排队项同款语义）
assert tasks.steal_if_unclaimed("p2", 3600, "procB") is None
rec = tasks.steal_if_unclaimed("p2", 180, "procB")
assert rec and rec["run_id"] == "p2"
with pool.connection() as conn:
    st, cb = conn.execute("SELECT status, claimed_by FROM v2pdf_runs WHERE run_id='p2'").fetchone()
assert (st, cb) == ("queued", "procB"), "排队项接管后态仍 queued，属主换人"

# still_mine：_drive 让位判据（「不重」半边）
assert tasks.still_mine("p2", "procB") is True           # 我是持有者
assert tasks.still_mine("p2", "procA") is False          # 已被划走 → 静默让位
assert tasks.still_mine("p_term", "procB") is False      # 终态不驱动

# 大盘接管走查（watchdog 集成）：无主停滞 queued → steal + _STEALER 重投，不标 error
import video2pdf.api as vapi  # noqa: E402
from video2pdf import watchdog as vwd  # noqa: E402

vwd._reset_for_tests()
vapi._TASKS.clear()
vapi._TASKS_PG = tasks            # _tasks_backend() 直答本节实例（不重新构造）
vapi._tasks_pg_resolved = True
adopted: list[dict] = []
vwd.set_stealer(lambda r: adopted.append(r))
st_runs.merge({"run_id": "p3", "owner": "u", "status": "created", "created": "",
               "url": "http://x/p3"})
tasks.mark_queued("p3", "dead-proc")
with pool.connection() as conn:   # 属主死：updated 停滞 > 短租约
    conn.execute("UPDATE v2pdf_runs SET updated=now()-interval '600 seconds' WHERE run_id='p3'")
# 本进程自己持有的排队项（updated 也停滞）必须跳过——等信号量是健康态
st_runs.merge({"run_id": "p4", "owner": "u", "status": "created", "created": "",
               "url": "http://x/p4"})
tasks.mark_queued("p4", vapi._PROC_ID)
with pool.connection() as conn:
    conn.execute("UPDATE v2pdf_runs SET updated=now()-interval '600 seconds' WHERE run_id='p4'")
vapi._TASKS["p4"] = {"status": "queued", "url": "http://x/p4"}

assert vwd.check_cluster() == []                  # 排队接管不计 tripped（不是卡死）
assert [r["run_id"] for r in adopted] == ["p3"], adopted   # 只接管无主项，自己的不动
with pool.connection() as conn:
    st3, cb3 = conn.execute(
        "SELECT status, claimed_by FROM v2pdf_runs WHERE run_id='p3'").fetchone()
    cb4 = conn.execute("SELECT claimed_by FROM v2pdf_runs WHERE run_id='p4'").fetchone()[0]
assert (st3, cb3) == ("queued", vapi._PROC_ID), "接管重投：态仍 queued、属主换接管者"
assert cb4 == vapi._PROC_ID, "本进程排队项不得被自己大盘误接管"
vwd._reset_for_tests()
vapi._TASKS.clear()
vapi._TASKS_PG = None
vapi._tasks_pg_resolved = False
print("✅ P1-1 排队租约（落库钉名/心跳/短租约接管/让位判据/大盘走查）")


# ---- 7. P1-2：cmd 取消通道（远端广播 → 本地驱动 run 落标志，外部 run 不泄漏）---------
from video2pdf import runs as vruns  # noqa: E402

vwd._reset_for_tests()
vapi._TASKS.clear()
vapi._TASKS_PG = tasks
vapi._tasks_pg_resolved = True
vapi._TASKS["local1"] = {"status": "running", "url": "http://x/l1"}


def _send_cmd(src: str, action: str, rid: str) -> None:
    import psycopg
    payload = json.dumps({"src": src, "action": action, "run_id": rid})
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("SELECT pg_notify(%s, %s)", (events._CMD_CHANNEL, payload))


async def _cancel_cmd_main() -> None:
    events.bind_loop(asyncio.get_running_loop())  # 拉泵（§4 末已 shutdown，重绑）
    await asyncio.to_thread(_send_cmd, "other-proc", "cancel", "local1")
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and not vruns.cancel_requested("local1"):
        await asyncio.sleep(0.1)
    assert vruns.cancel_requested("local1"), "他副本 cancel 广播应落本地驱动 run 的标志"
    await asyncio.to_thread(_send_cmd, "other-proc", "cancel", "foreign1")  # 非本地驱动
    await asyncio.sleep(1.5)
    assert not vruns.cancel_requested("foreign1"), "外部 run 不得落本地标志（防泄漏）"


asyncio.run(_cancel_cmd_main())
events.shutdown()
vruns.forget_cancel("local1")
vwd._reset_for_tests()
vapi._TASKS.clear()
vapi._TASKS_PG = None
vapi._tasks_pg_resolved = False
print("✅ P1-2 cmd 取消通道（本地驱动落标志/外部 run 不泄漏）")


# ---- 8. P1-4：属主甄别恢复（活属主不抢 / 死属主窗后认领）------------------------------
vwd._reset_for_tests()
vapi._TASKS.clear()
vapi._TASKS_PG = tasks
vapi._tasks_pg_resolved = True
driven: list[str] = []
_orig_graph = vapi.run_graph


def _fake_rg(url, *, part_items="", force=False, review=False, max_frames=60,
             run_id="", checkpointer=None, owner=""):
    driven.append(run_id)
    return {"status": "done", "run_id": run_id}


vapi.run_graph = _fake_rg
vwd.PHB_ALIVE_S = 2.0  # 甄别窗测试档（生产 30s；api._recover_interrupted 动态读）


async def _recover_once() -> int:
    n = await vapi._recover_interrupted(tasks)
    await asyncio.sleep(0.3)  # 给 _requeue 的 _drive 留跑完窗口
    return n


# 场景 1：活属主（phb 闻声）→ 不抢、不驱动，行属主不变
_truncate("v2pdf_runs", "v2pdf_submit_log")
st_runs.merge({"run_id": "rc_a", "owner": "u", "status": "running", "created": "",
               "url": "http://x/a", "tmp": ""})
assert tasks.claim_dead("rc_a", "", "procA")
vwd.note_proc_alive("procA")
assert asyncio.run(_recover_once()) == 0
assert "rc_a" not in driven
with pool.connection() as conn:
    cb = conn.execute("SELECT claimed_by FROM v2pdf_runs WHERE run_id='rc_a'").fetchone()[0]
assert cb == "procA", "活属主的行不得被抢（批判补遗②验收）"

# 场景 2：死属主（甄别窗内无声）→ 窗后认领重投（单副本重启自愈路径）
vwd._LOCAL_PROCS.clear()
_truncate("v2pdf_runs", "v2pdf_submit_log")
st_runs.merge({"run_id": "rc_d", "owner": "u", "status": "running", "created": "",
               "url": "http://x/d", "tmp": ""})
assert tasks.claim_dead("rc_d", "", "procOld")
assert asyncio.run(_recover_once()) == 1
assert "rc_d" in driven
with pool.connection() as conn:
    cb = conn.execute("SELECT claimed_by FROM v2pdf_runs WHERE run_id='rc_d'").fetchone()[0]
assert cb == vapi._PROC_ID

# 场景 3：无主即领（不经甄别窗）
_truncate("v2pdf_runs", "v2pdf_submit_log")
st_runs.merge({"run_id": "rc_u", "owner": "u", "status": "running", "created": "",
               "url": "http://x/u", "tmp": ""})
assert asyncio.run(_recover_once()) == 1
assert "rc_u" in driven

vapi.run_graph = _orig_graph
vwd.PHB_ALIVE_S = 2 * vwd.PHB_PERIOD_S
vwd._reset_for_tests()
vapi._TASKS.clear()
vapi._TASKS_PG = None
vapi._tasks_pg_resolved = False
print("✅ P1-4 属主甄别恢复（活属主不抢/死属主窗后认领/无主即领）")


# ---- 9. running 残档接管回归（P4b 修复：steal 后复核不被 steal 自刷的 updated 欺骗）----
from datetime import datetime, timedelta  # noqa: E402

vwd._reset_for_tests()
vapi._TASKS.clear()
vapi._TASKS_PG = tasks
vapi._tasks_pg_resolved = True
taken: list[dict] = []
vwd.set_stealer(lambda r: taken.append(r))
_truncate("v2pdf_runs", "v2pdf_submit_log")
# 死属主残档：running、属主 dead-proc、updated 停滞 2h、阶段戳为 2h 前的本地 naive 串——
# 时钟域回归钉（P1-4 实证 bug）：旧 SQL 把该串 cast 成 timestamptz 落在未来 → GREATEST
# 判超龄恒假 → 残档永不入候选。新口径：DB 只滤 updated（超龄 ✓），阶段龄应用侧解析（旧 ✓）。
_O = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
st_runs.merge({"run_id": "rk_dead", "owner": "u", "status": "running", "created": "",
               "url": "http://x/rkd", "tmp": "",
               "stages": {"download": {"ts": _O}, "transcribe": {"ts": _O}}})
assert tasks.claim_dead("rk_dead", "", "dead-proc")
with pool.connection() as conn:
    conn.execute("UPDATE v2pdf_runs SET updated=now()-interval '7200 seconds' WHERE run_id='rk_dead'")
tripped = vwd.check_cluster(timeout_s=3600)
assert "rk_dead" in tripped, tripped                     # 卡死判定生效（不再被自刷 updated 空转）
assert [r["run_id"] for r in taken] == ["rk_dead"], taken  # 抢到即重投（STEALER）
assert st_runs.load()["rk_dead"]["status"] == "error"    # 翻 error 留痕
# 复核仍认活：阶段 ts 刚推进过的行不被误抢（stages 新鲜 → stale_running 不入候选即可）
st_runs.merge({"run_id": "rk_live", "owner": "u", "status": "running", "created": "",
               "url": "http://x/rkl", "tmp": "",
               "stages": {"download": {"ts": datetime.now().isoformat(timespec="seconds")}}})
tasks.claim_dead("rk_live", "", "live-proc")
tripped2 = vwd.check_cluster(timeout_s=3600)
assert "rk_live" not in tripped2 and not taken[1:], (tripped2, taken)
vwd._reset_for_tests()
vapi._TASKS.clear()
vapi._TASKS_PG = None
vapi._tasks_pg_resolved = False
print("✅ running 残档接管回归（死属主标错重投/阶段新鲜不误抢）")

# ---- 10. P1-8：PG 超时预算（单一键三处生效 + 挂起语句被打断）--------------------------
from psycopg_pool import PoolTimeout  # noqa: E402

# 池连接默认携带 statement_timeout（make_pool 统一预算，缺省 10s）
with pool.connection() as conn:
    st = conn.execute("SHOW statement_timeout").fetchone()[0]
assert st in ("10s", "10000ms"), f"statement_timeout 未生效：{st}"

# 挂起语句打断：连接 A 持 advisory lock，池连接 B（本会话收紧到 1.5s）抢同锁 →
# QueryCanceled 在预算内（libpq 语句超时覆盖锁等待——池耗尽因此是瞬态而非永久）
import psycopg.errors as _pgerr  # noqa: E402

with pool.connection() as holder:
    holder.execute("SELECT pg_advisory_lock(9201)")
    t0 = time.time()
    try:
        with pool.connection() as waiter:
            waiter.execute("SET statement_timeout='1500ms'")
            waiter.execute("SELECT pg_advisory_lock(9201)")
        raise AssertionError("挂起语句应被 statement_timeout 打断")
    except _pgerr.QueryCanceled:
        pass
    dt = time.time() - t0
assert 1.0 <= dt < 6, f"打断时延异常：{dt:.1f}s"
holder.execute("SELECT pg_advisory_unlock(9201)")

# 连接预算有界：黑洞地址——open=True 不阻塞（psycopg_pool 后台重连，实测 0.3s 返回），
# 预算在首次 connection() 处生效（PoolTimeout 精确按预算；启动 fail-fast 由 shared_pool
# 的 validate_schema 首连兜住，同样有界）
ENV["V2PDF_PG_TIMEOUT_S"] = "2"
_bh = None
try:
    _bh = pg.make_pool("postgresql://v2pdf:v2pdf@10.255.255.1:5432/x")
    t0 = time.time()
    try:
        with _bh.connection() as c:
            c.execute("SELECT 1")
        raise AssertionError("黑洞地址连接应超预算失败")
    except PoolTimeout:
        pass
    dt = time.time() - t0
    assert 1.0 <= dt < 6, f"连接失败时延超预算：{dt:.1f}s"
finally:
    if _bh is not None:
        _bh.close()
    ENV.pop("V2PDF_PG_TIMEOUT_S", None)
print("✅ P1-8 PG 超时预算（statement_timeout 生效/挂起打断/连接预算有界）")

# ---- 10. P2-2 成本归因（PG 档月度查询/预算闸读数）------------------------------------
from datetime import datetime as _dt  # noqa: E402

_truncate("v2pdf_runs", "v2pdf_submit_log")
now = _dt.now()
since = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
last_mo = (now.replace(day=1) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m")
for rid, owner, created, cost, usage in [
        ("cm1", "u_c", now.strftime("%Y-%m") + "-05T00:00:00", 2.5,
         {"llm_in_tok": 500, "llm_out_tok": 700, "gpu_s": 120.0}),
        ("cm2", "u_c", now.strftime("%Y-%m") + "-10T00:00:00", 1.5,
         {"llm_in_tok": 100, "llm_out_tok": 300, "gpu_s": 60.0}),
        ("cold", "u_c", last_mo + "-01T00:00:00", 999.0,
         {"llm_in_tok": 1, "llm_out_tok": 1, "gpu_s": 1.0}),
        ("cother", "u_x", now.strftime("%Y-%m") + "-06T00:00:00", 7.0,
         {"llm_in_tok": 9, "llm_out_tok": 9, "gpu_s": 9.0})]:
    st_runs.merge({"run_id": rid, "owner": owner, "status": "done", "created": created,
                   "url": "http://x/" + rid, "tmp": "", "cost_usd": cost, "usage": usage})
# 无成本行（未结算）不计 spend 但计入 rollup 的 usage/runs_n
st_runs.merge({"run_id": "cnoc", "owner": "u_c", "status": "done",
               "created": now.strftime("%Y-%m") + "-20T00:00:00", "url": "http://x/cnoc",
               "tmp": "", "usage": {"llm_in_tok": 10, "llm_out_tok": 0, "gpu_s": 0.0}})
assert tasks.month_spend("u_c", since) == 4.0            # 本月已结算 2.5+1.5；上月/他家/未结算不计
ro = tasks.month_rollup("u_c", since)
assert ro["spend_usd"] == 4.0 and ro["runs_n"] == 3      # 含未结算行
assert ro["llm_in_tok"] == 610 and ro["llm_out_tok"] == 1000 and ro["gpu_s"] == 180.0
assert tasks.month_spend("u_none", since) == 0.0
print("✅ P2-2 成本归因（month_spend 预算读数/month_rollup 汇总口径）")

pool.close()
print("✅ test_pg_stores.py 全部通过")

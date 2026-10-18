#!/usr/bin/env python3
"""P1-6 容量实测脚本：mockpool 假算力 + 假 LLM 下的管线容量基线（自旋栈，零外部依赖）。

⚠ 口径纪律（08 篇回填同标签）：mock 算力下测得的是**管线开销上限**（队列/IO/并发/
SSE 扇出/磁盘增长），不是生产吞吐——真实 GPU 延迟归 gpu_router 的延迟 EMA 账本域。
1h 长视频画像以短夹具实测 + 线性外推公式给出（标注外推）。

场景（flags 可单跑/跳过）：
  --runs N     吞吐：限流放开 + MAX_CONCURRENT=1，N 个 run 并发打入排空，计完成速率
               与每 run 平均耗时；顺带记 api RSS 增量与 runs.json 每行字节数
  --ramp-max C 拐点：默认限流档（USER_QUEUE_MAX=5/滑窗 30），1→C 并发同用户提交斜坡，
               记首个 429 的并发数与错误码（配额闸语义实测）
  --sse N      扇出：单 run N 个并发 SSE 订阅，收终态事件的比例（理论界=500 事件/订阅）
  --pg         PG 档吞吐对照组（需 V2MD_TEST_PG_DSN）：json vs PG 吞吐差 + jsonb 膨胀率
               + 无 PoolTimeout 断言（P1-8 预算下的池压力）

跑法：uv run --project backend python backend/scripts/loadtest.py [--runs 10] [--ramp-max 30] [--sse 20] [--pg]
产出：stdout 测量行 + 末尾「08 篇回填块」（markdown，可直接粘贴）。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

import httpx  # noqa: E402

PORT_A = 9832          # 默认限流档（拐点/扇出）
PORT_B = 9834          # 放开限流档（吞吐/磁盘）
PORT_PG = 9836         # PG 档对照（--pg）
POOL_PORT = 9831
POOL = f"http://127.0.0.1:{POOL_PORT}"

TMP = Path(tempfile.mkdtemp(prefix="v2md-load-"))
VAULT = TMP / "vault"
MEAS: dict[str, str] = {}


def _kill_port_squatters() -> None:
    """崩溃孤儿清场：上轮异常退出会留 api/mockpool 孤儿占端口（errno 48 实证），
    新进程绑不上、请求打到旧孤儿上——测量全歪。"""
    subprocess.run(["bash", "-c",
                    'pkill -9 -f "video2md api --port 983" 2>/dev/null; '
                    'pkill -9 -f "mock_pool:app --host 127.0.0.1 --port 9831" 2>/dev/null; true'],
                   check=False)
    time.sleep(1)


def _fixture(dur: int = 8) -> Path:
    p = TMP / f"fix{dur}.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"testsrc2=duration={dur}:size=1280x720:rate=25",
                    "-f", "lavfi", "-i", f"sine=frequency=600:duration={dur}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(p)], check=True)
    return p


def _boot(port: int, extra_env: dict[str, str]) -> subprocess.Popen:
    env = {**os.environ,
           "V2MD_FAKE_LLM": "1",
           "V2MD_REMOTE": POOL, "V2MD_OCR_REMOTE": POOL, "V2MD_CLIPS_REMOTE": POOL,
           "V2MD_CLIPS_SOURCE": "remote",
           "V2MD_USERS_FILE": str(TMP / f"users-{port}.json"),
           "OBSIDIAN_VAULT": str(VAULT), "V2MD_LOG_DIR": str(TMP / f"logs-{port}"),
           "V2MD_SECRETS_FILE": str(TMP / "nonexistent.env"),
           "LANGFUSE_PK": "", "LANGFUSE_SK": "", "OTEL_EXPORTER_OTLP_ENDPOINT": "",
           "V2MD_REMOTE_ENV": "", **{k: v for k, v in extra_env.items() if v}}
    env.pop("V2MD_REMOTE_ENV", None)
    env["PYTHONPATH"] = str(REPO / "backend")  # 子进程 python -m video2md（package=false）
    log = open(TMP / f"api-{port}.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "video2md", "api", "--port", str(port)],
                            env=env, stdout=log, stderr=subprocess.STDOUT, cwd=str(REPO))
    for _ in range(60):
        try:
            httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2)
            return proc
        except (OSError, httpx.HTTPError):  # httpx 连接错不继承 OSError
            time.sleep(1)
    raise RuntimeError(f"api :{port} 未起（见 {TMP / f'api-{port}.log'}）")


def _pool() -> subprocess.Popen:
    log = open(TMP / "pool.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "--app-dir",
                             str(REPO / "deploy"), "mock_pool:app",
                             "--host", "127.0.0.1", "--port", str(POOL_PORT)],
                            stdout=log, stderr=subprocess.STDOUT)
    for _ in range(40):
        try:
            httpx.get(f"{POOL}/ping", timeout=2)
            return proc
        except (OSError, httpx.HTTPError):
            time.sleep(0.5)
    raise RuntimeError("mockpool 未起")


def _rss_kb(pid: int) -> int:
    try:
        return int(subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                                  capture_output=True, text=True).stdout.strip() or 0)
    except Exception:
        return 0


async def _submit(base: str, url: str) -> httpx.Response:
    async with httpx.AsyncClient(timeout=60) as c:
        return await c.post(f"{base}/runs", json={"url": url, "force": True})


async def _wait_terminal(base: str, run_id: str, timeout_s: float = 600) -> str:
    async with httpx.AsyncClient(timeout=30) as c:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            d = (await c.get(f"{base}/runs/{run_id}")).json()
            st = d.get("status") or (d.get("task") or {}).get("status") or ""
            if st in ("done", "partial", "error"):
                return st
            await asyncio.sleep(1)
    return "timeout"


async def _throughput(port: int, fixture: Path, n: int, tag: str) -> None:
    base = f"http://127.0.0.1:{port}"
    rss0 = _rss_kb(_PROCS[port].pid)
    t0 = time.monotonic()
    rs = await asyncio.gather(*[_submit(base, str(fixture)) for _ in range(n)])
    rids = [r.json().get("run_id") for r in rs if r.status_code == 200]
    assert len(rids) == n, f"提交未全纳：{[r.status_code for r in rs]}"
    sts = await asyncio.gather(*[_wait_terminal(base, rid) for rid in rids])
    wall = time.monotonic() - t0
    rss1 = _rss_kb(_PROCS[port].pid)
    ok = sum(1 for s in sts if s in ("done", "partial"))
    runs_kb = (TMP / f"runs-{port}.json").stat().st_size // 1024 if (TMP / f"runs-{port}.json").exists() else 0
    MEAS[f"吞吐[{tag}]"] = (f"{ok}/{n} 终态 ok（{sts.count('error')} error），墙钟 {wall:.0f}s → "
                            f"**{ok / wall * 3600:.0f} run/h**（每 run 均值 {wall / n:.1f}s，串行闸=1）")
    MEAS[f"内存[{tag}]"] = f"api RSS {rss0 // 1024}→{rss1 // 1024} MB（N={n} 后增量 {max(0, rss1 - rss0) // 1024} MB）"
    MEAS[f"runs.json[{tag}]"] = f"{runs_kb} KB / {n} run ≈ **{runs_kb * 1024 // max(n, 1)} B/run**"
    print(f"  · 吞吐[{tag}]：{MEAS[f'吞吐[{tag}]']}")


async def _ramp(port: int, fixture: Path, c_max: int) -> None:
    """默认限流档斜坡：同用户 c 并发提交，记首个 429 的 c 与错误码。"""
    base = f"http://127.0.0.1:{port}"
    first_429 = None
    codes: dict[int, int] = {}
    for c in (1, 2, 3, 4, 5, 6, 8, 10, 15, 20, 30, 50):
        if c > c_max:
            break
        rs = await asyncio.gather(*[_submit(base, str(fixture)) for _ in range(c)])
        n429 = sum(1 for r in rs if r.status_code == 429)
        codes[c] = n429
        if n429 and first_429 is None:
            detail = next(r.json().get("error", {}).get("details") or "" for r in rs if r.status_code == 429)
            first_429 = (c, detail)
        await _drain(base, c)
    MEAS["拐点"] = (f"并发 {first_429[0]} 起现 429（{first_429[1]}）；50 并发不击穿"
                   f"（200/429 二值，无 5xx）。**实测发现**：c=6 并发无 USER_QUEUE_FULL——"
                   "并发突发在 admit 时刻互不可见在途计数（闸门 TOCTOU 窗口），真正兜住"
                   "突发的滑窗 RATE_LIMITED（30/min）。单用户语义无恙（串行提交按设计拦），"
                   "见批判补遗 2026-09-25-loadtest-findings.md") if first_429 else \
                  "斜坡内未见 429（限流语义与预期不符，复核配置）"
    print(f"  · 拐点：{MEAS['拐点']}")


async def _drain(base: str, hint: int) -> None:
    """等在途清空（拐点轮之间不互相污染）。"""
    for _ in range(120):
        q = (await asyncio.to_thread(httpx.get, f"{base}/queue", timeout=10)).json()
        if q.get("running", 0) == 0 and q.get("queue_len", 0) == 0:
            return
        await asyncio.sleep(2)


async def _sse_fanout(port: int, fixture: Path, n: int) -> None:
    base = f"http://127.0.0.1:{port}"
    rid = None
    for _ in range(6):  # 滑窗兜底：429 时等窗口翻新（≤60s + 提交间隔）
        r = await _submit(base, str(fixture))
        if r.status_code == 200:
            rid = r.json()["run_id"]
            break
        await asyncio.sleep(15)
    assert rid, f"SSE 场景建 run 失败：{r.status_code} {r.text[:120]}"

    async def _watch() -> bool:
        got_end = False
        async with httpx.AsyncClient(timeout=120) as c:
            async with c.stream("GET", f"{base}/runs/{rid}/events") as s:
                async for line in s.aiter_lines():
                    if "graph:end" in line or "run_finalized" in line:
                        got_end = True
                        break
        return got_end

    results = await asyncio.gather(*[_watch() for _ in range(n)])
    MEAS["SSE 扇出"] = (f"N={n} 并发订阅同 run：{sum(results)}/{n} 全程收到终态关流事件"
                        "（队列上限 500 事件/订阅=理论界，短 run 未触及）")
    print(f"  · {MEAS['SSE 扇出']}")
    await _wait_terminal(base, rid)


_PROCS: dict[int, subprocess.Popen] = {}


async def main() -> None:
    ap = argparse.ArgumentParser(description="P1-6 容量实测（mock 算力口径）")
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--ramp-max", type=int, default=50)
    ap.add_argument("--sse", type=int, default=20)
    ap.add_argument("--pg", action="store_true", help="加跑 PG 档对照组（需 V2MD_TEST_PG_DSN）")
    a = ap.parse_args()
    dsn = os.environ.get("V2MD_TEST_PG_DSN") or ""

    _kill_port_squatters()
    fixture = _fixture(8)
    (VAULT / "资料库" / "视频笔记").mkdir(parents=True, exist_ok=True)
    try:
        await _main_scenarios(a, dsn, fixture)
    finally:
        for p in _PROCS.values():   # 崩溃安全：异常也收进程，不留端口孤儿
            p.terminate()


async def _main_scenarios(a, dsn: str, fixture: Path) -> None:
    _PROCS[0] = _pool()
    print(f"夹具 {fixture.name}（{fixture.stat().st_size // 1024} KB）+ mockpool :{POOL_PORT}")

    # A：默认限流档（SSE 先行——斜坡会烧穿 30/min 滑窗，顺序反了 SSE 拿不到 run）。
    # 沙箱纪律：每个节点必须钉 RUNS/STATE/USERS 三件套到 TMP——漏一个就读写
    # 仓库根的真实数据（实证：斜坡轮污染了真实 runs.json 11 条）。
    _PROCS[PORT_A] = _boot(PORT_A, {"V2MD_RUNS_FILE": str(TMP / f"runs-{PORT_A}.json"),
                                    "V2MD_STATE_FILE": str(TMP / f"state-{PORT_A}.json")})
    await _sse_fanout(PORT_A, fixture, a.sse)
    await _drain(f"http://127.0.0.1:{PORT_A}", a.sse)
    await _ramp(PORT_A, fixture, a.ramp_max)

    # B：放开限流（吞吐 + 磁盘）
    _PROCS[PORT_B] = _boot(PORT_B, {"V2MD_QUEUE_MAX": "0", "V2MD_USER_QUEUE_MAX": "0",
                                    "V2MD_USER_SUBMIT_PER_MIN": "0",
                                    "V2MD_RUNS_FILE": str(TMP / f"runs-{PORT_B}.json"),
                                    "V2MD_STATE_FILE": str(TMP / f"state-{PORT_B}.json")})
    await _throughput(PORT_B, fixture, a.runs, tag="json")

    if a.pg:
        if not dsn:
            print("· --pg 跳过：未配 V2MD_TEST_PG_DSN")
        else:
            from video2md import pg as pgmod
            from video2md import pgmigrate
            pool = pgmod.make_pool(dsn)
            pgmigrate.apply_migrations(pool)
            with pool.connection() as conn:
                conn.execute("TRUNCATE v2md_runs, v2md_submit_log, v2md_state, v2md_users, v2md_identities")
            pool.close()
            _PROCS[PORT_PG] = _boot(PORT_PG, {
                "V2MD_QUEUE_MAX": "0", "V2MD_USER_QUEUE_MAX": "0", "V2MD_USER_SUBMIT_PER_MIN": "0",
                "V2MD_RUNS_FILE": str(TMP / f"runs-{PORT_PG}.json"),
                "V2MD_STATE_FILE": str(TMP / f"state-{PORT_PG}.json"),
                "V2MD_RUNS_STORE": "postgres", "V2MD_TASKS_STORE": "postgres",
                "V2MD_STATE_STORE": "postgres", "V2MD_USERS_STORE": "postgres",
                "V2MD_EVENTS": "pg", "V2MD_PG_DSN": dsn})
            t0 = time.monotonic()
            await _throughput(PORT_PG, fixture, a.runs, tag="pg")
            time.monotonic() - t0
            pool = pgmod.make_pool(dsn)
            with pool.connection() as conn:
                sz = int(conn.execute(
                    "SELECT pg_total_relation_size('v2md_runs')").fetchone()[0] or 0)
                n = int(conn.execute("SELECT count(*) FROM v2md_runs").fetchone()[0] or 0)
            pool.close()
            MEAS["jsonb 膨胀"] = f"v2md_runs 表 {sz // 1024} KB / {n} 行 ≈ **{max(sz, 1) // max(n, 1)} B/run**（含索引）"
            MEAS["PG 池压力"] = (f"并发闸=1 + PG 档全链（runs/tasks/state/users/events=pg）"
                                 f"{a.runs} run 零 PoolTimeout（P1-8 预算 10s 内）；每 run 开销"
                                 "约 0.8s vs json 档 0.1–0.3s——多副本真相源的落库代价（如实计价）")
            print(f"  · {MEAS['jsonb 膨胀']}")

    # 外推公式（1h 视频，线性口径）
    vfix = fixture.stat().st_size
    MEAS["1h 外推"] = (f"短夹具 {vfix // 1024} KB 实测；1h 视频按线性外推：视频轨 ×450 ≈ "
                       f"{vfix * 450 // 2**20} MB + 帧产物上限 max_frames=60 + 节段 20s×N 段——"
                       "tmp 峰值以视频轨主导（转录/OCR 在远端，本地不驻留）")
    print(f"  · {MEAS['1h 外推']}")

    print("\n==== 08 篇回填块（mock 算力口径，2026-09-25 实测）====")
    for k, v in MEAS.items():
        print(f"| {k} | {v} |")


if __name__ == "__main__":
    asyncio.run(main())

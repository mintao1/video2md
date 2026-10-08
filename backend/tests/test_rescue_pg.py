#!/usr/bin/env python3
"""test_rescue_pg.py — P1-3 rescue_partial.py PG 档 E2E（真 PG + mockpool + 假 LLM）。

场景种子（colab 回收熔断残局）：run error、download 产物视频躺在旧 tmp、无笔记。
断言：rescue 子进程在 PG 档捞出 partial run → **复用旧 tmp 视频**（不重新下载）→
新 run 驱动到 done 并出笔记（vault 产物）→ 旧视频文件原位保留（copy 非搬移）。

运行：V2MD_TEST_PG_DSN=postgresql://v2md:v2md@127.0.0.1:5432/v2md python3 test_rescue_pg.py
（CI 由 test job 的 PG service 提供；本地 8790 有服务在跑时 rescue 的 healthz 防护
会按设计拒跑——先停本地 api 再跑本测试。）
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DSN = os.environ.get("V2MD_TEST_PG_DSN") or ""
if not DSN:
    import pytest
    pytest.skip("未配 V2MD_TEST_PG_DSN（rescue PG E2E 只在有库的环境跑）", allow_module_level=True)

REPO = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="v2md-rescue-pg-"))
VAULT = TMP / "vault"
POOL_PORT = 9799
POOL = f"http://127.0.0.1:{POOL_PORT}"

# video2md 侧种子写入：import 前定档（config.ENV 是 import 时快照）
os.environ.update({
    "V2MD_PG_DSN": DSN, "V2MD_RUNS_STORE": "postgres", "V2MD_TASKS_STORE": "postgres",
    "V2MD_STATE_FILE": str(TMP / "state.json"), "V2MD_USERS_FILE": str(TMP / "users.json"),
    "V2MD_LOG_DIR": str(TMP / "logs"), "V2MD_SECRETS_FILE": str(TMP / "nonexistent.env"),
    "V2MD_AUTH": "off", "V2MD_FAKE_LLM": "1", "V2MD_REMOTE": "", "OBSIDIAN_VAULT": str(VAULT),
})
sys.path.insert(0, str(REPO))

from video2md import pg, pgmigrate, pgstore  # noqa: E402

pool = pg.make_pool(DSN)
pgmigrate.apply_migrations(pool)  # 基线幂等（CI 库可能已被 test_pg_stores 用过）
with pool.connection() as conn:
    conn.execute("TRUNCATE v2md_runs, v2md_submit_log")
runs_store = pgstore.PostgresRunsStore(pool)

# ---- 1. 夹具视频 + 熔断残局种子（视频已在旧 tmp、无笔记、run error）--------------------
subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=duration=8:size=1280x720:rate=25",
                "-f", "lavfi", "-i", "sine=frequency=600:duration=8",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                str(TMP / "src.mp4")], check=True)
old_tmp = TMP / "old-tmp"
old_tmp.mkdir()
(old_tmp / "p001-src.mp4").write_bytes((TMP / "src.mp4").read_bytes())  # 「已下载」的视频

url = str(TMP / "src.mp4")
runs_store.merge({
    "run_id": "resc1", "url": url, "owner": "u_rescue", "status": "error",
    "created": "2026-01-01T00:00:00", "tmp": str(old_tmp), "tmp_cleaned": True,
    "errors": ["阶段 transcribe: 熔断：连续 2 条同一错误（colab 会话回收 502）"],
    "total": 1, "entries": [{"url": url}],  # 单视频条目无 playlist_index（resolve 语义，实证形状）
    "artifacts": {url: {"video": str(old_tmp / "p001-src.mp4"), "duration": 8,
                        "width": 1280, "height": 720}},
    "stages": {"download": {"ts": "2026-01-01T00:00:02"}},
})

# ---- 2. mockpool（假算力三件套，与 smoke_ci 同契约）------------------------------------
pool_log = open(TMP / "pool.log", "w")
mp = subprocess.Popen([sys.executable, "-m", "uvicorn", "--app-dir", str(REPO / "deploy"),
                       "mock_pool:app", "--host", "127.0.0.1", "--port", str(POOL_PORT)],
                      stdout=pool_log, stderr=pool_log)
try:
    import urllib.request
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"{POOL}/ping", timeout=2):
                break
        except OSError:
            time.sleep(0.5)

    # ---- 3. rescue 子进程（PG 档 env 自动分派）------------------------------------------
    r_env = {**os.environ,
             "V2MD_PG_DSN": DSN, "V2MD_RUNS_STORE": "postgres", "V2MD_TASKS_STORE": "postgres",
             "V2MD_STATE_FILE": str(TMP / "state.json"), "V2MD_USERS_FILE": str(TMP / "users.json"),
             "V2MD_LOG_DIR": str(TMP / "logs"), "V2MD_SECRETS_FILE": str(TMP / "nonexistent.env"),
             "OBSIDIAN_VAULT": str(VAULT), "V2MD_AUTH": "off", "V2MD_FAKE_LLM": "1",
             "V2MD_REMOTE": POOL, "V2MD_OCR_REMOTE": POOL, "V2MD_CLIPS_REMOTE": POOL,
             "V2MD_CLIPS_SOURCE": "remote", "PYTHONUNBUFFERED": "1",
             "V2MD_PORT": str(POOL_PORT + 1)}  # healthz 防护探测位（本测试无 api 在跑）
    (VAULT / "资料库" / "视频笔记").mkdir(parents=True, exist_ok=True)
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "rescue_partial.py"),
                        "resc1", "--max-entries", "1", "--no-tmp-guard"],
                       env=r_env, capture_output=True, text=True,
                       cwd=str(REPO), timeout=900)
    out = r.stdout + r.stderr
    assert r.returncode == 0, f"rescue 退出码 {r.returncode}\n{out[-2000:]}"

    # ---- 4. 断言：复用视频 + 新 run done + 笔记落地 -------------------------------------
    assert "复用已下载视频" in out, f"未走视频复用路径\n{out[-800:]}"
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT run_id, status FROM v2md_runs WHERE run_id != 'resc1'").fetchall()
    assert rows and all(st == "done" for _rid, st in rows), f"补跑 run 未 done：{rows}"
    notes = list((VAULT / "资料库" / "视频笔记").glob("测试笔记*.md"))
    assert notes, "vault 无笔记（rescue 未产出）"
    assert (old_tmp / "p001-src.mp4").is_file(), "旧 tmp 视频应原位保留（copy 非搬移）"
    print(f"✅ rescue PG 档 E2E（复用视频/补跑 {[rid for rid, _ in rows]} done/笔记落地）")
finally:
    mp.terminate()
    pool_log.close()
    pool.close()
    subprocess.run(["rm", "-rf", str(TMP)], check=False)

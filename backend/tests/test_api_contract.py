"""HTTP 契约测试（Phase 1）：鉴权（provider 无关）/ owner 越权隔离 / Pydantic 校验 /
错误信封 / OpenAPI 重开 / SSE 实时进度 / owner 线程到 resolve 去重。

全部离网：持久化文件指向临时目录；V2MD_AUTH=off（e2e/测试无 token 即可跑，用
X-Test-Auth 头注入不同身份断言多用户隔离）；POST 成功路径 monkeypatch resolve_run、
种子 run 永不 status=running（否则 lifespan 恢复会真驱动图联网）。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="v2md-contract-")
# —— 必须在 import video2md.config 之前设好（config 在 import 时读 env 与算路径）——
os.environ.update({
    "V2MD_AUTH": "off",
    "V2MD_USERS_FILE": str(Path(_TMP) / "users.json"),
    "V2MD_RUNS_FILE": str(Path(_TMP) / "runs.json"),
    "V2MD_PREFS_FILE": str(Path(_TMP) / "prefs.json"),
    "V2MD_STATE_FILE": str(Path(_TMP) / "state.json"),
    "V2MD_LOG_DIR": str(Path(_TMP) / "logs"),
    "V2MD_UPLOADS_DIR": str(Path(_TMP) / "uploads"),
    "V2MD_SECRETS_FILE": str(Path(_TMP) / "nonexistent.env"),
    "OBSIDIAN_VAULT": str(Path(_TMP) / "vault"),
    "V2MD_REMOTE": "off",  # 空串会被 load_env 丢弃→仓库 .env 真值渗入（隔离缺口，
                           # 实证 9700 下线时 preflight gpu_pool 打红）；"off" 非 http 前缀 → 不做算力池体检
    "V2MD_CORS_ORIGINS": "",
    "V2MD_USER_SUBMIT_PER_MIN": "1000",  # 套件整体抬高滑窗，配额行为由专测 monkeypatch 收紧
    "V2MD_FAKE_LLM": "1",  # 兜底：任何意外真驱动也不烧 token
})

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from video2md import events, runs  # noqa: E402
from video2md.api import app  # noqa: E402
from video2md.auth import put_identity, resolve_principal  # noqa: E402  # acctkit 接线层
from video2md.graph import resolve_run  # noqa: E402
from video2md.runs import create_run  # noqa: E402

# ---------------------------------------------------------------- fixtures / helpers

def _seed_run(run_id: str, owner: str, *, status: str = "done") -> dict:
    rec = {
        "run_id": run_id, "url": f"https://example.test/{run_id}", "part_items": "",
        "owner": owner, "created": "2026-01-01T00:00:00", "tmp": str(Path(_TMP) / "no-tmp"),
        "total": 1,
        "entries": [{"playlist_index": 1, "url": f"https://example.test/{run_id}"}],
        "artifacts": {f"https://example.test/{run_id}": {
            "note": "/vault/n.md", "md": "# hi", "title": "n", "video": "/tmp/v.mp4",
            "segs": [1], "candidates": "c", "ocr_raw": "o", "published": "p",
            "clips_done": True}},
        "stages": {}, "status": status, "tmp_cleaned": True,
    }
    existing = runs._load_runs()
    existing[run_id] = rec
    runs._save_runs(existing)
    return rec


@pytest.fixture(autouse=True)
def _clean_state():
    """每测前清空持久化文件——防跨用例泄漏（如上一用例留下的 running run 被 lifespan 恢复真跑）。"""
    for name in ("runs.json", "users.json", "state.json", "prefs.json"):
        p = Path(_TMP) / name
        if p.exists():
            p.unlink()
    import video2md.api as _api
    _api._GOOGLE_OAUTH_STATES.clear()  # OAuth state 单次消费测试的跨用例泄漏闸
    _api._GOOGLE_OAUTH_REDIRECTS.clear()
    _api._DEVICE_WIN.clear()           # IP 滑窗跨用例泄漏闸（设备流/授权码流限流共用一桶）
    yield


@pytest.fixture()
def client(_clean_state):
    # context manager 跑 lifespan → events.bind_loop（SSE 投递需要主循环）
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------- OpenAPI 重开

def test_openapi_and_docs_enabled(client):
    assert client.get("/openapi.json").status_code == 200
    spec = client.get("/openapi.json").json()
    for path in ("/runs", "/runs/{run_id}", "/runs/{run_id}/note", "/runs/{run_id}/events",
                 "/runs/{run_id}/cancel"):
        assert path in spec["paths"], path
    assert client.get("/docs").status_code == 200


# ---------------------------------------------------------------- 运维探针不鉴权

def test_operational_probes_no_auth(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/queue").json().get("queue_len") == 0
    assert client.get("/preflight").status_code == 200


# ---------------------------------------------------------------- 同源 SPA 路由不撞 API

_WEB_ROUTER = Path(__file__).resolve().parents[2] / "frontend/web/src/router/index.ts"


def _spa_paths() -> list[str]:
    import re
    paths = re.findall(r"path:\s*'([^']+)'", _WEB_ROUTER.read_text(encoding="utf-8"))
    # 参数段代入样例值；catch-all（`/:pathMatch(.*)*`）不是可直达的具体路由，跳过。
    return [re.sub(r":\w+", "x", p) for p in paths if p != "/" and "(" not in p]


def test_spa_routes_serve_index_not_api(client, monkeypatch, tmp_path):
    # 生产同源托管 Vite bundle：刷新/直达任一前端路由都必须拿到 index.html；
    # 前端路由一旦与 API 路由或 _API_PATH_PREFIXES 重名，浏览器会看到 JSON（L-01）。
    from video2md import api as _api
    (tmp_path / "index.html").write_text("<!doctype html><div id=app></div>")
    monkeypatch.setattr(_api, "_WEB_DIST", tmp_path.resolve())
    paths = _spa_paths()
    assert "/tasks" in paths and "/login" not in paths, paths
    for path in paths:
        r = client.get(path, headers={"Accept": "text/html"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html"), path
    assert client.get("/queue").json().get("queue_len") == 0  # gpu_router 回读口不受影响


# ---------------------------------------------------------------- 配置防呆（P0-1）

def _import_config_with_env(**overrides):
    """子进程隔离 import config——config 在 import 期读 env 且 fail-fast，本套件进程内
    已 import 过一次，无法同进程重放不同 env 组合（继承 os.environ 再覆盖目标键：
    真实环境变量在 load_env 合并序里最优先，结果确定）。"""
    import subprocess
    import sys
    env = {**os.environ, **overrides}
    # The subprocess runs from backend/, but package=false means video2md is
    # not installed into the venv. Preserve the same import path pytest uses.
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run([sys.executable, "-c", "import video2md.config"],
                          capture_output=True, text=True, env=env,
                          cwd=str(Path(__file__).resolve().parent.parent), timeout=60)


def test_production_anonymous_auth_is_rejected():
    assert _import_config_with_env(V2MD_ENV="production", V2MD_AUTH="off").returncode != 0
    assert _import_config_with_env(V2MD_ENV="production", V2MD_AUTH="").returncode != 0
    urls = {"V2MD_PUBLIC_BASE_URL": "https://v.test", "V2MD_WEB_BASE_URL": "https://v.test",
            "V2MD_LLM_SETTINGS_KEY": "hex:" + "11" * 32}
    assert _import_config_with_env(V2MD_ENV="production", V2MD_AUTH="google",
                                   V2MD_ARTIFACT_MODE="deliver",
                                   V2MD_OTEL_INCLUDE_CONTENT="off", **urls).returncode == 0
    assert _import_config_with_env(V2MD_ENV="production", V2MD_AUTH="google",
                                   V2MD_ARTIFACT_MODE="vault", **urls).returncode != 0
    assert _import_config_with_env(V2MD_ENV="production", V2MD_AUTH="google",
                                   V2MD_ARTIFACT_MODE="deliver",
                                   V2MD_OTEL_INCLUDE_CONTENT="on", **urls).returncode != 0


def test_production_google_requires_public_urls():
    # 缺省回落 localhost 会让生产 Google 登录跳回本机——启动期拒绝
    ok = {"V2MD_PUBLIC_BASE_URL": "https://v.test", "V2MD_WEB_BASE_URL": "https://v.test",
          "V2MD_LLM_SETTINGS_KEY": "hex:" + "11" * 32}
    for key in ("V2MD_PUBLIC_BASE_URL", "V2MD_WEB_BASE_URL"):
        for bad in ("", "http://localhost:5173"):
            env = {**ok, key: bad}
            r = _import_config_with_env(V2MD_ENV="production", V2MD_AUTH="google",
                                         V2MD_ARTIFACT_MODE="deliver",
                                         V2MD_OTEL_INCLUDE_CONTENT="off", **env)
            assert r.returncode != 0 and key in r.stderr, (key, bad)


def test_production_pg_requires_separate_app_and_migrate_roles():
    urls = {"V2MD_PUBLIC_BASE_URL": "https://v.test", "V2MD_WEB_BASE_URL": "https://v.test",
            "V2MD_LLM_SETTINGS_KEY": "hex:" + "11" * 32}
    pg = {"V2MD_RUNS_STORE": "postgres", "V2MD_STATE_STORE": "postgres",
          "V2MD_USERS_STORE": "postgres", "V2MD_TASKS_STORE": "postgres",
          "V2MD_PREFS_STORE": "postgres", "V2MD_EVENTS": "pg",
          "V2MD_CHECKPOINTER": "postgres"}
    common = {**urls, **pg, "V2MD_ENV": "production", "V2MD_AUTH": "google",
              "V2MD_ARTIFACT_MODE": "deliver", "V2MD_OTEL_INCLUDE_CONTENT": "off"}
    bad = _import_config_with_env(
        **common,
        V2MD_PG_DSN="postgresql://postgres:secret@db/v2pdf",
        V2MD_PG_MIGRATE_DSN="postgresql://video2md_migrate:secret@db/v2pdf")
    assert bad.returncode != 0 and "video2md_app" in bad.stderr
    good = _import_config_with_env(
        **common,
        V2MD_PG_DSN="postgresql://video2md_app:secret@db/v2pdf",
        V2MD_PG_MIGRATE_DSN="postgresql://video2md_migrate:secret@db/v2pdf")
    assert good.returncode == 0, good.stderr


# ---------------------------------------------------------------- /preflight 信息收敛（P0-3）

def test_config_spec_aggregate_errors_and_empty_clear():
    """P1-9：①多非法值一次性汇总（非首错即停）；②真实环境变量空串=显式清空回落
    默认，.env 真值不得渗入（旧 load_env 丢空串的隔离缺口根修）。"""
    import subprocess
    import sys
    # ① 三个非法键全部出现在同一次报错里
    r = _import_config_with_env(V2MD_MAX_CONCURRENT_RUNS="abc", V2MD_WATCHDOG_TIMEOUT_S="-5",
                                V2MD_TASKS_STORE="bogus")
    assert r.returncode != 0
    for key in ("V2MD_MAX_CONCURRENT_RUNS", "V2MD_WATCHDOG_TIMEOUT_S", "V2MD_TASKS_STORE"):
        assert key in r.stderr, f"汇总报错缺 {key}：{r.stderr[:400]}"
    assert "共 3 处" in r.stderr, r.stderr[:200]      # 一次报全（验收口径）
    # ② 空串=显式清空回落默认（旧 load_env 丢空串 → 真实环境压不掉任何来源值——
    # 该隔离缺口的根修验证；本仓 .env 未设 SPEC 键，断言默认即断言语义）
    code = ("import sys; sys.path.insert(0, '.');"
            "import video2md.config as c; print(c.TASKS_STORE, c.UPLOADS_TTL_H)")
    r2 = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                env={**os.environ, "V2MD_TASKS_STORE": "",
                     "V2MD_UPLOADS_TTL_H": "",
                     "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}, timeout=30)
    assert r2.stdout.split()[:2] == ["json", "24.0"], \
        f"空串未清空：{r2.stdout}{r2.stderr[:200]}"
    # ③ 显式合法值覆盖默认（合并优先级真实生效）
    r3 = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                        env={**os.environ, "V2MD_TASKS_STORE": "postgres",
                             "V2MD_UPLOADS_TTL_H": "48",
                             "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}, timeout=30)
    assert r3.stdout.split()[:2] == ["postgres", "48.0"], r3.stdout


def test_preflight_privilege_rules():
    from video2md.api import _preflight_privileged, _preflight_public
    assert _preflight_privileged("127.0.0.1", False) is True      # 回环（deploy.sh/容器体检）
    assert _preflight_privileged("::1", False) is True
    assert _preflight_privileged("localhost", False) is True
    assert _preflight_privileged("203.0.113.9", False) is False   # 公网匿名
    assert _preflight_privileged("203.0.113.9", True) is True     # 真实认证
    pub = _preflight_public({"ok": True, "ts": "t", "checks": [
        {"name": "ffmpeg", "ok": True, "detail": "/usr/local/bin/ffmpeg"},
        {"name": "gpu_pool", "ok": False, "detail": "算力池可用 provider: colab, kaggle"}]})
    assert pub == {"ok": True, "ts": "t",
                   "checks": [{"name": "ffmpeg", "ok": True}, {"name": "gpu_pool", "ok": False}]}


def test_preflight_anonymous_redacted(client, monkeypatch):
    # TestClient host="testclient" 非回环；本套件 AUTH=off 非真实认证 → 全部 detail 剥除
    # Keep this contract deterministic on developer machines with less than the
    # production 2 GB free-space threshold.
    import shutil

    import video2md.api as _api
    usage = shutil.disk_usage(tempfile.gettempdir())
    monkeypatch.setattr(_api.shutil, "disk_usage", lambda _path: usage._replace(free=max(usage.free, 3_000_000_000)))
    r = client.get("/preflight")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    for c in r.json()["checks"]:
        assert c.get("detail", "") == "", c
    assert "ffmpeg" in r.text and "provider" not in r.text.lower()


# ---------------------------------------------------------------- 鉴权：provider 无关 + 归属隔离

def test_owner_scoped_access(client):
    put_identity("test", "alice", "uid_alice")
    put_identity("test", "bob", "uid_bob")
    _seed_run("r_alice", "uid_alice")

    # 归属者 → 200
    ok = client.get("/runs/r_alice", headers={"X-Test-Auth": "alice"})
    assert ok.status_code == 200
    assert ok.json()["run_id"] == "r_alice"
    # 他人 → 403（信封统一：error.code）
    denied = client.get("/runs/r_alice", headers={"X-Test-Auth": "bob"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == 403
    # note 同样隔离
    assert client.get("/runs/r_alice/note", headers={"X-Test-Auth": "alice"}).status_code == 200
    assert client.get("/runs/r_alice/note", headers={"X-Test-Auth": "bob"}).status_code == 403


# ---------------------------------------------------------------- 会员/配额展示档（PG off 默认视图）

def test_me_defaults(client):
    # 本套件无 V2MD_PG_DSN（json 档）→ /me 回 free 默认视图：uid 回显、id_label 派生
    put_identity("test", "carol", "u_carol123")
    r = client.get("/me", headers={"X-Test-Auth": "carol"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["uid"] == "u_carol123"
    assert body["plan"] == "free"
    assert body["avatar_hue"] == 258
    assert body["id_label"] == "ID: carol123"  # uid 去 u_ 前缀派生（对齐 mock 语义）
    assert body["display_name"]  # name/display_name 至少回退到 uid，不为空


def test_quota_defaults(client):
    # PG off → /quota 全 0 但形状完整（前端 Quota 契约的 key 全在）
    put_identity("test", "carol", "u_carol123")
    r = client.get("/quota", headers={"X-Test-Auth": "carol"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ai_hours_used"] == 0 and body["ai_hours_total"] == 0
    assert body["storage_gb_used"] == 0 and body["storage_gb_total"] == 0
    assert body["stats"] == {"books": 0, "knowledge_points": 0, "accuracy": 0}


def test_quota_books_counts_finished_runs(client):
    # L-09：「已生成教材」= 本人未删的 done/partial run，不读无写入方的 membership 列
    put_identity("test", "carol", "u_carol123")
    _seed_run("r_c1", "u_carol123")
    _seed_run("r_c2", "u_carol123", status="partial")
    _seed_run("r_c3", "u_carol123", status="error")
    _seed_run("r_other", "u_someone_else")
    r = client.get("/quota", headers={"X-Test-Auth": "carol"})
    assert r.json()["stats"]["books"] == 2


def test_no_token_dev_identity_is_stable(client):
    # AUTH=off 无头 → dev 身份；同一条无头 run 自起自取通（e2e/batch 语义）
    ident = _seed_run("r_dev", _dev_uid())
    r = client.get("/runs/r_dev")  # 不带任何 header
    assert r.status_code == 200, r.text
    assert r.json()["status"] == ident["status"]


def test_missing_run_404(client):
    r = client.get("/runs/nope", headers={"X-Test-Auth": "alice"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == 404


# ---------------------------------------------------------------- Pydantic 校验 + 错误信封

def test_post_runs_validation_422(client):
    r = client.post("/runs", json={})  # 缺 url
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == 422
    assert body["error"]["details"]  # 指明缺哪个字段


def test_post_runs_bad_max_frames_422(client):
    r = client.post("/runs", json={"url": "x", "max_frames": 0})
    assert r.status_code == 422


# ---------------------------------------------------------------- owner 线程 + resolve 去重

def test_create_run_writes_owner():
    run = create_run("/tmp/local-x.mp4", "", False, owner="alice")
    assert run["owner"] == "alice"


def test_resolve_run_owner_scoped_dedup():
    put_identity("test", "carol", "uid_carol")
    put_identity("test", "dave", "uid_dave")
    url = "local-dedup-x"
    _seed_run("r_carol", "uid_carol", status="created")  # created/running 都能命中 dedup；created 不会被 lifespan 恢复真跑
    runs_runs = runs._load_runs()
    runs_runs["r_carol"]["url"] = url
    runs._save_runs(runs_runs)

    # 同 owner → 复用（is_dup True，返回旧 run，不新建）
    run, err, is_dup = resolve_run(url, "", False, "should_be_ignored", "uid_carol")
    assert err == "" and is_dup and run["run_id"] == "r_carol"
    # 他人同 URL → 各建各的（多用户不串），owner 落到新 run
    run2, err2, dup2 = resolve_run(url, "", False, "", "uid_dave")
    assert err2 == "" and not dup2 and run2["owner"] == "uid_dave"


def test_post_resolve_only_threads_owner(client, monkeypatch):
    # 不真解析：monkeypatch resolve_run，捕获 owner 是否为已认证 uid
    captured: dict = {}

    def fake_resolve(url, part_items, force, run_id, owner):
        captured["owner"] = owner
        rid = run_id or "abc"
        return ({"run_id": rid, "total": 1,
                 "entries": [{"playlist_index": 1, "url": "e"}],
                 "artifacts": {"e": {"skipped": True}}, "status": "created"}, "", False)

    import video2md.graph as graph_mod
    monkeypatch.setattr(graph_mod, "resolve_run", fake_resolve)
    put_identity("test", "erin", "uid_erin")
    r = client.post("/runs", json={"url": "whatever", "resolve_only": True},
                    headers={"X-Test-Auth": "erin"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "created" and r.json()["all_skipped"]
    assert captured["owner"] == "uid_erin"


# ---------------------------------------------------------------- SSE 实时进度

def test_sse_requires_owner(client):
    put_identity("test", "alice", "uid_alice")
    _seed_run("r_sse", "uid_alice")
    import json as _json
    with client.stream("GET", "/runs/r_sse/events", headers={"X-Test-Auth": "bob"}) as r:
        assert r.status_code == 403
        assert _json.loads(r.read())["error"]["code"] == 403


def test_sse_stream_connected_then_terminal(client):
    put_identity("test", "alice", "uid_alice")
    _seed_run("r_sse2", "uid_alice", status="done")
    rid = "r_sse2"
    # 连上后从主线程（≠ 应用循环线程）publish 一个收尾事件 → call_soon_threadsafe 投递
    threading.Timer(0.4, lambda: events.publish(
        rid, {"ts": "x", "run_id": rid, "event": "graph:end", "status": "done"})).start()
    seen: list[str] = []
    with client.stream("GET", f"/runs/{rid}/events",
                       headers={"X-Test-Auth": "alice"}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("data:"):
                seen.append(line)
            if "graph:end" in line:
                break
    assert any('"event": "connected"' in s or '"connected"' in s for s in seen), seen
    assert any("graph:end" in s for s in seen), seen


# ---------------------------------------------------------------- 内部辅助

def _dev_uid() -> str:
    """AUTH=off 无头 → (dev,dev) 的稳定内部 uid（首调自动开户）。"""
    from acctkit import Principal
    return resolve_principal(Principal(provider="dev", external_id="dev", name="dev")).uid


# ---------------------------------------------------------------- deliver 模式（在架 + TTL/ACK 即焚）

import io
import json
import shutil
import uuid
import zipfile
from datetime import datetime, timedelta

import video2md.config as v2config

_SEED_URL = "https://example.test/deliver-seed"


@pytest.fixture()
def deliver(_clean_state, monkeypatch):
    """切 deliver 模式：artsink/api 全部经 config 模块属性动态读（setattr 即生效），
    在架根指向本用例独立 tmp。"""
    ob = Path(_TMP) / f"shelf-{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(v2config, "ARTIFACT_MODE", "deliver")
    monkeypatch.setattr(v2config, "OUTBOX_DIR", ob)
    monkeypatch.setattr(v2config, "OUTBOX_TTL_H", 24.0)
    return ob


def _seed_delivered_run(run_id: str, owner: str, ob: Path, *,
                        expires_in_h: float = 24.0, burned: bool = False) -> dict:
    """模拟 deliver 终态 run：outbox 已搬家到在架位，runs.json 里 md 全文已剥
    （_relocate_outbox 之后的形状，单元级搬家逻辑本身在 test_graph 里测）。"""
    rec = _seed_run(run_id, owner)
    url = rec["entries"][0]["url"]
    root = ob / run_id
    (root / "transcripts").mkdir(parents=True, exist_ok=True)
    (root / "attachments").mkdir(parents=True, exist_ok=True)
    (root / "笔记.md").write_text(f"# {run_id} 正文", encoding="utf-8")
    (root / "transcripts" / "t-transcript.txt").write_text("转录", encoding="utf-8")
    (root / "attachments" / "a-f001.jpg").write_bytes(b"\xff\xd8fakejpg")
    (root / "索引-append.md").write_text("| [[笔记]] | 0:10 | — | 2026-09-23 |\n", encoding="utf-8")
    rec.update(outbox=str(root), tmp_cleaned=True,
               artifacts_expire=(datetime.now() + timedelta(hours=expires_in_h))
               .isoformat(timespec="seconds"))
    if burned:
        rec["artifacts_burned"] = True
    rec["artifacts"][url].update(note=str(root / "笔记.md"), title="n")
    rec["artifacts"][url].pop("md", None)  # finalize 的诚实口径：正文不留在 runs.json
    db = runs._load_runs()
    db[run_id] = rec
    runs._save_runs(db)
    return rec


def test_bundle_zip_roundtrip_and_ack(client, deliver):
    put_identity("test", "alice", "uid_alice")
    _seed_delivered_run("r_dl", "uid_alice", deliver)
    r = client.get("/runs/r_dl/bundle", headers={"X-Test-Auth": "alice"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
    assert {"笔记.md", "transcripts/t-transcript.txt", "attachments/a-f001.jpg",
            "索引-append.md"} <= names, names
    # 轮询面带在架状态与到期窗口
    st = client.get("/runs/r_dl", headers={"X-Test-Auth": "alice"}).json()
    assert st["artifacts"]["mode"] == "deliver" and st["artifacts"]["present"] is True
    assert st["artifacts"]["expires"] > datetime.now().isoformat()[:16]
    # note 从在架文件取（runs.json 已无 md）
    n = client.get("/runs/r_dl/note", headers={"X-Test-Auth": "alice"})
    assert n.status_code == 200 and n.json()["notes"][0]["md"] == "# r_dl 正文"
    # ACK 即焚：目录物理消失，再取全 410
    ack = client.delete("/runs/r_dl/artifacts", headers={"X-Test-Auth": "alice"})
    assert ack.status_code == 200 and ack.json() == {"run_id": "r_dl", "status": "burned"}
    assert not (deliver / "r_dl").exists(), "ACK 必须是物理删除（云端不储存口径）"
    assert client.get("/runs/r_dl/bundle", headers={"X-Test-Auth": "alice"}).status_code == 410
    assert client.get("/runs/r_dl/note", headers={"X-Test-Auth": "alice"}).status_code == 410
    assert client.get("/runs/r_dl", headers={"X-Test-Auth": "alice"}
                      ).json()["artifacts"]["present"] is False
    assert runs._load_runs()["r_dl"]["burned_reason"] == "ack"
    # 再 ACK 一次（幂等口径：已焚 → 410）
    assert client.delete("/runs/r_dl/artifacts", headers={"X-Test-Auth": "alice"}).status_code == 410


def test_bundle_owner_isolation_and_mode_gates(client, deliver):
    put_identity("test", "alice", "uid_alice")
    put_identity("test", "bob", "uid_bob")
    _seed_delivered_run("r_iso", "uid_alice", deliver)
    assert client.get("/runs/r_iso/bundle",
                      headers={"X-Test-Auth": "bob"}).status_code == 403
    assert client.delete("/runs/r_iso/artifacts",
                         headers={"X-Test-Auth": "bob"}).status_code == 403
    assert client.get("/runs/r_iso/media/a-f001.jpg",
                      headers={"X-Test-Auth": "bob"}).status_code == 403
    # 有 run 无产物（进行中）→ 404 不是 410
    _seed_run("r_noprod", "uid_alice", status="created")
    assert client.get("/runs/r_noprod/bundle",
                      headers={"X-Test-Auth": "alice"}).status_code == 404
    # vault 模式下产物端点整组 409（闸在模式，先于归属）
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(v2config, "ARTIFACT_MODE", "vault")
        assert client.get("/runs/r_iso/bundle",
                          headers={"X-Test-Auth": "alice"}).status_code == 409


def test_ttl_expiry_lazy_burns_on_request(client, deliver):
    put_identity("test", "alice", "uid_alice")
    _seed_delivered_run("r_exp", "uid_alice", deliver, expires_in_h=-0.5)
    root = deliver / "r_exp"
    assert root.exists()  # 周期 sweep 是 10min 一次——到期瞬间得靠请求时惰性焚兜底
    assert client.get("/runs/r_exp/bundle", headers={"X-Test-Auth": "alice"}).status_code == 410
    assert not root.exists(), "过期即焚必须是物理删除（云端不储存口径）"
    assert runs._load_runs()["r_exp"]["burned_reason"] == "ttl"


def test_sweep_outbox_sweeps_only_expired(client, deliver):
    from video2md.api import _sweep_outbox
    put_identity("test", "alice", "uid_alice")
    _seed_delivered_run("r_old", "uid_alice", deliver, expires_in_h=-1)
    _seed_delivered_run("r_new", "uid_alice", deliver, expires_in_h=6)
    assert _sweep_outbox() == 1
    assert not (deliver / "r_old").exists() and (deliver / "r_new").exists()
    assert _sweep_outbox() == 0  # 幂等：已烧不再计


def test_create_run_skip_requires_owner_and_shelf(deliver, monkeypatch):
    """deliver 幂等跳过的硬条件：state 条目 owner 相同 + 源 run 产物还在架上。"""
    url = "https://example.test/dedup-me"
    # state：alice 处理过（node_resolve 落盘带 owner/run_id），源 run r_src 在架
    st = {"fp1": {"url": url, "owner": "uid_alice", "run_id": "r_src", "title": "旧笔记",
                  "status": "completed"}}
    (Path(_TMP) / "state.json").write_text(json.dumps(st), encoding="utf-8")
    _seed_delivered_run("r_src", "uid_alice", deliver)
    import video2md.runs as _r
    monkeypatch.setattr(_r, "resolve_url", lambda u: [{"url": u, "id": ""}])

    a = _r.create_run(url, "", False, owner="uid_alice")
    art_a = a["artifacts"][a["entries"][0]["url"]]
    assert art_a.get("skipped") and art_a["skipped_from"]["run_id"] == "r_src"

    b = _r.create_run(url, "", False, owner="uid_bob")  # 他人：产物不在他手里 → 重烧
    assert not b["artifacts"][b["entries"][0]["url"]].get("skipped")

    # alice 再发，但源在架没了（ACK/清理烧掉）→ 同样重烧（skip 了也给不到东西）
    shutil.rmtree(deliver / "r_src")
    c = _r.create_run(url, "", False, owner="uid_alice")
    assert not c["artifacts"][c["entries"][0]["url"]].get("skipped")
    # 烧后 runs 库标记 burned 的口径（_outbox_live 的 missing 分支）同样不 skip
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_r, "resolve_url", lambda u: [{"url": u, "id": ""}])
        _seed_delivered_run("r_src2", "uid_alice", deliver, burned=True)
        st2 = {"fp1": {"url": url, "owner": "uid_alice", "run_id": "r_src2",
                       "status": "completed"}}
        (Path(_TMP) / "state.json").write_text(json.dumps(st2), encoding="utf-8")
        d = _r.create_run(url, "", False, owner="uid_alice")
        assert not d["artifacts"][d["entries"][0]["url"]].get("skipped")


def test_vault_state_dedup_is_owner_scoped(_clean_state, monkeypatch):
    """A completed URL from Alice must not suppress Bob, even in legacy vault mode."""
    import video2md.runs as _r

    url = "https://example.test/shared-source"
    monkeypatch.setattr(v2config, "ARTIFACT_MODE", "vault")
    monkeypatch.setattr(_r, "resolve_url", lambda u: [{"url": u, "id": ""}])
    (Path(_TMP) / "state.json").write_text(json.dumps({
        "alice::fp": {"url": url, "owner": "uid_alice", "run_id": "a1",
                       "status": "completed"},
    }), encoding="utf-8")

    bob = _r.create_run(url, "", False, owner="uid_bob")
    assert not bob["artifacts"][url].get("skipped")


def test_state_keys_are_tenant_namespaced():
    from video2md.util import state_key

    assert state_key("fp", "uid_alice") != state_key("fp", "uid_bob")
    assert state_key("fp", "uid_alice") == "uid_alice::fp"
    assert state_key("fp", "") == "fp"  # legacy CLI/development compatibility


def test_deliver_outboxes_do_not_reuse_same_named_attachment(_clean_state, tmp_path, monkeypatch):
    """Same basenames remain isolated because deliver uses one outbox per run."""
    from video2md import artsink, frames

    monkeypatch.setattr(v2config, "ARTIFACT_MODE", "deliver")
    a_src = tmp_path / "alice.jpg"
    b_src = tmp_path / "bob.jpg"
    a_src.write_bytes(b"alice")
    b_src.write_bytes(b"bob")
    run_a = {"run_id": "a1", "tmp": str(tmp_path / "run-a")}
    run_b = {"run_id": "b1", "tmp": str(tmp_path / "run-b")}

    with artsink.sink_for(run_a):
        frames.publish_named([(0.0, a_src, "same-f01-0000.jpg")], [])
    with artsink.sink_for(run_b):
        frames.publish_named([(0.0, b_src, "same-f01-0000.jpg")], [])

    assert (Path(run_a["tmp"]) / "outbox/attachments/same-f01-0000.jpg").read_bytes() == b"alice"
    assert (Path(run_b["tmp"]) / "outbox/attachments/same-f01-0000.jpg").read_bytes() == b"bob"


def test_preflight_swaps_vault_for_outbox(client, deliver):
    pf = client.get("/preflight").json()
    names = {c["name"] for c in pf["checks"]}
    assert "outbox" in names and "vault" not in names


# ---------------------------------------------------------------- GET /runs 列表（插件收件箱发现）

def test_run_list_owner_scoped_and_status_filter(client):
    put_identity("test", "alice", "uid_alice")
    put_identity("test", "bob", "uid_bob")
    _seed_run("r_a1", "uid_alice", status="done")
    _seed_run("r_a2", "uid_alice", status="error")
    _seed_run("r_b1", "uid_bob", status="done")

    items = client.get("/runs", headers={"X-Test-Auth": "alice"}).json()["items"]
    assert {i["run_id"] for i in items} == {"r_a1", "r_a2"}
    only_done = client.get("/runs?status=done", headers={"X-Test-Auth": "alice"}).json()["items"]
    assert [i["run_id"] for i in only_done] == ["r_a1"]
    # done_entries：note 有且非 skipped 的条目数（_seed_run 的形状=1）
    assert only_done[0]["done_entries"] == 1
    # vault 模式不带 artifacts 块（契约零扰动口径，同 GET /runs/{id}）
    assert only_done[0]["artifacts"] is None
    assert client.get("/runs", headers={"X-Test-Auth": "alice"}).json()["mode"] == "vault"


def test_run_list_deliver_marks_shelf_present(client, deliver):
    put_identity("test", "alice", "uid_alice")
    _seed_delivered_run("r_on", "uid_alice", deliver)
    _seed_delivered_run("r_off", "uid_alice", deliver, burned=True)
    items = {i["run_id"]: i for i in
             client.get("/runs?status=done", headers={"X-Test-Auth": "alice"}).json()["items"]}
    assert items["r_on"]["artifacts"] == {"mode": "deliver", "present": True,
                                          **{"expires": items["r_on"]["artifacts"]["expires"]}}
    assert items["r_off"]["artifacts"]["present"] is False


def test_run_list_title_derivation(client):
    """教材展示名三路派生：art.title（LLM note 标题）→ entry.title → url 主机名。"""
    put_identity("test", "alice", "uid_alice")
    _seed_run("r_art", "uid_alice")  # _seed_run 默认带 art.title="n"
    # entry.title 回落：抹掉 art.title，entries 里给 title
    _seed_run("r_entry", "uid_alice")
    # 主机名回落：art/entry 都没 title
    _seed_run("r_host", "uid_alice")
    db = runs._load_runs()
    db["r_entry"]["artifacts"] = {u: {k: v for k, v in a.items() if k != "title"}
                                  for u, a in db["r_entry"]["artifacts"].items()}
    db["r_entry"]["entries"] = [{"playlist_index": 1, "title": "Entry 标题",
                                 "url": "https://example.test/e"}]
    db["r_host"]["artifacts"] = {u: {k: v for k, v in a.items() if k != "title"}
                                 for u, a in db["r_host"]["artifacts"].items()}
    runs._save_runs(db)

    items = {i["run_id"]: i["title"] for i in
             client.get("/runs", headers={"X-Test-Auth": "alice"}).json()["items"]}
    assert items["r_art"] == "n"
    assert items["r_entry"] == "Entry 标题"
    assert items["r_host"] == "example.test"


# ---------------------------------------------------------------- /prefs（教材偏好：收藏/阅读进度）

def test_prefs_empty_upsert_partial_and_persist(client):
    put_identity("test", "alice", "uid_alice")
    _seed_run("r_p1", "uid_alice")
    assert client.get("/prefs", headers={"X-Test-Auth": "alice"}).json() == {"items": []}

    r = client.put("/prefs/r_p1", json={"fav": True, "progress": 42},
                   headers={"X-Test-Auth": "alice"})
    assert r.status_code == 200 and r.json() == {"run_id": "r_p1", "fav": True, "progress": 42}
    # 部分更新：只动 progress，fav 保持
    r = client.put("/prefs/r_p1", json={"progress": 68}, headers={"X-Test-Auth": "alice"})
    assert r.json() == {"run_id": "r_p1", "fav": True, "progress": 68}
    # 列表读回（文件档落盘——跨请求持久）
    items = client.get("/prefs", headers={"X-Test-Auth": "alice"}).json()["items"]
    assert items == [{"run_id": "r_p1", "fav": True, "progress": 68}]


def test_prefs_validation_and_access(client):
    put_identity("test", "alice", "uid_alice")
    put_identity("test", "bob", "uid_bob")
    _seed_run("r_mine", "uid_alice")
    _seed_run("r_other", "uid_bob")
    h = {"X-Test-Auth": "alice"}
    assert client.put("/prefs/r_mine", json={"progress": -1}, headers=h).status_code == 422
    assert client.put("/prefs/r_mine", json={"progress": 101}, headers=h).status_code == 422
    assert client.put("/prefs/r_nope", json={"fav": True}, headers=h).status_code == 404
    assert client.put("/prefs/r_other", json={"fav": True}, headers=h).status_code == 403


# ---------------------------------------------------------------- /auth/device/*（设备流桥）

def test_auth_device_code_requires_config(client, monkeypatch):
    import video2md.api as _api
    monkeypatch.setattr(_api, "ENV", {})  # 未配 client 凭据 → 500（服务端配置问题）
    assert client.post("/auth/device/code").status_code == 500


def test_auth_device_code_proxies_google(client, monkeypatch):

    import video2md.api as _api
    monkeypatch.setattr(_api, "ENV", {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "sec"})

    class _Resp:
        status_code = 200
        @staticmethod
        def json():
            return {"device_code": "DC", "user_code": "ABCD-1234",
                    "verification_url": "https://google.com/device",
                    "expires_in": 1800, "interval": 5}

    async def _fake_post(self, url, data=None, **kw):  # noqa: ANN001
        assert url == _api._GOOGLE_DEVICE_CODE_URL
        assert data["client_id"] == "cid"
        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    r = client.post("/auth/device/code")
    assert r.status_code == 200, r.text
    assert r.json() == {"device_code": "DC", "user_code": "ABCD-1234",
                        "verification_url": "https://google.com/device",
                        "expires_in": 1800, "interval": 5}


def test_auth_device_token_pending_then_ok(client, monkeypatch):
    import video2md.api as _api
    from acctkit import providers as _p
    monkeypatch.setattr(_api, "ENV", {"GOOGLE_CLIENT_ID": "cid", "GOOGLE_CLIENT_SECRET": "sec"})

    # 用户还没授权：Google 回 400 authorization_pending → 桥透传 401（客户端继续轮询）
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_p.GoogleDeviceProvider, "_exchange",
                   lambda self, dc: (_ for _ in ()).throw(
                       _p.AuthError("Google 设备流未就绪（authorization_pending）", status=401)))
        r = client.post("/auth/device/token", json={"device_code": "DC"})
        assert r.status_code == 401

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_p.GoogleDeviceProvider, "_exchange",
                   lambda self, dc: {"id_token": "JWT"})
        ok = client.post("/auth/device/token", json={"device_code": "DC"})
        assert ok.status_code == 200 and ok.json() == {"id_token": "JWT"}


# ------------------------------------------- /auth/google/*（网页端授权码重定向登录）

_GENV = {"GOOGLE_CLIENT_ID": "cid-web", "GOOGLE_CLIENT_SECRET": "sec-web",
         "V2MD_WEB_BASE_URL": "http://frontend.test",
         "V2MD_PUBLIC_BASE_URL": "http://api.public"}


def _seed_state(st: str = "state-ok", ttl: float = 300.0) -> None:
    import video2md.api as _api
    _api._GOOGLE_OAUTH_STATES[st] = time.monotonic() + ttl


def test_auth_google_login_redirects_to_google(client, monkeypatch):
    import urllib.parse as _up

    import video2md.api as _api
    monkeypatch.setattr(_api, "ENV", dict(_GENV))
    r = client.get("/auth/google/login", params={"terms_accepted": "1"}, follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    u = _up.urlparse(loc)
    assert u.netloc == "accounts.google.com"
    q = _up.parse_qs(u.query)
    assert q["client_id"] == ["cid-web"]
    assert q["redirect_uri"] == ["http://api.public/auth/google/callback"]
    assert q["response_type"] == ["code"]
    assert "openid" in q["scope"][0]
    st = q["state"][0]
    assert len(st) >= 32 and st in _api._GOOGLE_OAUTH_STATES  # 已登记待消费
    assert "v2md_oauth_state" in r.headers.get("set-cookie", "")  # CSRF 绑定 cookie


def test_auth_google_login_requires_terms_before_oauth(client, monkeypatch):
    import urllib.parse as _up

    import video2md.api as _api
    monkeypatch.setattr(_api, "ENV", dict(_GENV))
    blocked = client.get("/auth/google/login", follow_redirects=False)
    assert blocked.status_code == 302
    assert "auth_error=" in blocked.headers["location"]
    assert "请先阅读并同意" in _up.unquote(blocked.headers["location"])
    assert "accounts.google.com" not in blocked.headers["location"]
    assert not _api._GOOGLE_OAUTH_STATES

    allowed = client.get("/auth/google/login", params={"terms_accepted": "1"}, follow_redirects=False)
    assert allowed.status_code == 302
    assert "accounts.google.com" in allowed.headers["location"]
    assert _api._GOOGLE_OAUTH_STATES


def test_auth_google_login_missing_config(client, monkeypatch):
    import video2md.api as _api
    monkeypatch.setattr(_api, "ENV", {})
    r = client.get("/auth/google/login", params={"terms_accepted": "1"}, follow_redirects=False)
    assert r.status_code == 302
    assert "auth_error=" in r.headers["location"]  # 未配凭据 → fragment 错误出口
    r = client.get("/auth/google/login", params={"redirect": "/tasks", "terms_accepted": "1"}, follow_redirects=False)
    assert r.headers["location"].startswith("http://localhost:5173/?redirect=%2Ftasks#auth_error=")


def test_auth_google_login_rate_limit(client):
    import video2md.api as _api
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_api, "ENV", dict(_GENV))
        mp.setattr(_api, "DEVICE_RATE_PER_MIN", 2)
        _api._DEVICE_WIN.clear()
        for _ in range(2):
            ok = client.get("/auth/google/login", params={"terms_accepted": "1"}, follow_redirects=False)
            assert ok.status_code == 302 and "auth_error=" not in ok.headers["location"]
        over = client.get("/auth/google/login", params={"terms_accepted": "1"}, follow_redirects=False)
        assert over.status_code == 302  # 浏览器导航端点：429 折叠进 fragment 错误出口
        assert "auth_error=" in over.headers["location"]


def test_auth_google_callback_happy_path(client, monkeypatch):
    import video2md.api as _api
    from acctkit import providers as _p
    monkeypatch.setattr(_api, "ENV", dict(_GENV))
    _seed_state("st-ok")
    seen: dict = {}

    class _Resp:
        status_code = 200
        @staticmethod
        def json():
            return {"id_token": "JWT.WT.OK"}

    async def _fake_post(self, url, data=None, **kw):  # noqa: ANN001
        seen["url"] = url
        seen["data"] = data
        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    monkeypatch.setattr(_p.GoogleProvider, "_verify",
                        lambda self, t: {"sub": "s1", "email": "a@b.test"})
    r = client.get("/auth/google/callback?code=abc&state=st-ok",
                   cookies={"v2md_oauth_state": "st-ok"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "http://frontend.test/#id_token=JWT.WT.OK"
    assert seen["url"] == _api._GOOGLE_OAUTH_TOKEN_URL
    assert seen["data"]["code"] == "abc" and seen["data"]["redirect_uri"] == \
        "http://api.public/auth/google/callback"
    assert "st-ok" not in _api._GOOGLE_OAUTH_STATES  # state 单次消费
    assert 'v2md_oauth_state="";' in r.headers.get("set-cookie", "").replace(" ", "") \
        or "v2md_oauth_state=;" in r.headers.get("set-cookie", "")  # cookie 已焚


def test_google_oauth_proxy_rejects_retired_17898(monkeypatch):
    """OAuth must never silently fall back to the retired mihomo 17898 port."""
    import video2md.api as _api
    env = dict(_GENV)
    env["V2MD_GOOGLE_OAUTH_PROXY_URL"] = "http://127.0.0.1:17898"
    monkeypatch.setattr(_api, "ENV", env)
    with pytest.raises(RuntimeError, match="17898"):
        _api._google_oauth_proxy()
    env["V2MD_GOOGLE_OAUTH_PROXY_URL"] = ""
    assert _api._google_oauth_proxy() is None


def test_auth_google_redirect_round_trip(client, monkeypatch):
    """L-05：发起时带站内 redirect，随 state 记账，回调落到品牌首页；
    站外/协议相对路径不记账（防开放重定向）。"""
    import urllib.parse as _up

    import video2md.api as _api
    from acctkit import providers as _p
    monkeypatch.setattr(_api, "ENV", dict(_GENV))

    def _state_of(r):
        return _up.parse_qs(_up.urlparse(r.headers["location"]).query)["state"][0]

    for bad in ("//evil.test/x", "/\\evil.test", "https://evil.test/", "read/x"):
        st = _state_of(client.get("/auth/google/login", params={"redirect": bad, "terms_accepted": "1"},
                                  follow_redirects=False))
        assert st not in _api._GOOGLE_OAUTH_REDIRECTS, bad
    st = _state_of(client.get("/auth/google/login", params={"redirect": "/read/r1?p=2", "terms_accepted": "1"},
                              follow_redirects=False))
    assert _api._GOOGLE_OAUTH_REDIRECTS[st] == "/read/r1?p=2"

    class _Resp:
        status_code = 200
        @staticmethod
        def json():
            return {"id_token": "JWT.WT.OK"}

    async def _fake_post(self, url, data=None, **kw):  # noqa: ANN001
        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    monkeypatch.setattr(_p.GoogleProvider, "_verify", lambda self, t: {"sub": "s1"})
    r = client.get(f"/auth/google/callback?code=abc&state={st}",
                   cookies={"v2md_oauth_state": st}, follow_redirects=False)
    assert r.headers["location"] == \
        "http://frontend.test/?redirect=%2Fread%2Fr1%3Fp%3D2#id_token=JWT.WT.OK"
    assert st not in _api._GOOGLE_OAUTH_REDIRECTS


def test_auth_google_callback_state_rejected(client, monkeypatch):
    """未知 state / 缺 cookie / cookie 不符 → fragment 错误出口；合法 state 被消耗。"""
    import video2md.api as _api
    monkeypatch.setattr(_api, "ENV", dict(_GENV))

    def _assert_rejected(r):
        assert r.status_code == 302 and "auth_error=" in r.headers["location"]

    # (a) 未知 state
    _assert_rejected(client.get("/auth/google/callback?code=abc&state=nope",
                                cookies={"v2md_oauth_state": "nope"}, follow_redirects=False))
    # (b) 合法 state 但缺 cookie
    _seed_state("st-nc")
    _assert_rejected(client.get("/auth/google/callback?code=abc&state=st-nc",
                                follow_redirects=False))
    assert "st-nc" not in _api._GOOGLE_OAUTH_STATES  # 即便被拒也消耗（防重放试探）
    # (c) 合法 state + cookie 值不符
    _seed_state("st-bad")
    _assert_rejected(client.get("/auth/google/callback?code=abc&state=st-bad",
                                cookies={"v2md_oauth_state": "other"}, follow_redirects=False))
    # (d) 缺 code（用户在 Google 授权页取消）
    _seed_state("st-nocode")
    r = client.get("/auth/google/callback?state=st-nocode",
                   cookies={"v2md_oauth_state": "st-nocode"}, follow_redirects=False)
    assert r.status_code == 302 and "auth_error=" in r.headers["location"]


def test_auth_google_callback_exchange_rejected(client, monkeypatch):
    import video2md.api as _api
    monkeypatch.setattr(_api, "ENV", dict(_GENV))
    _seed_state("st-x")

    class _Resp:
        status_code = 400
        @staticmethod
        def json():
            return {"error": "invalid_grant"}

    async def _fake_post(self, url, data=None, **kw):  # noqa: ANN001
        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    r = client.get("/auth/google/callback?code=abc&state=st-x",
                   cookies={"v2md_oauth_state": "st-x"}, follow_redirects=False)
    assert r.status_code == 302 and "auth_error=" in r.headers["location"]


def test_auth_google_callback_verify_fails(client, monkeypatch):
    import video2md.api as _api
    from acctkit import providers as _p
    monkeypatch.setattr(_api, "ENV", dict(_GENV))
    _seed_state("st-v")

    class _Resp:
        status_code = 200
        @staticmethod
        def json():
            return {"id_token": "JWT.BAD"}

    async def _fake_post(self, url, data=None, **kw):  # noqa: ANN001
        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    monkeypatch.setattr(_p.GoogleProvider, "_verify",
                        lambda self, t: (_ for _ in ()).throw(ValueError("bad aud")))
    r = client.get("/auth/google/callback?code=abc&state=st-v",
                   cookies={"v2md_oauth_state": "st-v"}, follow_redirects=False)
    assert r.status_code == 302 and "auth_error=" in r.headers["location"]


def test_multi_aud_google_provider_registration():
    from acctkit.providers import make_provider
    from video2md.auth.ext_google import MultiAudGoogleProvider

    # 双 client 并存：audiences 去重保序 [web, tv]
    p = make_provider("google", {"GOOGLE_CLIENT_ID": "tv", "GOOGLE_WEB_CLIENT_ID": "web"})
    assert isinstance(p, MultiAudGoogleProvider) and p._audiences == ["web", "tv"]
    # 只配 base（设备流单 client 档）：与基座等价
    p2 = make_provider("google", {"GOOGLE_CLIENT_ID": "tv"})
    assert p2._audiences == ["tv"] and p2.client_id == "tv"
    # 只配 web（无设备流档）：client_id 回落 web（基座空 client_id 直接 500 的防呆）
    p3 = make_provider("google", {"GOOGLE_WEB_CLIENT_ID": "web"})
    assert p3._audiences == ["web"] and p3.client_id == "web"


# ---------------------------------------------------------------- 配额闸（终局布局·限流配额）

def test_queue_full_429(client):
    import video2md.api as _api
    _api._TASKS["occupied"] = {"status": "queued", "url": "u", "owner": "someone",
                               "created": "x"}
    try:
        monkey = pytest.MonkeyPatch.context()
        with monkey as mp:
            mp.setattr(_api, "QUEUE_MAX", 1)
            r = client.post("/runs", json={"url": "https://example.test/x",
                                           "resolve_only": True})
        assert r.status_code == 429 and r.json()["error"]["code"] == 429
    finally:
        _api._TASKS.pop("occupied", None)


def test_user_queue_full_429(client):
    import video2md.api as _api
    _api._TASKS["mine"] = {"status": "running", "url": "u", "owner": _dev_uid(),
                           "created": "x"}
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_api, "USER_QUEUE_MAX", 1)
            r = client.post("/runs", json={"url": "https://example.test/x",
                                           "resolve_only": True})
        assert r.status_code == 429
    finally:
        _api._TASKS.pop("mine", None)


def test_rate_limited_429(client, monkeypatch):
    import video2md.api as _api

    def fake_resolve(url, part_items, force, run_id, owner):
        return ({"run_id": run_id or "rk", "total": 1,
                 "entries": [{"playlist_index": 1, "url": "e"}],
                 "artifacts": {"e": {}}, "status": "created"}, "", False)

    import video2md.graph as graph_mod
    monkeypatch.setattr(graph_mod, "resolve_run", fake_resolve)
    _api._USER_SUBMIT_TS.clear()
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_api, "USER_SUBMIT_PER_MIN", 1)
            first = client.post("/runs", json={"url": "https://example.test/a",
                                               "resolve_only": True})
            second = client.post("/runs", json={"url": "https://example.test/b",
                                                "resolve_only": True})
        assert first.status_code == 200, first.text
        assert second.status_code == 429
    finally:
        _api._USER_SUBMIT_TS.clear()


# ---------------------------------------------------------------- 本地路径白名单 + 上传面

def _stub_resolve(monkeypatch):
    """POST /runs 成功路径共用：假 resolve（不落盘也行，_TASKS/runs.json 形状由并发专测覆盖）。"""
    import video2md.graph as graph_mod

    def fake_resolve(url, part_items, force, run_id, owner):
        return ({"run_id": run_id or "rk", "total": 1,
                 "entries": [{"playlist_index": 1, "url": url}],
                 "artifacts": {url: {}}, "status": "created"}, "", False)

    monkeypatch.setattr(graph_mod, "resolve_run", fake_resolve)


def test_local_path_policy(client, monkeypatch):
    import video2md.api as _api
    _stub_resolve(monkeypatch)
    # auto + AUTH=off（本套件 env）→ 裸路径放行（开发/自托管姿势）
    ok = client.post("/runs", json={"url": "/etc/passwd", "resolve_only": True})
    assert ok.status_code == 200, ok.text

    # deny → 422 LOCAL_PATH_FORBIDDEN（闸在 resolve 之前，无需真解析）
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_api, "LOCAL_PATHS", "deny")
        denied = client.post("/runs", json={"url": "/etc/passwd"})
        assert denied.status_code == 422
        assert denied.json()["error"]["code"] == 422

    # auto + AUTH=google → 只认本人 uploads 白名单内路径
    up = Path(_TMP) / "up-policy"
    dev = _dev_uid()
    mine = up / dev / "clip.mp4"
    mine.parent.mkdir(parents=True, exist_ok=True)
    mine.write_bytes(b"x")
    other = up / "uid_other" / "steal.mp4"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(b"x")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_api, "LOCAL_PATHS", "auto")
        mp.setattr(_api, "UPLOADS_DIR", up)
        mp.setitem(_api.ENV, "V2MD_AUTH", "google")
        assert client.post("/runs", json={"url": str(mine),
                                          "resolve_only": True}).status_code == 200
        for bad in ("/etc/passwd", str(other), str(up / "escape.mp4")):
            r = client.post("/runs", json={"url": bad})
            assert r.status_code == 422, (bad, r.text)


def test_upload_roundtrip_and_gates(client, monkeypatch):
    import video2md.api as _api
    up = Path(_TMP) / "uploads"
    monkeypatch.setattr(_api, "UPLOADS_DIR", up)
    dev = _dev_uid()

    # 最小合法 mp4 头（size + ftyp + brand…）：魔数校验只看首 16 字节，后续补零到 500B
    mp4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 476
    r = client.post("/uploads", files={"file": ("demo.mp4", mp4, "video/mp4")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "demo.mp4" and body["size"] == 500
    p = Path(body["path"])
    assert p.exists() and p.parent == up / dev  # 落在本人白名单目录（可直接喂 /runs）

    _stub_resolve(monkeypatch)
    fed = client.post("/runs", json={"url": body["path"], "resolve_only": True})
    assert fed.status_code == 200, fed.text  # uploads 内路径过白名单

    # 类型白名单：可执行文件拒收
    assert client.post("/uploads",
                       files={"file": ("evil.exe", b"MZ", "application/x-msdownload")}).status_code == 422
    # 文件名 sanitize：路径穿越片段被剥
    sneak = client.post("/uploads", files={"file": ("../..%2Fc.mp4", mp4, "video/mp4")})
    assert sneak.status_code == 200
    assert "/" not in Path(sneak.json()["name"]).name and sneak.json()["name"].endswith(".mp4")

    # 魔数校验（P0-6）：文本改名 .mp4 → 422 且文件已删（X-Error 符号码经统一信封
    # 转译进 error.details，非响应头）；各白名单格式的合法容器头放行
    fake = client.post("/uploads", files={"file": ("fake.mp4", b"hello" * 100, "video/mp4")})
    assert fake.status_code == 422
    assert fake.json()["error"]["details"] == "UNSUPPORTED_TYPE"
    assert not list((up / dev).glob("*fake.mp4"))
    wav = b"RIFF\x24\x08\x00\x00WAVEfmt " + b"\x00" * 16
    assert client.post("/uploads", files={"file": ("a.wav", wav, "audio/wav")}).status_code == 200
    mkv = b"\x1a\x45\xdf\xa3" + b"\x00" * 28
    assert client.post("/uploads", files={"file": ("a.mkv", mkv, "video/x-matroska")}).status_code == 200
    mp3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 22
    assert client.post("/uploads", files={"file": ("a.mp3", mp3, "audio/mpeg")}).status_code == 200
    m4a = b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 16
    assert client.post("/uploads", files={"file": ("a.m4a", m4a, "audio/mp4")}).status_code == 200
    # 裸 MPEG 帧同步（无 ID3 前缀的 mp3）也放行
    raw_mp3 = b"\xff\xfb\x90\x00" + b"\x00" * 28
    assert client.post("/uploads", files={"file": ("b.mp3", raw_mp3, "audio/mpeg")}).status_code == 200
    # wav 壳但非 WAVE（如 AVI 的 RIFF 头）→ 拒
    assert client.post("/uploads",
                       files={"file": ("c.wav", b"RIFF\x00\x00\x00\x00AVI ", "audio/wav")}).status_code == 422

    # 单文件上限 → 413，且半截文件已删
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_api, "UPLOAD_MAX_GB", 0.000001)  # ≈1KB
        big = client.post("/uploads", files={"file": ("big.mp4", b"x" * 4096, "video/mp4")})
    assert big.status_code == 413
    assert not list((up / dev).glob("big.mp4"))

    # 每用户配额 → 413，且刚传的文件回滚删除
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_api, "UPLOAD_QUOTA_GB", 0.000001)
        quota = client.post("/uploads", files={"file": ("q.mp4", mp4, "video/mp4")})
    assert quota.status_code == 413
    assert not list((up / dev).glob("q.mp4"))


def test_sweep_uploads_ttl(client, monkeypatch):
    import video2md.api as _api
    up = Path(_TMP) / "uploads-sweep"
    f = up / _dev_uid() / "old.mp4"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x")
    stale = time.time() - 100000  # >24h TTL
    os.utime(f, (stale, stale))
    monkeypatch.setattr(_api, "UPLOADS_DIR", up)
    assert _api._sweep_uploads() == 1 and not f.exists()
    assert _api._sweep_uploads() == 0  # 幂等


# ---------------------------------------------------------------- 成本归因→预算闸（P2-2）

def test_budget_gate_and_cost_endpoint(client, monkeypatch):
    import video2md.api as _api
    import video2md.runs as _runs_mod
    put_identity("test", "alice", "uid_alice")
    # 本月已结算 $5 的 run（json 档：created 用本月真实日期保证进统计窗）
    month_now = datetime.now().strftime("%Y-%m")
    rec = _seed_run("costy", "uid_alice")
    rec["created"] = f"{month_now}-15T00:00:00"
    rec["cost_usd"] = 5.0
    rec["usage"] = {"llm_in_tok": 1000, "llm_out_tok": 2000, "gpu_s": 60.0}
    with _runs_mod._runs_file_lock():
        _runs_mod._runs_merge(rec)

    _stub_resolve(monkeypatch)
    # 预算 $4 < 已花 $5 → POST /runs 429 BUDGET_EXCEEDED（信封 details 符号码）
    monkeypatch.setattr(_api, "BUDGET_MONTH_USD", 4.0)
    r = client.post("/runs", json={"url": "https://example.test/c1", "resolve_only": True},
                     headers={"X-Test-Auth": "alice"})
    assert r.status_code == 429 and r.json()["error"]["details"] == "BUDGET_EXCEEDED", r.text
    assert "5.00" in r.json()["error"]["message"] and "4.00" in r.json()["error"]["message"]
    # 预算 $10 > $5 → 放行；预算 0 = 不设闸
    monkeypatch.setattr(_api, "BUDGET_MONTH_USD", 10.0)
    assert client.post("/runs", json={"url": "https://example.test/c2", "resolve_only": True},
                       headers={"X-Test-Auth": "alice"}).status_code == 200
    monkeypatch.setattr(_api, "BUDGET_MONTH_USD", 0.0)
    assert client.post("/runs", json={"url": "https://example.test/c3", "resolve_only": True},
                       headers={"X-Test-Auth": "alice"}).status_code == 200
    # 上月消费不入本月预算（月度重置语义）
    old = _seed_run("oldmo", "uid_alice")
    old["created"] = "2020-01-15T00:00:00"
    old["cost_usd"] = 999.0
    with _runs_mod._runs_file_lock():
        _runs_mod._runs_merge(old)
    monkeypatch.setattr(_api, "BUDGET_MONTH_USD", 4.0)
    assert client.post("/runs", json={"url": "https://example.test/c4", "resolve_only": True},
                       headers={"X-Test-Auth": "alice"}).status_code == 429  # 本月 $5 仍超

    # GET /cost：本人月度归因（spend/usage/单价回显/budget）
    monkeypatch.setattr(_api, "BUDGET_MONTH_USD", 10.0)
    c = client.get("/cost", headers={"X-Test-Auth": "alice"})
    assert c.status_code == 200, c.text
    body = c.json()
    assert body["spend_usd"] == 5.0 and body["budget_usd"] == 10.0
    assert body["usage"]["llm_in_tok"] == 1000 and body["usage"]["gpu_s"] == 60.0
    assert set(body["prices"]) == {"llm_in_mtok", "llm_out_mtok", "llm_cached_in_mtok", "gpu_min"}
    assert "prices" in body and body["month"].startswith(month_now)


def test_usage_accumulate_and_settle(monkeypatch, tmp_path):
    """归因链路单测：ContextVar 绑定→bump 累计→settle 按单价结算落记录（幂等）。"""
    import video2md.runs as _runs_mod
    import video2md.util as _util
    from video2md import llm as _llm
    monkeypatch.setattr(_runs_mod, "RUNS_FILE", tmp_path / "runs.json")
    monkeypatch.setattr(_runs_mod, "_RUNS_LOCKFILE", _runs_mod.RUNS_FILE.with_suffix(".lock"))

    rid = "usetest"
    with _runs_mod._runs_file_lock():
        _runs_mod._runs_merge({"run_id": rid, "owner": "u", "status": "done",
                               "created": "2026-01-01", "artifacts": {}, "entries": []})
    # 未绑定 run：bump 静默跳过（CLI/测试直调不记账）
    _runs_mod.bump_usage(llm_in_tok=111)
    assert not _runs_mod._USAGE
    # 绑定后：llm 观测钩子与 GPU 钩子都累计
    _util.bind_current_run(rid)
    _llm._llm_observed("note", "completed", 0.0, 1, {"usage": {"prompt_tokens": 100,
                                   "completion_tokens": 50}}, None, "s", "u", "text")
    _runs_mod.bump_usage(gpu_s=90.0)
    assert _runs_mod._USAGE[rid]["llm_in_tok"] == 100 and _runs_mod._USAGE[rid]["gpu_s"] == 90.0
    # 结算：单价 in=2 / out=4 USD/MTok，GPU=0.03 USD/min → 100/1e6*2 + 50/1e6*4 + 1.5min*0.03
    monkeypatch.setattr(_runs_mod, "PRICE_LLM_IN_MTOK", 2.0)
    monkeypatch.setattr(_runs_mod, "PRICE_LLM_OUT_MTOK", 4.0)
    monkeypatch.setattr(_runs_mod, "PRICE_GPU_MIN", 0.03)
    settled = _runs_mod.settle_cost(rid)
    assert settled["cost_usd"] == round(100 / 1e6 * 2 + 50 / 1e6 * 4 + 1.5 * 0.03, 6)
    assert settled["usage"]["gpu_s"] == 90.0
    # Repeating settlement without new calls is idempotent.
    assert _runs_mod.settle_cost(rid) is None
    # A retry that really consumes more tokens must add that new batch.
    _runs_mod.bump_usage(llm_in_tok=999)
    again = _runs_mod.settle_cost(rid)
    assert again["cost_usd"] == round(settled["cost_usd"] + 999 / 1e6 * 2, 6)
    assert again["usage"]["llm_in_tok"] == 1099
    assert _runs_mod.settle_cost(rid) is None
    assert _runs_mod._load_runs()[rid]["usage"]["llm_in_tok"] == 1099  # Retry adds to persisted usage.


def test_cost_cli_export(client, monkeypatch, capsys):
    import video2md.runs as _runs_mod
    put_identity("test", "alice", "uid_alice")
    month_now = datetime.now().strftime("%Y-%m")
    rec = _seed_run("exp1", "uid_alice")
    rec["created"] = f"{month_now}-02T00:00:00"
    rec["cost_usd"] = 1.25
    rec["usage"] = {"llm_in_tok": 10, "llm_out_tok": 20, "gpu_s": 30.0}
    with _runs_mod._runs_file_lock():
        _runs_mod._runs_merge(rec)
    import video2md.__main__ as main_mod
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("sys.argv", ["video2md", "cost", "--uid", "uid_alice"])
        main_mod.main()
    out = capsys.readouterr().out.strip().splitlines()
    rows = [json.loads(ln) for ln in out]
    detail = [r for r in rows if not r.get("summary")]
    summary = [r for r in rows if r.get("summary")][0]
    assert any(r["run_id"] == "exp1" and r["cost_usd"] == 1.25 for r in detail)
    assert summary["spend_usd"] == 1.25 and summary["runs_n"] == 1 and summary["llm_in_tok"] == 10


# ---------------------------------------------------------------- 匿名面限流（P0-2）

def test_device_rate_limit_and_xff(client, monkeypatch):
    import video2md.api as _api

    async def _fake_google_code():
        return {"device_code": "dc", "user_code": "uc", "expires_in": 1800,
                "interval": 5, "verification_url": "https://example.test/verify"}

    monkeypatch.setattr(_api, "_google_device_code", _fake_google_code)
    monkeypatch.setattr(_api, "DEVICE_RATE_PER_MIN", 3)

    _api._DEVICE_WIN.clear()
    # 同 IP 连打 3 次过、第 4 次 429 RATE_LIMITED（信封 details 符号码）
    for _ in range(3):
        assert client.post("/auth/device/code").status_code == 200
    r = client.post("/auth/device/code")
    assert r.status_code == 429 and r.json()["error"]["details"] == "RATE_LIMITED", r.text
    # token 端点同桶（同一次登录的两半）：code 打满后 token 也拒——且拒在调 Google 之前
    t = client.post("/auth/device/token", json={"device_code": "dc"})
    assert t.status_code == 429 and t.json()["error"]["details"] == "RATE_LIMITED"
    # 桶独立：换 IP（TRUST_XFF=on + XFF 末位）立即恢复
    monkeypatch.setattr(_api, "TRUST_XFF", "on")
    _api._DEVICE_WIN.clear()
    for _ in range(3):
        assert client.post("/auth/device/code",
                           headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 200
    assert client.post("/auth/device/code",
                       headers={"X-Forwarded-For": "198.51.100.7"}).status_code == 429
    assert client.post("/auth/device/code",
                       headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 200
    # 尾位语义：客户端伪造前置链，末位（反代 append 的真实 IP）才是记账键——
    # 同尾位不同伪造前缀共享同一桶（打满即 429），换尾位即新桶
    for _ in range(2):
        assert client.post("/auth/device/code",
                           headers={"X-Forwarded-For": f"9.9.9.{_}, 203.0.113.9"}).status_code == 200
    assert client.post("/auth/device/code",
                       headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.9"}).status_code == 429
    assert client.post("/auth/device/code",
                       headers={"X-Forwarded-For": "5.5.5.5, 203.0.113.8"}).status_code == 200
    # 伪造对 TRUST_XFF=off 无效：socket 对端（testclient）记账
    monkeypatch.setattr(_api, "TRUST_XFF", "off")
    _api._DEVICE_WIN.clear()
    assert client.post("/auth/device/code",
                       headers={"X-Forwarded-For": "6.6.6.6"}).status_code == 200
    assert client.post("/auth/device/code",
                       headers={"X-Forwarded-For": "7.7.7.7"}).status_code == 200
    # 0 = 不设限
    monkeypatch.setattr(_api, "DEVICE_RATE_PER_MIN", 0)
    _api._DEVICE_WIN.clear()
    for _ in range(10):
        assert client.post("/auth/device/code").status_code == 200
    monkeypatch.setattr(_api, "DEVICE_RATE_PER_MIN", 30)


# ---------------------------------------------------------------- 协作式取消（P1-2）

def _cancel_stubs(monkeypatch, graph_fn):
    """取消三测共用：假 resolve 落 runs.json 记录 + 假图（graph_fn 自带取消行为）。
    注意：真 run_graph 会把终态 merge 回 runs.json 记录（GET /runs/{id} 顶层 status
    读记录），假图必须同款收尾——只改 _TASKS 的假图会让顶层 status 永远停在 running。"""
    import asyncio

    import video2md.api as _api
    import video2md.graph as graph_mod
    import video2md.runs as _runs_mod

    def fake_resolve(url, part_items, force, run_id, owner):
        rec = {"run_id": run_id, "url": url, "part_items": part_items, "owner": owner,
               "created": "2026-01-01T00:00:00", "tmp": "", "total": 1,
               "entries": [{"playlist_index": 1, "url": url}],
               "artifacts": {}, "stages": {}, "status": "created"}
        db = _runs_mod._load_runs()
        db[run_id] = rec
        _runs_mod._save_runs(db)
        return rec, "", False

    def recording_graph(url, *, part_items="", force=False, review=False, max_frames=60,
                        run_id="", checkpointer=None, owner=""):
        final = graph_fn(url, run_id=run_id)  # (status, errors) —— 真实语义的取消/完成判定
        with _runs_mod._runs_file_lock():
            rec = _runs_mod._load_runs().get(run_id) or {"run_id": run_id}
            rec["status"] = final["status"]
            if final.get("errors"):
                rec["errors"] = final["errors"]
            _runs_mod._runs_merge(rec)
        return final

    monkeypatch.setattr(graph_mod, "resolve_run", fake_resolve)
    monkeypatch.setattr(_api, "run_graph", recording_graph)
    monkeypatch.setattr(_api, "_SEMA", asyncio.Semaphore(1))
    return _api, _runs_mod


def _poll_status(client, run_id, want, timeout_s=8.0) -> str:
    deadline = time.monotonic() + timeout_s
    st = ""
    while time.monotonic() < deadline:
        st = client.get(f"/runs/{run_id}").json().get("status") or ""
        if st == want:
            return st
        time.sleep(0.05)
    return st


def test_cancel_queued_immediate(client, monkeypatch):
    def fake_graph(url, *, run_id=""):
        time.sleep(0.5)  # 首跑占住唯一信号量 → 第二个 run 排队
        return {"status": "done", "run_id": run_id}

    _api, _runs_mod = _cancel_stubs(monkeypatch, fake_graph)
    first = client.post("/runs", json={"url": "https://example.test/cq1"}).json()
    second = client.post("/runs", json={"url": "https://example.test/cq2"}).json()
    assert second["status"] == "queued"

    r = client.post(f"/runs/{second['run_id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "error", r.text
    # 立即翻终态：任务视图 + runs 记录双重留痕（error(cancelled)）
    assert client.get(f"/runs/{second['run_id']}").json()["status"] == "error"
    rec = _runs_mod._load_runs()[second["run_id"]]
    assert rec["status"] == "error" and any("cancelled" in e for e in rec.get("errors", []))

    # 首跑完成后，撤销项不得被 _drive 唤醒驱动（静默退场，不翻 running/done）；
    # 取消标志归 _drive 醒来后的退场路径/finally 清——端点不清（防复活竞态）。
    assert _poll_status(client, first["run_id"], "done") == "done"
    time.sleep(0.5)  # 给唤醒后的退场路径留足窗口
    st = client.get(f"/runs/{second['run_id']}").json().get("status")
    assert st == "error", f"撤销的排队项被唤醒驱动成了 {st}"
    assert not _runs_mod.cancel_requested(second["run_id"])


def test_cancel_running_cooperative(client, monkeypatch):
    def cooperative_graph(url, *, run_id=""):
        # 模拟条目循环头检查（真检查在 _run_stage_inner，另有直测）：标志到 → error 留痕
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _runs_mod.cancel_requested(run_id):
            time.sleep(0.02)
        if _runs_mod.cancel_requested(run_id):
            return {"status": "error", "run_id": run_id,
                    "errors": ["用户取消（cancelled）：条目循环头拦截"]}
        return {"status": "done", "run_id": run_id}

    _api, _runs_mod = _cancel_stubs(monkeypatch, cooperative_graph)
    rid = client.post("/runs", json={"url": "https://example.test/cc-r1"}).json()["run_id"]
    assert _poll_status(client, rid, "running") == "running"

    c = client.post(f"/runs/{rid}/cancel")
    assert c.status_code == 200 and c.json()["status"] == "cancelling", c.text
    assert _poll_status(client, rid, "error") == "error"
    assert not _runs_mod.cancel_requested(rid)  # _drive finally 清标志


def test_cancel_terminal_conflict_and_access(client):
    put_identity("test", "alice", "uid_alice")
    put_identity("test", "bob", "uid_bob")
    _seed_run("r_done", "uid_alice")  # 终态 → 409 NOT_CANCELLABLE
    r = client.post("/runs/r_done/cancel", headers={"X-Test-Auth": "alice"})
    assert r.status_code == 409
    assert r.json()["error"]["details"] == "NOT_CANCELLABLE"
    assert client.post("/runs/r_done/cancel", headers={"X-Test-Auth": "bob"}).status_code == 403
    assert client.post("/runs/ghost/cancel", headers={"X-Test-Auth": "alice"}).status_code == 404


def test_pg_unavailable_503_envelope(client, monkeypatch):
    """P1-8：PG 超预算 → 快速 503（统一信封），不挂死不 500。"""
    from psycopg_pool import PoolTimeout

    import video2md.api as _api

    class _DeadStore:
        def total_active(self):
            raise PoolTimeout("couldn't get a connection after 10.00 sec")

    _api._TASKS_PG = _DeadStore()
    _api._tasks_pg_resolved = True
    try:
        r = client.post("/runs", json={"url": "https://example.test/pg-dead"})
        assert r.status_code == 503, r.text
        assert r.json()["error"]["details"] == "PG_UNAVAILABLE"
        # /queue 仪表既有兜底语义不受影响（gauge 不许炸，回退本进程视图）
        assert client.get("/queue").status_code == 200
    finally:
        _api._TASKS_PG = None
        _api._tasks_pg_resolved = False


def test_stage_loop_head_cancel_gate():
    """真检查点直测：_run_stage_inner 条目循环头，标志在 → SystemExit(cancelled)；
    标志不在 → skipped-only 条目正常走完（不被误伤）。"""
    import tempfile as _tf

    from video2md import runs as rmod

    class _Span:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def __setitem__(self, k, v):
            pass

    class _Log:
        def span(self, *a, **k):
            return _Span()

    run = {"run_id": "cx1", "tmp": _tf.mkdtemp(prefix="v2md-cancel-"), "entries": [{"url": "u"}],
           "artifacts": {"u": {"skipped": True}}, "stages": {}}
    rmod.request_cancel("cx1")
    try:
        rmod._run_stage_inner(run, "download", {}, _Log())
        raise AssertionError("循环头取消必须抛 SystemExit")
    except SystemExit as exc:
        assert "cancelled" in str(exc)
    finally:
        rmod.forget_cancel("cx1")
    rmod._run_stage_inner(run, "download", {}, _Log())  # 无标志：skipped 正常走完不抛


# ---------------------------------------------------------------- 并发执行（终局布局·执行层）

def test_concurrent_runs_and_per_user_cap(client, monkeypatch):
    import asyncio

    import video2md.api as _api

    def fake_graph(url, *, part_items="", force=False, review=False, max_frames=60,
                   run_id="", checkpointer=None, owner=""):
        time.sleep(0.25)
        return {"status": "done", "run_id": run_id}

    def fake_resolve(url, part_items, force, run_id, owner):
        rec = {"run_id": run_id, "url": url, "part_items": part_items, "owner": owner,
               "created": "2026-01-01T00:00:00", "tmp": "", "total": 1,
               "entries": [{"playlist_index": 1, "url": url}],
               "artifacts": {}, "stages": {}, "status": "created"}
        db = runs._load_runs()
        db[run_id] = rec
        runs._save_runs(db)
        return rec, "", False

    import video2md.graph as graph_mod
    monkeypatch.setattr(graph_mod, "resolve_run", fake_resolve)
    monkeypatch.setattr(_api, "run_graph", fake_graph)
    monkeypatch.setattr(_api, "_SEMA", asyncio.Semaphore(2))  # 并发闸门=2
    monkeypatch.setattr(_api, "MAX_CONCURRENT_RUNS", 2)       # /queue 的 limits 档位同源
    made: list[str] = []
    try:
        for i in (1, 2):
            r = client.post("/runs", json={"url": f"https://example.test/c{i}"})
            assert r.status_code == 200, r.text
            made.append(r.json()["run_id"])
        time.sleep(0.05)
        q = client.get("/queue").json()
        assert q["running"] == 2, q  # 信号量=2：两条同时进入 running
        assert q["limits"]["max_concurrent"] == 2

        # 第三条：每用户在途上限（收紧到 2）→ 429 USER_QUEUE_FULL
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_api, "USER_QUEUE_MAX", 2)
            third = client.post("/runs", json={"url": "https://example.test/c3"})
            assert third.status_code == 429

        deadline = time.time() + 5
        while time.time() < deadline:
            if client.get("/queue").json()["running"] == 0:
                break
            time.sleep(0.05)
        final = {rid: client.get(f"/runs/{rid}").json()["task"]["status"] for rid in made}
        assert final == {made[0]: "done", made[1]: "done"}, final
    finally:
        for rid in made:
            _api._TASKS.pop(rid, None)


def test_current_log_contextvar_two_threads():
    """并发多 run 时 current_log 按 ContextVar 取本 run 的 log（不再 dict 第一个）。"""
    from video2md import util
    log_a = util.RunLog("stageA", run_id="runA")
    log_b = util.RunLog("stageB", run_id="runB")
    util._STAGE_LOGS.update(runA=log_a, runB=log_b)
    errs: list = []

    def worker(rid, log):
        try:
            util.bind_current_run(rid)
            for _ in range(50):
                got = util.current_log()
                assert got is log, f"{rid}: 实得 {getattr(got, 'run_id', None)}"
                time.sleep(0.001)
        except AssertionError as exc:
            errs.append(exc)

    t1 = threading.Thread(target=worker, args=("runA", log_a))
    t2 = threading.Thread(target=worker, args=("runB", log_b))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    util._STAGE_LOGS.pop("runA", None)
    util._STAGE_LOGS.pop("runB", None)
    assert not errs, errs


def test_state_save_concurrent_no_tear():
    """state.json 原子写：四线程并发全量写，读侧恒为合法完整 JSON。"""
    from video2md import util

    def worker(i: int) -> None:
        for k in range(50):
            util.save_state({f"k{i}": k})

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert isinstance(util.load_state(), dict)  # 无半截 JSON / JSONDecodeError


# ---------------------------------------------------------------- P4b 任务表 PG 真相源（假后端）

class _FakeTaskStore:
    """PostgresTaskStore 的读侧替身：只实现 api 接线用到的方法，调用可断言。"""

    def __init__(self, *, queued=0, running=0, active_total=0, mine=0,
                 alive_ids=(), window_counts=None):
        self.counts_map = {"queued": queued, "running": running}
        self._total_active = active_total
        self._mine = mine
        self.alive_ids = set(alive_ids)
        self.window_counts = dict(window_counts or {})
        self.calls: list[str] = []

    def counts(self) -> dict:
        self.calls.append("counts")
        return dict(self.counts_map)

    def total_active(self) -> int:
        self.calls.append("total_active")
        return self._total_active

    def active_for(self, uid: str) -> int:
        self.calls.append(f"active_for:{uid}")
        return self._mine

    def is_alive(self, run_id: str) -> bool:
        self.calls.append(f"is_alive:{run_id}")
        return run_id in self.alive_ids

    def submit_window_count(self, uid: str) -> int:
        self.calls.append(f"submit_window_count:{uid}")
        return self.window_counts.get(uid, 0)

    def note_claimed_by(self, run_id: str, proc: str) -> None:
        self.calls.append(f"note_claimed_by:{run_id}")


def test_tasks_pg_backend_readside(client, monkeypatch):
    """PG 档读侧换源：/queue、配额闸、滑窗全走 fake backend，不再碰进程内 _TASKS。"""
    import video2md.api as _api

    fake = _FakeTaskStore(queued=7, running=3, active_total=10, mine=4)
    monkeypatch.setattr(_api, "_TASKS_PG", fake)
    monkeypatch.setattr(_api, "_tasks_pg_resolved", True)
    q = client.get("/queue").json()
    assert q["queue_len"] == 7 and q["running"] == 3
    assert "counts" in fake.calls

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_api, "QUEUE_MAX", 9)          # fake total_active=10 → 满
        r = client.post("/runs", json={"url": "https://example.test/a"})
        assert r.status_code == 429 and r.json()["error"]["details"] == "QUEUE_FULL"
    assert "total_active" in fake.calls

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_api, "QUEUE_MAX", 0)           # 放行总闸
        mp.setattr(_api, "USER_QUEUE_MAX", 3)      # fake active_for=4 → 超
        r = client.post("/runs", json={"url": "https://example.test/b"})
        assert r.status_code == 429 and r.json()["error"]["details"] == "USER_QUEUE_FULL"

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_api, "USER_QUEUE_MAX", 0)
        mp.setattr(_api, "USER_SUBMIT_PER_MIN", 2)  # fake 窗口计数已达 2
        fake.window_counts[_dev_uid()] = 2
        r = client.post("/runs", json={"url": "https://example.test/c"})
        assert r.status_code == 429 and r.json()["error"]["details"] == "RATE_LIMITED"
    assert any(c.startswith("submit_window_count:") for c in fake.calls)


def test_tasks_pg_dedup_alive(client, monkeypatch):
    """dedup 判活 PG 档：本进程 _TASKS 无活条目、DB is_alive=True → 不重投，回旧状态。"""
    import video2md.api as _api
    import video2md.graph as graph_mod

    dup_rec = {"run_id": "dupx", "url": "https://example.test/dup", "part_items": "",
               "owner": _dev_uid(), "created": "2026-01-01T00:00:00", "tmp": "",
               "total": 1, "entries": [{"playlist_index": 1, "url": "https://example.test/dup"}],
               "artifacts": {}, "stages": {}, "status": "running"}

    def fake_resolve(url, part_items, force, run_id, owner):
        return dup_rec, "", True

    fake = _FakeTaskStore(alive_ids={"dupx"})
    monkeypatch.setattr(graph_mod, "resolve_run", fake_resolve)
    monkeypatch.setattr(_api, "_TASKS_PG", fake)
    monkeypatch.setattr(_api, "_tasks_pg_resolved", True)
    assert "dupx" not in _api._TASKS  # 前提：本进程视角「驱动已死」
    r = client.post("/runs", json={"url": "https://example.test/dup"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("dedup") is True and body["status"] == "running"
    assert "is_alive:dupx" in fake.calls
    assert "dupx" not in _api._TASKS  # 关键：不得重新入队驱动（他进程正跑）


def test_tasks_pg_dead_dup_requeues(client, monkeypatch):
    """DB 判活=False（崩溃残档）→ 既有重驱路径不变。"""
    import video2md.api as _api
    import video2md.graph as graph_mod

    dup_rec = {"run_id": "dead1", "url": "https://example.test/dead", "part_items": "",
               "owner": _dev_uid(), "created": "2026-01-01T00:00:00", "tmp": "",
               "total": 1, "entries": [{"playlist_index": 1, "url": "https://example.test/dead"}],
               "artifacts": {}, "stages": {}, "status": "running"}

    def fake_resolve(url, part_items, force, run_id, owner):
        return dup_rec, "", True

    enq: list[str] = []

    def spy_enqueue(url, part_items, force, review, max_frames, run_id="", owner=""):
        enq.append(run_id)
        return {"run_id": run_id, "status": "queued"}

    fake = _FakeTaskStore(alive_ids=set())
    monkeypatch.setattr(graph_mod, "resolve_run", fake_resolve)
    monkeypatch.setattr(_api, "_TASKS_PG", fake)
    monkeypatch.setattr(_api, "_tasks_pg_resolved", True)
    monkeypatch.setattr(_api, "_enqueue", spy_enqueue)
    r = client.post("/runs", json={"url": "https://example.test/dead"})
    assert r.status_code == 200
    assert r.json()["status"] == "queued" and r.json().get("dedup") is True
    assert enq == ["dead1"]


def run_all() -> None:  # `python3 test_api_contract.py` 兜底（无 pytest 时）
    # 必须走子进程：直跑时本模块已以 __main__ 身份 exec 过一遍（env 段 + import video2md.*），
    # 同进程再 pytest.main(__file__) 会把本文件二次加载成独立模块 → 两份 _TMP/两份 config
    # 快照落在不同临时目录，读侧写侧错位（实测泄漏打穿 deliver 幂等与 owner 隔离两例）。
    import subprocess
    import sys
    sys.exit(subprocess.run([sys.executable, "-m", "pytest", __file__, "-q"]).returncode)


if __name__ == "__main__":
    run_all()


def test_prefs_first_request_creates_nested_directory(client, monkeypatch, tmp_path):
    from video2md import prefs
    monkeypatch.setattr(prefs, "_STORE", prefs.JsonPrefsStore(tmp_path / "new-data" / "prefs.json"))
    monkeypatch.setattr(prefs, "_store_resolved", True)
    put_identity("test", "alice", "uid_alice")
    _seed_run("prefs_first", "uid_alice")
    h = {"X-Test-Auth": "alice"}
    assert client.get("/prefs", headers=h).json() == {"items": []}
    assert client.put("/prefs/prefs_first", headers=h, json={"fav": True}).status_code == 200
    assert client.get("/prefs", headers=h).json()["items"][0]["fav"] is True


def test_web_media_requires_owner_and_referenced_file(client, tmp_path):
    from video2md.config import OUT_DIR
    put_identity("test", "alice", "uid_alice")
    put_identity("test", "bob", "uid_bob")
    _seed_run("web_media", "uid_alice")
    attachments = OUT_DIR / "attachments"
    attachments.mkdir(parents=True, exist_ok=True)
    (attachments / "web-frame.jpg").write_bytes(b"jpeg-fixture")
    (attachments / "web-clip.mp4").write_bytes(b"mp4-fixture")
    (attachments / "unreferenced.jpg").write_bytes(b"private")
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"secret")
    (attachments / "escape.jpg").symlink_to(secret)
    records = runs._load_runs()
    art = next(iter(records["web_media"]["artifacts"].values()))
    art.update(note=str(OUT_DIR / "web-media.md"), md=(
        "![[attachments/web-frame.jpg]]\n![[attachments/web-clip.mp4]]\n![[attachments/escape.jpg]]"))
    runs._save_runs(records)
    h = {"X-Test-Auth": "alice"}
    response = client.get("/runs/web_media/media/web-frame.jpg", headers=h)
    assert response.status_code == 200 and response.content == b"jpeg-fixture"
    assert response.headers["content-type"].startswith("image/jpeg")
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/runs/web_media/media/web-clip.mp4", headers=h).content == b"mp4-fixture"
    part = client.get("/runs/web_media/media/web-clip.mp4", headers={**h, "Range": "bytes=0-3"})
    assert part.status_code == 206 and part.content == b"mp4-"
    assert part.headers["content-range"] == "bytes 0-3/11"
    assert client.get("/runs/web_media/media/web-clip.mp4", headers={
        "X-Test-Auth": "bob", "Range": "bytes=0-3"}).status_code == 403
    assert client.get("/runs/web_media/media/web-frame.jpg", headers={"X-Test-Auth": "bob"}).status_code == 403
    assert client.get("/runs/web_media/media/unreferenced.jpg", headers=h).status_code == 404
    assert client.get("/runs/web_media/media/escape.jpg", headers=h).status_code == 404


def test_promote_changes_actual_execution_order(client, monkeypatch):
    """Three real enqueued coroutines; external video work is deterministic and gated."""
    started = threading.Event()
    release = threading.Event()
    order = []

    def workload(url, *, run_id=""):
        order.append(run_id)
        if len(order) == 1:
            started.set()
            assert release.wait(5)
        return {"status": "done", "run_id": run_id}

    _cancel_stubs(monkeypatch, workload)
    first = client.post("/runs", json={"url": "https://example.test/order1"}).json()["run_id"]
    assert started.wait(3)
    second = client.post("/runs", json={"url": "https://example.test/order2"}).json()["run_id"]
    third = client.post("/runs", json={"url": "https://example.test/order3"}).json()["run_id"]
    try:
        assert client.post(f"/runs/{third}/promote").status_code == 200
    finally:
        release.set()
    assert _poll_status(client, second, "done") == "done"
    assert order == [first, third, second]


def test_retry_cancelled_queue_does_not_revive_old_driver(client, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    order = []

    def workload(url, *, run_id=""):
        order.append(run_id)
        if len(order) == 1:
            started.set()
            assert release.wait(5)
        return {"status": "done", "run_id": run_id}

    _cancel_stubs(monkeypatch, workload)
    client.post("/runs", json={"url": "https://example.test/retry1"})
    assert started.wait(3)
    retry = client.post("/runs", json={"url": "https://example.test/retry2"}).json()["run_id"]
    try:
        assert client.post(f"/runs/{retry}/cancel").status_code == 200
        assert client.post(f"/runs/{retry}/resume").status_code == 200
        assert client.post(f"/runs/{retry}/resume").status_code == 409
    finally:
        release.set()
    assert _poll_status(client, retry, "done") == "done"
    assert order.count(retry) == 1
    assert client.delete(f"/runs/{retry}").status_code == 200
    assert client.get(f"/runs/{retry}").status_code == 404


def test_cancelling_state_survives_list_refresh(client, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def workload(url, *, run_id=""):
        started.set()
        assert release.wait(5)
        return {"status": "error", "errors": ["cancelled"], "run_id": run_id}

    _cancel_stubs(monkeypatch, workload)
    rid = client.post("/runs", json={"url": "https://example.test/long-stage"}).json()["run_id"]
    assert started.wait(3)
    try:
        assert client.post(f"/runs/{rid}/cancel").json()["status"] == "cancelling"
        assert client.get(f"/runs/{rid}").json()["status"] == "cancelling"
        items = client.get("/runs").json()["items"]
        assert next(row for row in items if row["run_id"] == rid)["status"] == "cancelling"
    finally:
        release.set()
    assert _poll_status(client, rid, "error") == "error"


def test_partial_deliver_retry_preserves_already_written_media_and_notes(client, deliver):
    put_identity("test", "alice", "uid_alice")
    rec = _seed_delivered_run("retry_shelf", "uid_alice", deliver)
    rec["status"] = "partial"
    with runs._runs_file_lock():
        runs._runs_merge(rec)
    h = {"X-Test-Auth": "alice"}
    response = client.post("/runs/retry_shelf/resume", headers=h)
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        status = client.get("/runs/retry_shelf", headers=h).json()["status"]
        if status in ("done", "error", "partial"):
            break
        time.sleep(0.05)
    assert status == "done"
    notes = client.get("/runs/retry_shelf/note", headers=h)
    assert notes.status_code == 200 and notes.json()["notes"][0]["md"] == "# retry_shelf 正文"
    bundle = client.get("/runs/retry_shelf/bundle", headers=h)
    assert bundle.status_code == 200
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        assert "attachments/a-f001.jpg" in archive.namelist()


def test_unpromoted_jobs_keep_submission_order_even_with_identical_timestamps(client, monkeypatch):
    from types import SimpleNamespace
    started, release = threading.Event(), threading.Event()
    order = []
    def workload(url, *, run_id=""):
        order.append(run_id)
        if len(order) == 1:
            started.set()
            assert release.wait(5)
        return {"status": "done", "run_id": run_id}
    api, _runs = _cancel_stubs(monkeypatch, workload)
    ids = iter(["ffffffff", "eeeeeeee", "aaaaaaaa"])
    monkeypatch.setattr(api, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=next(ids))))
    monkeypatch.setattr(api, "_now", lambda: "2026-01-01T00:00:00")
    client.post("/runs", json={"url": "https://example.test/fifo1"})
    assert started.wait(3)
    try:
        client.post("/runs", json={"url": "https://example.test/fifo2"})
        client.post("/runs", json={"url": "https://example.test/fifo3"})
    finally:
        release.set()
    assert _poll_status(client, "aaaaaaaa", "done") == "done"
    assert _poll_status(client, "eeeeeeee", "done") == "done"
    assert order == ["ffffffff", "eeeeeeee", "aaaaaaaa"]


def test_deleting_task_keeps_monthly_cost_and_budget_accounting(client):
    put_identity("test", "alice", "uid_alice")
    rec = _seed_run("billed_deleted", "uid_alice")
    rec.update(created=datetime.now().isoformat(), cost_usd=2.5, usage={"llm_in_tok": 100, "gpu_s": 60})
    with runs._runs_file_lock():
        runs._runs_merge(rec)
    h = {"X-Test-Auth": "alice"}
    assert client.get("/cost", headers=h).json()["spend_usd"] == 2.5
    assert client.delete("/runs/billed_deleted", headers=h).status_code == 200
    with runs._runs_file_lock():
        runs._runs_merge(rec)  # 模拟删除前已读取旧快照的结算写回
    assert client.get("/runs/billed_deleted", headers=h).status_code == 404
    assert all(row["run_id"] != "billed_deleted" for row in client.get("/runs", headers=h).json()["items"])
    assert client.get("/cost", headers=h).json()["spend_usd"] == 2.5


def test_same_url_submission_reuses_an_already_queued_run(client, monkeypatch):
    import video2md.graph as graph_mod
    real_resolve = graph_mod.resolve_run
    started, release = threading.Event(), threading.Event()
    def workload(url, *, run_id=""):
        if url.endswith('/blocker'):
            started.set()
            assert release.wait(5)
        return {"status": "done", "run_id": run_id}
    api, run_mod = _cancel_stubs(monkeypatch, workload)
    monkeypatch.setattr(graph_mod, "resolve_run", real_resolve)
    monkeypatch.setattr(run_mod, "resolve_url", lambda url: [{"url": url, "playlist_index": 1}])
    monkeypatch.setattr(api, "USER_QUEUE_MAX", 0)
    client.post("/runs", json={"url": "https://example.test/blocker"})
    assert started.wait(3)
    try:
        queued = client.post("/runs", json={"url": "https://example.test/dedup-queued"}).json()
        duplicate = client.post("/runs", json={"url": "https://example.test/dedup-queued"}).json()
        assert duplicate["run_id"] == queued["run_id"] and duplicate["dedup"] is True
    finally:
        release.set()
    assert _poll_status(client, queued["run_id"], "done") == "done"


def test_json_startup_restores_persisted_queued_work(monkeypatch):
    def workload(url, *, run_id=""):
        return {"status": "done", "run_id": run_id}
    _cancel_stubs(monkeypatch, workload)
    put_identity("test", "alice", "uid_alice")
    _seed_run("queued_restart", "uid_alice", status="queued")
    with TestClient(app) as restarted:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = restarted.get("/runs/queued_restart", headers={"X-Test-Auth": "alice"}).json()["status"]
            if status == "done":
                break
            time.sleep(0.05)
        assert status == "done"


@pytest.fixture
def llm_settings_path(tmp_path, monkeypatch):
    from video2md import config
    monkeypatch.setattr(config, "PREFS_FILE", tmp_path / "prefs.json")
    monkeypatch.setattr(config, "PREFS_STORE", "json")
    monkeypatch.setitem(config.ENV, "V2MD_LLM_SETTINGS_KEY", "hex:" + "11" * 32)
    monkeypatch.delitem(config.ENV, "V2MD_LLM_SETTINGS_FILE", raising=False)
    return tmp_path / "llm-settings.json"


def test_llm_settings_private_persistent_update_and_clear(client, llm_settings_path):
    from video2md import llm_settings
    put_identity("test", "alice", "uid_alice")
    put_identity("test", "bob", "uid_bob")
    alice, bob = {"X-Test-Auth": "alice"}, {"X-Test-Auth": "bob"}
    endpoint = "/settings/llm"
    assert not client.get(endpoint, headers=alice).json()["configured"]
    r = client.put(endpoint, headers=alice, json={"base_url": "https://alice.example/v1/chat/completions/",
                                                 "api_key": "alice-secret", "model": "alice-model"})
    assert r.status_code == 200
    assert r.json() == {"base_url": "https://alice.example/v1", "model": "alice-model",
                        "has_api_key": True, "configured": True}
    assert "alice-secret" not in r.text
    assert "alice-secret" not in llm_settings_path.read_text()
    assert "alice-secret" not in client.get(endpoint, headers=alice).text
    assert not client.get(endpoint, headers=bob).json()["configured"]
    assert llm_settings_path.stat().st_mode & 0o777 == 0o600
    r = client.put(endpoint, headers=alice, json={"base_url": "https://alice.example/v2", "api_key": ""})
    assert r.status_code == 200
    assert llm_settings.load("uid_alice")["api_key"] == "alice-secret"
    client.put(endpoint, headers=bob, json={"base_url": "https://bob.example/v1", "api_key": "bob-secret"})
    assert client.delete(endpoint, headers=alice).json()["configured"] is False
    assert llm_settings.load("uid_alice") == {}
    assert llm_settings.load("uid_bob")["api_key"] == "bob-secret"


def test_llm_settings_plaintext_migration_reencrypts_without_echo(llm_settings_path):
    from video2md import llm_settings
    llm_settings_path.write_text(json.dumps({
        "uid_legacy": {"base_url": "https://legacy.example/v1",
                        "api_key": "legacy-secret", "model": "legacy"}
    }), encoding="utf-8")
    assert llm_settings.migrate_plaintext() == 1
    raw = llm_settings_path.read_text(encoding="utf-8")
    assert "legacy-secret" not in raw and "v1:aesgcm:" in raw
    assert llm_settings.load("uid_legacy")["api_key"] == "legacy-secret"


def test_llm_settings_wrong_encryption_key_fails_closed(llm_settings_path, monkeypatch):
    from video2md import llm_settings

    llm_settings.update("uid_wrong_key", base_url="https://example.test/v1", api_key="secret")
    monkeypatch.setitem(llm_settings.config.ENV, "V2MD_LLM_SETTINGS_KEY", "hex:" + "22" * 32)
    with pytest.raises(llm_settings.CredentialEncryptionError, match="解密失败"):
        llm_settings.load("uid_wrong_key")


def test_production_llm_observability_redacts_prompt_and_completion(monkeypatch):
    from video2md import config, llm

    span = {}
    monkeypatch.setattr(config, "OTEL_INCLUDE_CONTENT", False)
    llm._llm_observed("note", "ok", 0.0, 1,
                      {"usage": {"prompt_tokens": 3, "completion_tokens": 4}},
                      span, "private system prompt", "private user prompt", "private completion", "model")
    assert "_otel" not in span
    assert span["prompt_chars"] > 0 and span["completion_chars"] > 0

    monkeypatch.setattr(config, "OTEL_INCLUDE_CONTENT", True)
    debug_span = {}
    llm._llm_observed("note", "ok", 0.0, 1, {}, debug_span,
                      "system", "user", "completion", "model")
    assert debug_span["_otel"] == {"input": '[{"role": "system", "content": "system"}, {"role": "user", "content": "user"}]',
                                    "output": "completion"}


@pytest.mark.parametrize("url", ["file:///tmp/secret", "https://user:pass@example.com/v1",
                                      "https://example.com/v1?key=secret", "https://example.com:wrong/v1"])
def test_llm_settings_validation_never_reflects_key(client, llm_settings_path, url):
    r = client.put("/settings/llm", json={"base_url": url, "api_key": "must-not-echo"})
    assert r.status_code == 422 and "must-not-echo" not in r.text
    assert not llm_settings_path.exists()


def test_llm_settings_requires_initial_key(client, llm_settings_path):
    r = client.put("/settings/llm", json={"base_url": "https://example.com/v1"})
    assert r.status_code == 422 and not llm_settings_path.exists()
    r = client.put("/settings/llm", json={"base_url": "https://example.com/v1",
                                       "api_key": {"secret": "must-not-echo"}})
    assert r.status_code == 422 and "must-not-echo" not in r.text


def test_llm_user_credentials_follow_graph_threads_without_leaking(llm_settings_path, monkeypatch):
    import json
    from concurrent.futures import ThreadPoolExecutor

    from langgraph.graph import END, START, StateGraph

    from video2md import llm, llm_settings

    for uid in ("alice", "bob"):
        llm_settings.update(uid, base_url=f"https://{uid}.example/v1", api_key=uid + "-secret", model=uid)
    monkeypatch.setattr(llm, "LLM_BASE", "https://default.example/v1")
    monkeypatch.setattr(llm, "LLM_KEY", "default-secret")
    monkeypatch.setattr(llm, "LLM_MODEL", "default")
    monkeypatch.setattr(llm, "_llm_observed", lambda *a, **kw: None)
    calls = []

    def handler(request):
        calls.append((str(request.url), request.headers["authorization"], json.loads(request.content)["model"]))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(llm, "_TRANSPORT", httpx.MockTransport(handler))

    def invoke(uid):
        def node(state):
            assert llm.chat("sys", "usr", task="metadata", temperature=0, max_tokens=5) == "ok"
            return {}
        graph = StateGraph(dict)
        graph.add_node("llm", node)
        graph.add_edge(START, "llm")
        graph.add_edge("llm", END)
        with llm_settings.user_scope(uid):
            graph.compile().invoke({})
        assert llm_settings.current() is None

    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(invoke, ["alice", "bob"]))
    invoke("")
    assert set(calls) == {(f"https://{uid}.example/v1/chat/completions", f"Bearer {uid}-secret", uid)
                          for uid in ("alice", "bob", "default")}
    monkeypatch.setattr(llm, "_TRANSPORT", httpx.MockTransport(
        lambda req: httpx.Response(401, text="alice-secret")))
    with llm_settings.user_scope("alice"), pytest.raises(RuntimeError) as exc:
        llm.chat("sys", "usr", task="metadata", temperature=0, max_tokens=5)
    assert "alice-secret" not in str(exc.value)


def test_run_accounting_and_redacted_failure_reach_owned_list_and_detail(client):
    put_identity('test', 'alice', 'uid_alice')
    rec = _seed_run('diagnostic-bill', 'uid_alice', status='error')
    rec.update(usage={'llm_in_tok': 100, 'llm_out_tok': 50, 'gpu_s': 12.5}, cost_usd=0.000028,
               errors=['note 条目 1 LLM: HTTP 403 sk-do-not-expose-this-key https://upstream.test/?token=secret'])
    with runs._runs_file_lock():
        runs._runs_merge(rec)
    headers = {'X-Test-Auth': 'alice'}
    detail = client.get('/runs/diagnostic-bill', headers=headers).json()
    item = next(r for r in client.get('/runs', headers=headers).json()['items'] if r['run_id'] == rec['run_id'])
    for value in (detail, item):
        assert value['usage']['gpu_s'] == 12.5 and value['cost_usd'] == 0.000028
        assert value['diagnostic']['stage'] == 'note'
        assert '403' in value['diagnostic']['message']
        assert 'sk-do-not' not in str(value) and 'token=secret' not in str(value)
    assert client.get('/runs/diagnostic-bill', headers={'X-Test-Auth': 'bob'}).status_code == 403


def test_framework_colab_failure_is_visible_without_provider_secrets(client):
    put_identity('test', 'alice', 'uid_alice')
    rec = _seed_run('colab-framework-failure', 'uid_alice', status='error')
    rec['errors'] = [
        'RuntimeError: colab sessions 失败: /private/colab/token.json token=secret '
        'https://upstream.test/?key=secret',
    ]
    with runs._runs_file_lock():
        runs._runs_merge(rec)
    diagnostic = client.get('/runs/colab-framework-failure',
                            headers={'X-Test-Auth': 'alice'}).json()['diagnostic']
    assert diagnostic['stage'] == 'colab'
    assert 'Colab' in diagnostic['message'] and 'sessions' in diagnostic['message']
    assert '/private' not in diagnostic['message']
    assert 'token=secret' not in diagnostic['message']
    assert 'key=secret' not in diagnostic['message']


def test_drive_preserves_nested_graph_errors(monkeypatch):
    """node_fail returns errors under ``run``; the task view must retain them."""
    import video2md.api as _api

    run_id = "nested-graph-error"
    _api._TASKS[run_id] = {"status": "queued", "url": "fixture", "owner": ""}

    @asynccontextmanager
    async def slot(_run_id):
        yield

    monkeypatch.setattr(_api, "_execution_slot", slot)
    monkeypatch.setattr(_api, "run_graph",
                        lambda *args, **kwargs: {
                            "status": "error",
                            "run": {"errors": ["阶段 transcribe: remote worker failed"]},
                        })
    monkeypatch.setattr(_api, "settle_cost", lambda _run_id: None)
    asyncio.run(_api._drive(run_id, "fixture", "", False, False, 60))
    assert _api._TASKS[run_id]["errors"] == ["阶段 transcribe: remote worker failed"]
    _api._TASKS.pop(run_id, None)


def test_monthly_cost_details_include_only_owner_and_keep_deleted_bills(client):
    put_identity('test', 'alice', 'uid_alice')
    rec = _seed_run('monthly-bill', 'uid_alice', status='done')
    rec.update(created=datetime.now().isoformat(), cost_usd=0.5,
               usage={'llm_in_tok': 1000, 'llm_out_tok': 500, 'gpu_s': 180}, deleted=True)
    with runs._runs_file_lock():
        runs._runs_merge(rec)
    _seed_run('someone-elses-bill', 'uid_bob')
    body = client.get('/cost', headers={'X-Test-Auth': 'alice'}).json()
    assert [r['run_id'] for r in body['runs']] == ['monthly-bill']
    assert body['runs'][0]['deleted'] is True and body['runs'][0]['usage']['gpu_s'] == 180
    assert body['spend_usd'] == sum(r['cost_usd'] or 0 for r in body['runs'])


def test_partial_without_notes_has_quality_diagnostic_and_is_not_a_book(client):
    put_identity('test', 'alice', 'uid_alice')
    rec = _seed_run('short-transcript', 'uid_alice', status='partial')
    rec.update(partial_failures={'note': ['1']}, errors=[])
    rec['artifacts'] = {rec['entries'][0]['url']: {'transcript': '字' * 193}}
    with runs._runs_file_lock():
        runs._runs_merge(rec)
    headers = {'X-Test-Auth': 'alice'}
    item = client.get('/runs', headers=headers).json()['items'][0]
    assert item['done_entries'] == 0
    assert item['diagnostic']['stage'] == 'note'
    assert '过短' in item['diagnostic']['message']
    assert client.get('/quota', headers=headers).json()['stats']['books'] == 0


def test_cached_tokens_and_free_gpu_cost_are_settled_with_price_snapshot(monkeypatch, tmp_path):
    from video2md import llm, util
    monkeypatch.setattr(runs, 'RUNS_FILE', tmp_path / 'runs.json')
    monkeypatch.setattr(runs, '_RUNS_LOCKFILE', tmp_path / 'runs.lock')
    monkeypatch.setattr(runs, 'PRICE_LLM_IN_MTOK', 0.14)
    monkeypatch.setattr(runs, 'PRICE_LLM_OUT_MTOK', 0.28)
    monkeypatch.setattr(runs, 'PRICE_LLM_CACHED_IN_MTOK', 0.0028, raising=False)
    monkeypatch.setattr(runs, 'PRICE_GPU_MIN', 0.0)
    monkeypatch.setitem(runs.ENV, 'V2MD_GPU_PRICE_POLICY', 'free')
    runs._USAGE.clear()
    rid = 'cached-priced'
    with runs._runs_file_lock():
        runs._runs_merge({'run_id': rid, 'status': 'done', 'owner': 'alice', 'entries': [], 'artifacts': {}})
    util.bind_current_run(rid)
    try:
        llm._llm_observed('note', 'ok', 0, 1, {'usage': {
            'prompt_tokens': 1000, 'completion_tokens': 500,
            'prompt_tokens_details': {'cached_tokens': 900}}}, None, '', '', 'ok')
        runs.bump_usage(gpu_s=180)
        rec = runs.settle_cost(rid)
    finally:
        util.clear_current_run()
    assert rec['usage']['llm_cached_in_tok'] == 900
    assert rec['cost_usd'] == round((100 * .14 + 900 * .0028 + 500 * .28) / 1e6, 6)
    assert rec['cost_pricing']['gpu_policy'] == 'free'
    assert rec['cost_pricing']['llm_cached_in_mtok'] == 0.0028


def test_stage_proof_records_files_timestamps_and_local_gpu_attribution(tmp_path):
    video = tmp_path / 'short.mp4'
    frame = tmp_path / 'frame.jpg'
    video.write_bytes(b'video-fixture')
    frame.write_bytes(b'frame-fixture')
    art = {'video': str(video), 'duration': 41, 'segs': [{'start': 0, 'end': 2, 'text': 'hello'}],
           'candidates': [[1, 'frame.jpg']], 'clips': {'frame': 'clip.mp4'}, 'published': [[1, 'frame.jpg']],
           'selected': [{'name': 'frame.jpg', 't': 1}], 'selection_mode': 'heuristic_fallback'}
    assert runs._stage_proof('download', art, tmp_path, {}, 1.5, 0)['bytes'] == len(b'video-fixture')
    assert runs._stage_proof('transcribe', art, tmp_path, {}, 5, 5)['timestamped_segments'] is True
    art['segs'][0]['end'] = 999
    assert runs._stage_proof('transcribe', art, tmp_path, {}, 5, 5)['timestamped_segments'] is False
    frames = runs._stage_proof('frames', art, tmp_path, {}, 1, 0)
    assert frames['files_nonempty'] is True and frames['timestamps_valid'] is True
    frame.write_bytes(b'')
    assert runs._stage_proof('frames', art, tmp_path, {}, 1, 0)['files_nonempty'] is False
    clips = runs._stage_proof('clips', art, tmp_path, {}, 0.25, 0)
    assert clips['device'] == 'cpu' and clips['gpu_seconds'] == 0 and clips['duration_s'] == .25


def test_completed_live_artifact_submission_reuses_run_without_new_work(client, monkeypatch, tmp_path):
    put_identity('test', 'alice', 'uid_alice')
    import video2md.graph as graph
    note = tmp_path / 'existing.md'
    note.write_text('# Completed textbook')
    rec = _seed_run('already-billed', 'uid_alice', status='done')
    rec.update(url='https://example.test/completed', part_items='', cost_usd=.001,
               usage={'llm_in_tok':100, 'llm_out_tok':50, 'gpu_s':20})
    next(iter(rec['artifacts'].values()))['note'] = str(note)
    with runs._runs_file_lock():
        runs._runs_merge(rec)
    monkeypatch.setattr(graph, 'resolve_run', lambda *a, **k: (_ for _ in ()).throw(AssertionError('duplicate resolve')))
    before = runs._load_runs()
    for _ in range(2):
        result = client.post('/runs', json={'url':rec['url']}, headers={'X-Test-Auth':'alice'})
        assert result.status_code == 200, result.text
        assert result.json()['run_id'] == rec['run_id'] and result.json()['dedup'] is True
        assert result.json()['status'] == 'done'
    assert runs._load_runs() == before


@pytest.mark.parametrize('variant', ['force', 'other-owner', 'missing-artifact', 'different-partition'])
def test_completed_run_reuse_keeps_force_ownership_partition_and_artifact_guards(client, monkeypatch, tmp_path, variant):
    put_identity('test', 'alice', 'uid_alice')
    note = tmp_path / 'previous.md'
    note.write_text('# Existing textbook')
    rec = _seed_run('protected-completion', 'uid_bob' if variant == 'other-owner' else 'uid_alice')
    rec.update(url='https://example.test/reuse-guard', part_items='')
    next(iter(rec['artifacts'].values()))['note'] = str(tmp_path / 'missing.md') if variant == 'missing-artifact' else str(note)
    with runs._runs_file_lock():
        runs._runs_merge(rec)
    _stub_resolve(monkeypatch)
    body = {'url':rec['url'], 'resolve_only':True, 'force':variant == 'force'}
    if variant == 'different-partition':
        body['part_items'] = '2'
    result = client.post('/runs', json=body, headers={'X-Test-Auth':'alice'})
    assert result.status_code == 200
    assert result.json()['run_id'] != rec['run_id']


@pytest.mark.parametrize('error', [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ProxyError])
def test_oauth_transport_failure_is_safe_and_fresh_login_recovers(client, monkeypatch, error):
    """Production timeout replay: consume state, show retry, never replay code."""
    import urllib.parse as up

    import video2md.api as api
    from acctkit.providers import GoogleProvider

    env = {**_GENV, 'V2MD_GOOGLE_OAUTH_TIMEOUT_S': '9',
           'V2MD_GOOGLE_OAUTH_PROXY_URL': 'http://oauth-relay.test:18080'}
    monkeypatch.setattr(api, 'ENV', env)
    events, calls = [], []
    monkeypatch.setattr(api, '_req_log', lambda *args, **kw: events.append((args, kw)))
    monkeypatch.setattr(GoogleProvider, '_verify', lambda *a: {'sub': 'synthetic-user'})

    async def token(self, url, data=None, **kwargs):
        calls.append((url, self.timeout.read))
        if len(calls) == 1:
            raise error('synthetic-code synthetic-secret must never leak')
        return httpx.Response(200, json={'id_token': 'synthetic.id.token'})

    monkeypatch.setattr(httpx.AsyncClient, 'post', token)
    login = client.get('/auth/google/login?redirect=%2Ftasks&terms_accepted=1', follow_redirects=False)
    state = up.parse_qs(up.urlparse(login.headers['location']).query)['state'][0]
    callback = client.get('/auth/google/callback', params={'code': 'synthetic-code', 'state': state},
                          follow_redirects=False)
    location = up.unquote(callback.headers['location'])
    assert callback.status_code == 302
    assert 'auth_error=' in location and error.__name__ in location
    assert '重新登录' in location and 'redirect=%2Ftasks' in callback.headers['location']
    assert 'id_token=' not in location
    assert 'Max-Age=0' in callback.headers['set-cookie']
    assert state not in api._GOOGLE_OAUTH_STATES and state not in api._GOOGLE_OAUTH_REDIRECTS
    # A consumed authorization code must never be retried implicitly.
    client.get('/auth/google/callback', params={'code': 'synthetic-code', 'state': state},
               follow_redirects=False)
    assert len(calls) == 1 and calls[0][1] == 9
    for sensitive in ('synthetic-code', 'synthetic-secret', env['GOOGLE_CLIENT_SECRET'], state):
        assert sensitive not in repr(events) and sensitive not in location
    assert any(args[2] == 503 and fields['error_type'] == error.__name__ for args, fields in events)
    # Recovery uses a fresh login, CSRF cookie and code, with the original destination.
    login = client.get('/auth/google/login?redirect=%2Ftasks&terms_accepted=1', follow_redirects=False)
    fresh = up.parse_qs(up.urlparse(login.headers['location']).query)['state'][0]
    assert fresh != state
    recovered = client.get('/auth/google/callback', params={'code': 'fresh-code', 'state': fresh},
                           follow_redirects=False)
    assert recovered.headers['location'] == 'http://frontend.test/?redirect=%2Ftasks#id_token=synthetic.id.token'
    assert len(calls) == 2 and fresh not in api._GOOGLE_OAUTH_STATES


@pytest.mark.parametrize(('value', 'expected'), [('1', 3), ('999', 60), ('invalid', 15), ('9', 9)])
def test_oauth_exchange_timeout_is_bounded(value, expected, monkeypatch):
    import video2md.api as api
    monkeypatch.setattr(api, 'ENV', {'V2MD_GOOGLE_OAUTH_TIMEOUT_S': value})
    assert api._google_oauth_timeout_s() == expected


@pytest.mark.parametrize(('stage','error'), [
    ('clips', 'note 条目 1: clips 阶段无节段产物且已禁用本地回退 /private/upstream?token=secret'),
    ('clips', 'note 条目 2: 依赖阶段 clips 未成功，停止模型生成'),
    ('ocr', 'note 条目 1: 依赖阶段 ocr 未成功，停止模型生成'),
])
def test_dependency_diagnostic_exposes_action_without_provider_details(client, stage, error):
    put_identity('test', 'alice', 'uid_alice')
    _seed_run('dependency-diag', 'uid_alice', status='partial')
    records = runs._load_runs()
    records['dependency-diag'].update(errors=[error], partial_failures={stage: ['1'], 'note': ['1']})
    runs._save_runs(records)
    r = client.get('/runs/dependency-diag', headers={'X-Test-Auth': 'alice'})
    assert r.status_code == 200
    diagnostic = r.json()['diagnostic']
    assert diagnostic['stage'] == stage
    assert '补跑' in diagnostic['message']
    assert all(value not in diagnostic['message'] for value in ('/private', 'token=', 'secret'))

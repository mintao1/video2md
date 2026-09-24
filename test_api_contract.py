"""HTTP 契约测试（Phase 1）：鉴权（provider 无关）/ owner 越权隔离 / Pydantic 校验 /
错误信封 / OpenAPI 重开 / SSE 实时进度 / owner 线程到 resolve 去重。

全部离网：持久化文件指向临时目录；V2MD_AUTH=off（e2e/测试无 token 即可跑，用
X-Test-Auth 头注入不同身份断言多用户隔离）；POST 成功路径 monkeypatch resolve_run、
种子 run 永不 status=running（否则 lifespan 恢复会真驱动图联网）。
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="v2md-contract-")
# —— 必须在 import video2md.config 之前设好（config 在 import 时读 env 与算路径）——
os.environ.update({
    "V2MD_AUTH": "off",
    "V2MD_USERS_FILE": str(Path(_TMP) / "users.json"),
    "V2MD_RUNS_FILE": str(Path(_TMP) / "runs.json"),
    "V2MD_STATE_FILE": str(Path(_TMP) / "state.json"),
    "V2MD_LOG_DIR": str(Path(_TMP) / "logs"),
    "V2MD_UPLOADS_DIR": str(Path(_TMP) / "uploads"),
    "V2MD_SECRETS_FILE": str(Path(_TMP) / "nonexistent.env"),
    "OBSIDIAN_VAULT": str(Path(_TMP) / "vault"),
    "V2MD_REMOTE": "",
    "V2MD_CORS_ORIGINS": "",
    "V2MD_USER_SUBMIT_PER_MIN": "1000",  # 套件整体抬高滑窗，配额行为由专测 monkeypatch 收紧
    "V2MD_FAKE_LLM": "1",  # 兜底：任何意外真驱动也不烧 token
})

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from video2md import events, runs  # noqa: E402
from video2md.api import app  # noqa: E402
from video2md.auth import put_identity, resolve_principal  # noqa: E402（acctkit 接线层）
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
    for name in ("runs.json", "users.json", "state.json"):
        p = Path(_TMP) / name
        if p.exists():
            p.unlink()
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
    for path in ("/runs", "/runs/{run_id}", "/runs/{run_id}/note", "/runs/{run_id}/events"):
        assert path in spec["paths"], path
    assert client.get("/docs").status_code == 200


# ---------------------------------------------------------------- 运维探针不鉴权

def test_operational_probes_no_auth(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/queue").json().get("queue_len") == 0
    assert client.get("/preflight").status_code == 200


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


# ---------------------------------------------------------------- /auth/device/*（设备流桥）

def test_auth_device_code_requires_config(client, monkeypatch):
    import video2md.api as _api
    monkeypatch.setattr(_api, "ENV", {})  # 未配 client 凭据 → 500（服务端配置问题）
    assert client.post("/auth/device/code").status_code == 500


def test_auth_device_code_proxies_google(client, monkeypatch):
    import httpx

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
    from acctkit import providers as _p

    import video2md.api as _api
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

    r = client.post("/uploads", files={"file": ("demo.mp4", b"hello" * 100, "video/mp4")})
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
    sneak = client.post("/uploads", files={"file": ("../..%2Fc.mp4", b"ab", "video/mp4")})
    assert sneak.status_code == 200
    assert "/" not in Path(sneak.json()["name"]).name and sneak.json()["name"].endswith(".mp4")

    # 单文件上限 → 413，且半截文件已删
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_api, "UPLOAD_MAX_GB", 0.000001)  # ≈1KB
        big = client.post("/uploads", files={"file": ("big.mp4", b"x" * 4096, "video/mp4")})
    assert big.status_code == 413
    assert not list((up / dev).glob("big.mp4"))

    # 每用户配额 → 413，且刚传的文件回滚删除
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_api, "UPLOAD_QUOTA_GB", 0.000001)
        quota = client.post("/uploads", files={"file": ("q.mp4", b"x" * 2048, "video/mp4")})
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
    import video2md.graph as graph_mod
    import video2md.api as _api

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
    assert f"is_alive:dupx" in fake.calls
    assert "dupx" not in _api._TASKS  # 关键：不得重新入队驱动（他进程正跑）


def test_tasks_pg_dead_dup_requeues(client, monkeypatch):
    """DB 判活=False（崩溃残档）→ 既有重驱路径不变。"""
    import video2md.graph as graph_mod
    import video2md.api as _api

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

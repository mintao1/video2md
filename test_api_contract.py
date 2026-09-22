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
    "V2MD_SECRETS_FILE": str(Path(_TMP) / "nonexistent.env"),
    "OBSIDIAN_VAULT": str(Path(_TMP) / "vault"),
    "V2MD_REMOTE": "",
    "V2MD_CORS_ORIGINS": "",
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


def run_all() -> None:  # `python3 test_api_contract.py` 兜底（无 pytest 时）
    import sys
    sys.exit(pytest.main([__file__, "-q"]))


if __name__ == "__main__":
    run_all()

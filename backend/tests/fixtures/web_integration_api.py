"""Loopback-only HTTP fixture for the web integration suite.

Real API, JSON persistence and cancellation; replace only external identity verification.
No lifespan: seeded active tasks must never launch video/GPU/LLM work.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.update({
    "V2MD_SECRETS_FILE": str(root / "no-secrets.env"),
    "V2MD_ENV": "development", "V2MD_AUTH": "off", "V2MD_AUTH_PROVIDERS": "off",
    "V2MD_RUNS_STORE": "json", "V2MD_STATE_STORE": "json", "V2MD_USERS_STORE": "json",
    "V2MD_TASKS_STORE": "json", "V2MD_PREFS_STORE": "json", "V2MD_ARTIFACT_MODE": "vault",
    "V2MD_RUNS_FILE": str(root / "runs.json"), "V2MD_STATE_FILE": str(root / "state.json"),
    "V2MD_USERS_FILE": str(root / "users.json"), "V2MD_PREFS_FILE": str(root / "prefs.json"),
    "V2MD_LLM_SETTINGS_FILE": str(root / "llm-settings.json"),
    "V2MD_LOG_DIR": str(root / "logs"), "V2MD_UPLOADS_DIR": str(root / "uploads"),
    "OBSIDIAN_VAULT": str(root / "vault"), "V2MD_REMOTE": "off", "V2MD_OCR_REMOTE": "off",
    "V2MD_CLIPS_REMOTE": "off", "V2MD_FAKE_LLM": "1", "V2MD_EVAL": "off",
    # The live LLM-settings endpoint now encrypts credentials in every mode.
    # Keep the fixture deterministic while exercising the same encryption path;
    # this is test-only material and never represents a production secret.
    "V2MD_LLM_SETTINGS_KEY": "hex:" + "11" * 32,
    "V2MD_LOCAL_PATHS": "deny", "V2MD_BUDGET_MONTH_USD": "0",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "", "LANGFUSE_PK": "", "LANGFUSE_SK": "",
    "TMPDIR": str(root),
    # Browser-level integration tests run from the local Vite origin. Keep the
    # fixture usable for both hostnames used by the dev server; callers can
    # still override this explicitly for a stricter CORS test.
    "V2MD_CORS_ORIGINS": os.environ.get(
        "V2MD_TEST_CORS_ORIGIN",
        "http://localhost:5173,http://127.0.0.1:5173",
    ),
})

import uvicorn  # noqa: E402
from fastapi import HTTPException, Request  # noqa: E402

from acctkit.models import Identity  # noqa: E402
from video2md import api, runs  # noqa: E402
from video2md.auth import current_identity  # noqa: E402


async def identity(request: Request) -> Identity:
    token = request.headers.get("authorization", "")
    if token not in ("Bearer integration-alice", "Bearer integration-bob"):
        raise HTTPException(401, "invalid integration credential")
    uid = token.removeprefix("Bearer integration-")
    return Identity(uid=uid, provider="integration", external_id=uid)


api.app.dependency_overrides[current_identity] = identity

# Only this isolated fixture app exposes these endpoints. Capture no credentials.
_range_audit = []


@api.app.middleware("http")
async def range_media_audit(request, call_next):
    response = await call_next(request)
    if request.url.path.endswith("/media/range-demo.mp4"):
        _range_audit.append({"range": request.headers.get("range"),
                             "if_range": request.headers.get("if-range"),
                             "authenticated": bool(request.headers.get("authorization")),
                             "status": response.status_code})
    return response


@api.app.post("/__tests/seed-range-media")
def seed_range_media():
    from video2md.config import OUT_DIR
    body = bytes(i % 251 for i in range(150000))
    (OUT_DIR / "attachments/range-demo.mp4").write_bytes(body)
    records = runs._load_runs()
    next(iter(records["finished"]["artifacts"].values()))["md"] += "\n![[attachments/range-demo.mp4]]"
    runs._save_runs(records)
    _range_audit.clear()
    return {"bytes": len(body)}


@api.app.post("/__tests/range-audit")
def range_audit():
    return {"requests": _range_audit}


@api.app.post("/__tests/reset")
async def reset():
    _range_audit.clear()
    api.BUDGET_MONTH_USD = 0.0
    for task in list(api._DRIVES.values()):
        task.cancel()
    if api._DRIVES:
        await asyncio.gather(*list(api._DRIVES.values()), return_exceptions=True)
    api._DRIVES.clear()
    api._SEMA = asyncio.Semaphore(0)  # 不启动实际视频处理，只测试控制面。
    api._TASKS.clear()
    runs._CANCEL.clear()
    for name in ("prefs.json",):
        (root / name).unlink(missing_ok=True)
    records = {}
    for rid, owner, status, total, completed in (
        ("active", "alice", "running", 100, 99),
        ("queued", "alice", "queued", 1, 0),
        ("finished", "alice", "done", 1, 1),
        ("other", "bob", "queued", 1, 0),
    ):
        entries = [{"url": f"https://example.test/{rid}/{i}", "playlist_index": i + 1}
                   for i in range(total)]
        artifacts = {e["url"]: ({"note": str(root / f"{rid}-{i}.md"),
                                "title": f"Lesson {i + 1}", "md": "# Integration lesson"}
                               if i < completed else {}) for i, e in enumerate(entries)}
        records[rid] = {"run_id": rid, "url": f"https://example.test/{rid}", "part_items": "0,1,2",
                        "owner": owner, "status": status, "total": total,
                        "created": "2026-01-01T00:00:00", "entries": entries,
                        "artifacts": artifacts, "stages": {}}
        if status in ("running", "queued"):
            api._TASKS[rid] = {"status": status, "url": records[rid]["url"], "owner": owner}
    records['finished'].update(created=__import__('datetime').datetime.now().isoformat(),
        usage={'llm_in_tok': 1000, 'llm_out_tok': 500, 'gpu_s': 180}, cost_usd=0.00028,
        cost_pricing={'gpu_policy': 'free', 'gpu_min': 0, 'currency': 'USD'})
    runs._save_runs(records)
    import shutil

    from PIL import Image, ImageDraw

    from video2md.config import OUT_DIR
    directory = OUT_DIR / "attachments"
    directory.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (640, 240), "#ede9fe")
    ImageDraw.Draw(image).text((35, 90), "VIDEO2MD MEDIA TEST", fill="#4c1d95", font_size=36)
    image.save(directory / "demo.png")
    note = next(iter(records["finished"]["artifacts"].values()))
    note["md"] += "\n\n![[attachments/demo.png]]"
    if os.environ.get("V2MD_TEST_VIDEO"):
        shutil.copyfile(os.environ["V2MD_TEST_VIDEO"], directory / "demo.mp4")
        note["md"] += "\n\n![[attachments/demo.mp4]]"
    runs._save_runs(records)
    return {"ok": True}


@api.app.post("/__tests/complete")
def complete():
    records = runs._load_runs()
    record = records["active"]
    record["status"] = "done"
    record["artifacts"][record["entries"][-1]["url"]] = {
        "note": str(root / "last.md"), "title": "Last lesson", "md": "# Last lesson"}
    runs._save_runs(records)
    api._TASKS["active"]["status"] = "done"
    return {"ok": True}



@api.app.post('/__tests/model-failure')
async def model_failure():
    records = runs._load_runs()
    rec = records['finished']
    rec.update(status='error', errors=['note LLM: HTTP 403 sk-private-upstream-secret'],
               usage={'llm_in_tok': 0, 'llm_out_tok': 0, 'gpu_s': 204.669}, cost_usd=0)
    runs._save_runs(records)
    return {'ok': True}


@api.app.post('/__tests/quality-partial')
async def quality_partial():
    records = runs._load_runs()
    rec = records['finished']
    rec.update(status='partial', errors=[], partial_failures={'note': ['1']})
    rec['artifacts'] = {rec['entries'][0]['url']: {'transcript': '字' * 193}}
    runs._save_runs(records)
    return {'ok': True}


@api.app.post('/__tests/budget')
async def set_test_budget(request: Request):
    # Loopback fixture only: exercise the actual admission path, never a stubbed
    # POST /runs response. reset() and test finally blocks restore this value.
    body = await request.json()
    api.BUDGET_MONTH_USD = 0.000001 if body.get('enabled') else 0.0
    return {'budget_usd': api.BUDGET_MONTH_USD}


sock = socket.socket()
sock.bind(("127.0.0.1", int(os.environ.get("V2MD_TEST_PORT", "0"))))
print(json.dumps({"base_url": f"http://127.0.0.1:{sock.getsockname()[1]}"}), flush=True)
server = uvicorn.Server(uvicorn.Config(api.app, lifespan="off", log_level="error"))
server.run(sockets=[sock])

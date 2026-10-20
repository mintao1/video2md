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
    "V2MD_LOG_DIR": str(root / "logs"), "V2MD_UPLOADS_DIR": str(root / "uploads"),
    "OBSIDIAN_VAULT": str(root / "vault"), "V2MD_REMOTE": "off", "V2MD_OCR_REMOTE": "off",
    "V2MD_CLIPS_REMOTE": "off", "V2MD_FAKE_LLM": "1", "V2MD_EVAL": "off",
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


@api.app.post("/__tests/reset")
async def reset():
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
        records[rid] = {"run_id": rid, "url": f"https://example.test/{rid}",
                        "owner": owner, "status": status, "total": total,
                        "created": "2026-01-01T00:00:00", "entries": entries,
                        "artifacts": artifacts, "stages": {}}
        if status in ("running", "queued"):
            api._TASKS[rid] = {"status": status, "url": records[rid]["url"], "owner": owner}
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


sock = socket.socket()
sock.bind(("127.0.0.1", int(os.environ.get("V2MD_TEST_PORT", "0"))))
print(json.dumps({"base_url": f"http://127.0.0.1:{sock.getsockname()[1]}"}), flush=True)
server = uvicorn.Server(uvicorn.Config(api.app, lifespan="off", log_level="error"))
server.run(sockets=[sock])

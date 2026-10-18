#!/usr/bin/env python3
"""Offline Colab process boundary for tests. Never contacts Google."""
import io
import json
import os
import re
import shutil
import sys
import tarfile
from pathlib import Path

root = Path(os.environ["FAKE_COLAB_ROOT"])
root.mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]
command = args[0]
active = root / "active"

def remote(name):
    return root / Path(name).name

if command == "sessions":
    print(f"[{active.read_text()}] GPU T4" if active.exists() else "No active sessions")
elif command == "new":
    if os.environ.get("FAKE_COLAB_NO_GPU") and "--gpu" in args:
        print("503 Service Unavailable", file=sys.stderr)
        sys.exit(1)
    active.write_text(args[args.index("-s") + 1])
elif command == "stop":
    active.unlink(missing_ok=True)
elif command == "upload":
    shutil.copyfile(args[-2], remote(args[-1]))
elif command == "download":
    if not remote(args[-2]).exists():
        sys.exit(1)
    shutil.copyfile(remote(args[-2]), args[-1])
elif command == "exec":
    if os.environ.get("FAKE_COLAB_FAIL_EXEC"):
        print("remote worker failed", file=sys.stderr)
        sys.exit(1)
    if "-f" not in args:
        source = sys.stdin.read()
        for name in re.findall(r"'/content/([^']+)'", source):
            remote(name).unlink(missing_ok=True)
        print("DEPS-OK")
    elif "clip" in Path(args[args.index("-f") + 1]).name:
        specs = json.loads(remote("clip_job.json").read_text())["clips"]
        with tarfile.open(remote("clips.tar"), "w") as archive:
            for spec in specs:
                data = b"offline-clip-fixture"
                entry = tarfile.TarInfo(spec["out"])
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
        remote("clip_status.json").write_text(json.dumps({"state": "done", "ok": len(specs)}))
    else:
        if "RapidOCR" in remote("v2md_worker.py").read_text():
            with tarfile.open(remote("frames.tar")) as archive:
                payload = {"results": [{"name": name, "text": "CLI OCR", "conf": 0.99}
                                       for name in archive.getnames()]}
        else:
            payload = {"model": "tiny", "duration": 1, "segments": [
                {"start": 0, "end": 1, "text": "CLI speech", "avg_logprob": -0.1}]}
        remote("out.json").write_text(json.dumps(payload))
        remote("status.json").write_text('{"state":"done"}')
elif command != "install":
    raise SystemExit(f"unknown fixture command {command}")

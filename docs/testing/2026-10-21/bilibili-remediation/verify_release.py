"""Read-only verification of the deployed files, readiness and anonymous access."""

import argparse
import json
import subprocess
from pathlib import Path

out = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--preflight",
    action="store_true",
    help="Read-only idle/health/auth check before rollout; no deployment",
)
args = parser.parse_args()
manifest = json.loads((out / "release-manifest-final.json").read_text())
paths = {
    p: "/app/" + p.removeprefix("backend/")
    if p.startswith("backend/")
    else "/app/web-dist/" + p.removeprefix("frontend/web/dist/")
    for p in manifest["files"]
}
program = """
import json,hashlib,httpx
from pathlib import Path
from video2md.api import _load_runs
paths = PATHS
result={'hashes':{p:hashlib.sha256(Path(f).read_bytes()).hexdigest() for p,f in paths.items()}}
result['active_runs']=[r['run_id'] for r in _load_runs().values() if r.get('status') not in ('done','partial','error','cancelled')]
with httpx.Client(trust_env=False,timeout=10) as client:
 result['health_status']=client.get('http://127.0.0.1:8790/healthz').status_code
 result['anonymous_media_status']=client.get('http://127.0.0.1:8790/runs/be188f78/media/p002-video-c08-0007.mp4',headers={'Range':'bytes=0-3'}).status_code
print(json.dumps(result))
""".replace("PATHS", repr({} if args.preflight else paths))
result = subprocess.run(
    [
        "ssh",
        "-o",
        "BatchMode=yes",
        "lcode-4c",
        "kubectl -n video2md exec -i deploy/video2md-api -c api -- python -",
    ],
    input=program,
    capture_output=True,
    text=True,
    check=True,
)
data = json.loads(result.stdout.strip().splitlines()[-1])
assert data["active_runs"] == [], (
    "active runs: defer rollout and retry after tasks finish"
)
assert data["health_status"] == 200 and data["anonymous_media_status"] == 401
if args.preflight:
    print(
        "PASS: pre-rollout read-only probe: idle, health 200, anonymous media 401; no deployment performed"
    )
    raise SystemExit(0)
assert data["hashes"] == manifest["files"]
result = subprocess.run(
    [
        "ssh",
        "-o",
        "BatchMode=yes",
        "lcode-4c",
        "kubectl -n video2md get pods -l app.kubernetes.io/name=video2md,app.kubernetes.io/component=api -o json",
    ],
    capture_output=True,
    text=True,
    check=True,
)
status = [
    {
        "pod": p["metadata"]["name"],
        "phase": p["status"]["phase"],
        "containers": [
            {k: c[k] for k in ("name", "image", "imageID", "ready", "restartCount")}
            for c in p["status"]["containerStatuses"]
        ],
    }
    for p in json.loads(result.stdout)["items"]
]
assert len(status) == 1 and status[0]["phase"] == "Running"
assert len(status[0]["containers"]) == 1 and all(
    c["name"] == "api"
    and c["ready"] is True
    and c["restartCount"] == 0
    and c["image"] in {manifest["image"], "docker.io/library/" + manifest["image"]}
    and c["imageID"]
    for c in status[0]["containers"]
)
(out / "deployment-status.json").write_text(json.dumps(status, indent=2) + "\n")
verification = {
    "image": manifest["image"],
    "all_release_file_hashes_match": True,
    "files_count": len(paths),
    "manifest_sha256": manifest["manifest_sha256"],
    "health_status": data["health_status"],
    "anonymous_media_status": data["anonymous_media_status"],
    "active_runs": data["active_runs"],
    "containers": status[0]["containers"],
}
(out / "release-verification.json").write_text(
    json.dumps(verification, indent=2) + "\n"
)
print("PASS: release files match, pod ready, health 200, anonymous ranged media 401")

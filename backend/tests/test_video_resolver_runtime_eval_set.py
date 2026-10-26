import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "docs/evals/video-resolver-runtime-eval-set.jsonl"
CHECKER = ROOT / "scripts/check_video_resolver_runtime.py"


@pytest.fixture(autouse=True)
def isolated_runtime_imports(monkeypatch, tmp_path):
    """Keep application imports after collection and outside local persisted data."""
    for key, name in {
        "V2PDF_RUNS_FILE": "runs.json", "V2PDF_STATE_FILE": "state.json",
        "V2PDF_USERS_FILE": "users.json", "V2PDF_PREFS_FILE": "prefs.json",
        "V2PDF_SECRETS_FILE": "nonexistent.env", "V2PDF_LOG_DIR": "logs",
        "V2PDF_UPLOADS_DIR": "uploads", "OBSIDIAN_VAULT": "vault",
    }.items():
        monkeypatch.setenv(key, str(tmp_path / name))
    monkeypatch.setenv("V2PDF_AUTH", "off")
    monkeypatch.setenv("V2PDF_FAKE_LLM", "1")


def _cases() -> list[dict]:
    return [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]


def test_api_runtime_path_finds_venv_yt_dlp(monkeypatch):
    from video2pdf import api

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    configured = api.configure_subprocess_path()
    assert configured.split(os.pathsep)[0] == str(Path(sys.executable).parent)
    assert shutil.which("yt-dlp") == str(Path(sys.executable).parent / "yt-dlp")


def test_explicit_direct_host_strips_ambient_proxy(monkeypatch):
    from video2pdf import resolve

    monkeypatch.setenv("V2PDF_DIRECT_HOSTS", "ferosse.tech,.internal.example")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:17898")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:17898")
    direct = resolve._subprocess_env(
        "https://ferosse.tech/test-assets/model-billing-smoke.mp4"
    )
    assert direct is not None
    assert "HTTP_PROXY" not in direct
    assert "HTTPS_PROXY" not in direct
    assert "http_proxy" not in direct
    assert "https_proxy" not in direct
    assert resolve._subprocess_env("https://youtube.com/watch?v=example") is None


def test_direct_host_suffix_does_not_match_lookalike(monkeypatch):
    from video2pdf import resolve

    monkeypatch.setenv("V2PDF_DIRECT_HOSTS", ".ferosse.tech")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:17898")
    assert "HTTP_PROXY" not in resolve._subprocess_env("https://cdn.ferosse.tech/v.mp4")
    assert resolve._subprocess_env("https://ferosse.tech.evil.example/v.mp4") is None


def test_download_selector_accepts_single_format_mp4():
    from video2pdf import download

    assert download._FORMAT_SELECTOR.endswith("/best")


def test_eval_set_and_checker_require_all_layers(tmp_path: Path):
    cases = _cases()
    assert {case["id"] for case in cases} == {
        "vrr-001", "vrr-002", "vrr-003", "vrr-004", "vrr-005", "vrr-006", "vrr-007",
        "vrr-008", "vrr-009"
    }
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"results": [
        {"id": case["id"], "passed": True, "observed": case["pass"]} for case in cases
    ]}))
    ok = subprocess.run([sys.executable, str(CHECKER), "--evidence", str(evidence)],
                        cwd=ROOT, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr

    bad = json.loads(evidence.read_text())
    bad["results"][0] = {"id": "vrr-001", "passed": False,
                         "observed": "yt-dlp installed but missing from PATH"}
    evidence.write_text(json.dumps(bad))
    rejected = subprocess.run([sys.executable, str(CHECKER), "--evidence", str(evidence)],
                              cwd=ROOT, capture_output=True, text=True)
    assert rejected.returncode != 0
    assert "vrr-001" in (rejected.stdout + rejected.stderr)

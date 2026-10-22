"""Catch the actual eager-import regression without touching developer records."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_collecting_bilibili_tests_does_not_import_application_configuration(tmp_path):
    program = """
import runpy,sys
runpy.run_path('tests/test_bilibili_public_fallback.py')
assert 'video2md.config' not in sys.modules, 'eager config import captures development storage'
"""
    env = {**os.environ, "V2MD_SECRETS_FILE": str(tmp_path / "missing.env")}
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=ROOT / "backend", env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr[-1000:]


def test_fallback_then_api_suite_preserves_external_sentinel_records(tmp_path):
    sentinel = tmp_path / "development-records"
    sentinel.mkdir()
    keys = {
        "V2MD_RUNS_FILE": "runs.json",
        "V2MD_STATE_FILE": "state.json",
        "V2MD_USERS_FILE": "users.json",
        "V2MD_PREFS_FILE": "prefs.json",
    }
    paths = {key: sentinel / name for key, name in keys.items()}
    for path in paths.values():
        path.write_text('{"preserved-development-record":"never-overwrite"}')
    before = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}
    env = {
        **os.environ,
        **{key: str(path) for key, path in paths.items()},
        "V2MD_ENV": "development",
        "V2MD_AUTH": "off",
        "V2MD_FAKE_LLM": "1",
        "V2MD_SECRETS_FILE": str(tmp_path / "missing.env"),
        "V2MD_PG_DSN": "",
        "V2MD_RUNS_STORE": "json",
        "V2MD_STATE_STORE": "json",
        "V2MD_USERS_STORE": "json",
        "V2MD_EVENTS": "local",
        "V2MD_CHECKPOINTER": "sqlite",
        "OBSIDIAN_VAULT": str(sentinel / "vault"),
        "V2MD_LOG_DIR": str(sentinel / "logs"),
        "V2MD_UPLOADS_DIR": str(sentinel / "uploads"),
        "V2MD_REMOTE": "off",
    }
    # A regressed import would bind these sandbox sentinel paths before the API
    # suite sets its own temp paths. It can damage only the sandbox, never real data.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_bilibili_public_fallback.py",
            "tests/test_api_contract.py",
            "-k",
            "public or webpage or part or web_media or usage_accumulate",
        ],
        cwd=ROOT / "backend",
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    after = {key: hashlib.sha256(path.read_bytes()).hexdigest() for key, path in paths.items()}
    assert after == before, "joint test collection mutated external development records"
    assert result.returncode == 0, result.stdout[-1500:] + result.stderr[-1000:]
    assert not (sentinel / "vault").exists()

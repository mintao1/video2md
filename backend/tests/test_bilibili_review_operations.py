"""Exercise the real ops scripts with sandbox files and fake process boundaries."""

from __future__ import annotations

import importlib.util
import json
import plistlib
import runpy
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts/install_bilibili_home_egress.py"
VERIFY = ROOT / "docs/testing/2026-10-21/bilibili-remediation/verify_release.py"
SPEC = importlib.util.spec_from_file_location("home_installer", INSTALLER)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


@pytest.fixture
def service_sandbox(monkeypatch, tmp_path):
    calls, loaded = [], set()
    binary = Path("/Applications/Clash Verge.app/Contents/MacOS/verge-mihomo")
    original = Path.is_file
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(Path, "is_file", lambda self: self == binary or original(self))
    (tmp_path / "Library/LaunchAgents").mkdir(parents=True)
    state = {"listening": True}

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == str(binary):
            assert cmd[1] == "-t"
            return SimpleNamespace(returncode=0)
        assert cmd[0] == "launchctl", "sandbox forbids actual SSH or proxy startup"
        if cmd[1] == "print":
            return SimpleNamespace(returncode=0 if cmd[2] in loaded else 1)
        assert cmd[1] == "bootstrap"
        label = plistlib.loads(Path(cmd[3]).read_bytes())["Label"]
        loaded.add(cmd[2] + "/" + label)
        return SimpleNamespace(returncode=0)

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def settimeout(self, value):
            assert value == 2

        def connect_ex(self, target):
            assert target == ("127.0.0.1", 17895)
            return 0 if state["listening"] else 1

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(socket, "socket", FakeSocket)
    return tmp_path, calls, state


def test_home_route_is_loopback_only_bilibili_only_and_default_reject():
    config = yaml.safe_load((ROOT / "deploy/bilibili-home-proxy.yaml").read_text())
    assert config["mixed-port"] == 17895 and config["bind-address"] == "127.0.0.1"
    assert config["allow-lan"] is False and config["external-controller"] == ""
    assert config["rules"] == [
        f"DOMAIN-SUFFIX,{host},DIRECT"
        for host in ("bilibili.com", "bilivideo.com", "bilivideo.cn", "hdslb.com")
    ] + ["MATCH,REJECT"]


def test_installer_is_idempotent_and_ssh_keeps_host_verification(monkeypatch, service_sandbox):
    home, calls, state = service_sandbox
    monkeypatch.setattr(sys, "argv", [str(INSTALLER)])
    installer.main()
    files = list((home / "Library/LaunchAgents").glob("*.plist"))
    before = {str(f): f.read_bytes() for f in files}
    installer.main()
    assert {str(f): f.read_bytes() for f in files} == before
    assert len([c for c in calls if c[:2] == ["launchctl", "bootstrap"]]) == 2
    tunnel = next(plistlib.loads(f.read_bytes()) for f in files if "tunnel" in f.name)
    args = tunnel["ProgramArguments"]
    assert "127.0.0.1:17895:127.0.0.1:17895" in args
    assert "StrictHostKeyChecking=yes" in args and "BatchMode=yes" in args
    assert all("networksetup" not in c and "scutil" not in c for c in calls)


def test_conflicting_config_is_preserved_not_overwritten(monkeypatch, service_sandbox):
    home, calls, _ = service_sandbox
    config = home / ".config/video2md-bilibili-home/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("preserve existing operator config")
    monkeypatch.setattr(sys, "argv", [str(INSTALLER)])
    with pytest.raises(SystemExit):
        installer.main()
    assert config.read_text() == "preserve existing operator config" and calls == []


def test_readonly_check_missing_listener_fails_without_installing(monkeypatch, service_sandbox):
    home, calls, state = service_sandbox
    state["listening"] = False
    monkeypatch.setattr(sys, "argv", [str(INSTALLER), "--check"])
    with pytest.raises(SystemExit):
        installer.main()
    assert not (home / ".config/video2md-bilibili-home").exists()
    assert all(c[:2] == ["launchctl", "print"] for c in calls)


def test_kubernetes_keeps_single_writer_and_nonroot_api():
    spec = yaml.safe_load((ROOT / "deploy/k8s/base/deployment-api.yaml").read_text())["spec"]
    assert spec["replicas"] == 1 and spec["strategy"]["type"] == "Recreate"
    pod = spec["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsUser"] == 10001
    assert [c["name"] for c in pod["initContainers"]] == ["fix-volume-permissions", "migrate"]


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "active",
        "unknown_active",
        "wrong_image",
        "wrong_registry",
        "not_ready",
        "restart",
        "empty_containers",
        "hash",
        "anonymous",
        "health",
    ],
)
def test_release_verifier_rejects_bad_snapshot(monkeypatch, tmp_path, failure):
    target = tmp_path / "evidence"
    target.mkdir()
    script = target / "verify_release.py"
    shutil.copyfile(VERIFY, script)
    manifest = json.loads((VERIFY.parent / "release-manifest-final.json").read_text())
    (target / "release-manifest-final.json").write_text(json.dumps(manifest))
    probe = {
        "hashes": manifest["files"],
        "health_status": 200,
        "anonymous_media_status": 401,
        "active_runs": [],
    }
    container = {
        "name": "api",
        "image": manifest["image"],
        "imageID": "sha256:test",
        "ready": True,
        "restartCount": 0,
    }
    pods = {
        "items": [
            {
                "metadata": {"name": "test-pod"},
                "status": {"phase": "Running", "containerStatuses": [container]},
            }
        ]
    }
    if failure == "active":
        probe["active_runs"] = ["busy-run"]
    if failure == "unknown_active":
        del probe["active_runs"]
    if failure == "wrong_image":
        container["image"] = "video2md:retired"
    if failure == "wrong_registry":
        container["image"] = "untrusted.example/" + manifest["image"]
    if failure == "not_ready":
        container["ready"] = False
    if failure == "restart":
        container["restartCount"] = 1
    if failure == "empty_containers":
        pods["items"][0]["status"]["containerStatuses"] = []
    if failure == "hash":
        probe["hashes"] = {}
    if failure == "anonymous":
        probe["anonymous_media_status"] = 206
    if failure == "health":
        probe["health_status"] = 503
    responses = iter([probe, pods])

    def run(cmd, **kwargs):
        assert cmd[0] == "ssh"  # Replace process boundary; never execute it.
        return SimpleNamespace(stdout=json.dumps(next(responses)))

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", [str(script)])
    if failure:
        with pytest.raises((AssertionError, KeyError)):
            runpy.run_path(str(script), run_name="__main__")
        assert not (target / "release-verification.json").exists()
    else:
        runpy.run_path(str(script), run_name="__main__")
        assert json.loads((target / "release-verification.json").read_text())["image"] == manifest["image"]


def test_preflight_never_requires_not_yet_deployed_files_or_mutates_cluster(monkeypatch, tmp_path):
    script = tmp_path / "verify_release.py"
    shutil.copyfile(VERIFY, script)
    (tmp_path / "release-manifest-final.json").write_text('{"files":{"new-file":"new-hash"}}')
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        assert "paths = {}" in kwargs["input"]
        assert not any(word in cmd[-1] for word in ("set image", "rollout", "delete", "apply"))
        return SimpleNamespace(
            stdout=json.dumps(
                {"hashes": {}, "health_status": 200, "anonymous_media_status": 401, "active_runs": []}
            )
        )

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", [str(script), "--preflight"])
    with pytest.raises(SystemExit) as done:
        runpy.run_path(str(script), run_name="__main__")
    assert done.value.code == 0 and len(calls) == 1
    assert not (tmp_path / "release-verification.json").exists()


def test_preflight_actual_probe_blocks_cancelling_and_unknown_states(monkeypatch, tmp_path):
    script = tmp_path / "verify_release.py"
    shutil.copyfile(VERIFY, script)
    (tmp_path / "release-manifest-final.json").write_text('{"files":{}}')
    captured = []

    def run(cmd, **kwargs):
        captured.append(kwargs["input"])
        return SimpleNamespace(
            stdout=json.dumps(
                {"hashes": {}, "health_status": 200, "anonymous_media_status": 401, "active_runs": []}
            )
        )

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", [str(script), "--preflight"])
    with pytest.raises(SystemExit):
        runpy.run_path(str(script), run_name="__main__")
    fake_api = ModuleType("video2md.api")
    states = ("queued", "running", "cancelling", "unknown", None, "done", "partial", "error", "cancelled")
    fake_api._load_runs = lambda: {str(i): {"run_id": str(i), "status": s} for i, s in enumerate(states)}
    monkeypatch.setitem(sys.modules, "video2md.api", fake_api)

    class FakeHTTP:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            return SimpleNamespace(status_code=200 if url.endswith("/healthz") else 401)

    import httpx

    monkeypatch.setattr(httpx, "Client", FakeHTTP)
    namespace = {}
    exec(compile(captured[0], "<preflight probe>", "exec"), namespace)
    assert namespace["result"]["active_runs"] == ["0", "1", "2", "3", "4"]

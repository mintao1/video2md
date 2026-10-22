"""Run the actual image deployment entrypoint at fake SSH/filesystem seams."""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "docs/testing/2026-10-22/production-resolution/deploy_image.py"


@pytest.fixture
def deployment(monkeypatch, tmp_path):
    # The entrypoint is unchanged. Copying only redirects its HERE evidence output.
    script = tmp_path / "deploy_image.py"
    script.write_bytes(SCRIPT.read_bytes())
    private = Path("/Users/linmintao/.config/video2md-settlement")
    write, chmod = Path.write_text, Path.chmod

    def redirect(path):
        if path.is_relative_to(private):
            return tmp_path / path.name
        assert path.is_relative_to(tmp_path), "test attempted a real storage write"
        return path

    monkeypatch.setattr(Path, "write_text", lambda p, *a, **kw: write(redirect(p), *a, **kw))
    monkeypatch.setattr(Path, "chmod", lambda p, *a, **kw: chmod(redirect(p), *a, **kw))
    before = {
        "metadata": {"managedFields": [{"manager": "fixture"}], "annotations": {
            "kubectl.kubernetes.io/last-applied-configuration": json.dumps({"env": [
                {"name": "V2MD_ENV", "value": "production"},
                {"name": "V2MD_ENV", "value": "production"},
            ]}),
        }},
        "spec": {"replicas": 1, "template": {"spec": {
            "containers": [{"name": "api", "image": "video2md:old", "env": [
                {"name": "V2MD_ENV", "value": "production"},
                {"name": "SECRET", "valueFrom": {"secretKeyRef": {"name": "fixture", "key": "key"}}},
            ], "resources": {"limits": {"memory": "768Mi"}}}],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}],
            "initContainers": [{"name": "fix", "securityContext": {"capabilities": {"add": ["CHOWN"]}}}],
            "securityContext": {"runAsUser": 10001, "runAsGroup": 10001},
        }}},
        "status": {"readyReplicas": 1},
    }
    state = {"before": before, "calls": [], "mutation": None, "reject": "", "submitted": None}

    def remote(args, **kwargs):
        assert args[:4] == ["ssh", "-o", "BatchMode=yes", "lcode-4c"]
        command = args[-1]
        state["calls"].append(command)
        if command == "kubectl -n video2md get deployment video2md-api -o json":
            return SimpleNamespace(returncode=0, stdout=json.dumps(before), stderr="")
        if command == "kubectl apply --dry-run=server -f - -o json":
            desired = json.loads(kwargs["input"])
            state["submitted"] = desired
            actual = deepcopy(desired)
            if state["mutation"]:
                state["mutation"](actual)
            return SimpleNamespace(returncode=1 if state["reject"] == "dry-run" else 0,
                                   stdout=json.dumps(actual), stderr="private canary, do not publish")
        assert command == "kubectl apply -f - && kubectl -n video2md rollout status deployment/video2md-api --timeout=60s"
        return SimpleNamespace(returncode=1 if state["reject"] == "rollout" else 0,
                               stdout="fixture rollout", stderr="fixture rollout failed")

    monkeypatch.setattr(subprocess, "run", remote)
    monkeypatch.setattr(sys, "argv", [str(script), "video2md", "video2md-api", "api", "video2md:old", "video2md:new"])
    state["run"] = lambda: runpy.run_path(str(script), run_name="__main__")
    state["directory"] = tmp_path
    return state


@pytest.mark.parametrize("field", ["env", "volumes", "initContainers", "securityContext", "resources"])
def test_server_dry_run_rejects_every_unrelated_spec_change(deployment, field):
    def change(actual):
        spec = actual["spec"]["template"]["spec"]
        if field in ("env", "resources"):
            del spec["containers"][0][field]
        else:
            del spec[field]
    deployment["mutation"] = change
    with pytest.raises(AssertionError):
        deployment["run"]()
    assert len(deployment["calls"]) == 2  # read + server dry-run; no real apply
    assert not (deployment["directory"] / "video2md-api-dry-run.json").exists()


def test_image_only_plan_preserves_env_secret_volumes_init_and_live_metadata(deployment):
    original = deepcopy(deployment["before"])
    deployment["run"]()
    submitted = deepcopy(deployment["submitted"])
    submitted["spec"]["template"]["spec"]["containers"][0]["image"] = "video2md:old"
    assert submitted["spec"] == original["spec"]
    assert deployment["before"] == original
    assert "kubectl.kubernetes.io/last-applied-configuration" not in submitted["metadata"]["annotations"]
    assert "force" not in " ".join(deployment["calls"])
    assert len(deployment["calls"]) == 3


@pytest.mark.parametrize("phase", ["dry-run", "rollout"])
def test_failed_preflight_or_rollout_cannot_emit_success(deployment, phase):
    deployment["reject"] = phase
    with pytest.raises(RuntimeError, match="Deployment rejected"):
        deployment["run"]()
    assert len(deployment["calls"]) == (2 if phase == "dry-run" else 3)
    assert not (deployment["directory"] / "video2md-api-rollout.txt").exists()


def test_concurrent_image_change_rejected_before_any_write_or_apply(deployment):
    deployment["before"]["spec"]["template"]["spec"]["containers"][0]["image"] = "video2md:other"
    with pytest.raises(AssertionError):
        deployment["run"]()
    assert len(deployment["calls"]) == 1
    assert list(deployment["directory"].iterdir()) == [deployment["directory"] / "deploy_image.py"]

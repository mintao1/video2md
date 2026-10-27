"""Offline regression contract for the production 502 deployment failure chain.

This test deliberately inspects repository artifacts only.  It does not SSH to a
node, contact the production URL, or read deployment credentials.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/deploy.yml"
DISK_GUARD = ROOT / "scripts/node-build-guard.sh"
DATASET = ROOT / "docs/evals/production-deploy-resilience-eval-set.jsonl"
IDS = [f"pdr-{index:03d}" for index in range(1, 7)]


def _rows() -> list[dict]:
    return [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_deploy_resilience_eval_set_is_complete_and_redacted() -> None:
    rows = _rows()
    assert [row["id"] for row in rows] == IDS
    assert len({row["id"] for row in rows}) == len(IDS)
    for row in rows:
        assert row["scope"] == "production-deploy-resilience"
        assert row["input"] and row["expectedOutput"]
        assert row["counterexamples"]
        assert row["metadata"]["offline_can_close_production"] is False
        assert row["metadata"]["privacy"]
        serialized = json.dumps(row, ensure_ascii=False)
        assert "Bearer " not in serialized
        assert "live-secret" not in serialized
        assert "BEGIN OPENSSH PRIVATE KEY" not in serialized


def test_workflow_imports_rendered_images_before_apply_and_smokes_healthz() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    import_pos = source.find("docker save")
    apply_pos = source.find("kubectl apply -n")
    assert import_pos >= 0, "the node must import the locally built image into K3s containerd"
    assert apply_pos > import_pos, "K3s apply must happen only after image import"
    assert "awk '$1 == \"image:\" {print $2}'" in source
    assert "ctr images list -q" in source
    assert "all rendered images imported and verified" in source
    assert "steps.limits.outputs.min_free_gib" in source
    assert "path: ${{ inputs.product || 'video2md' }}" in source
    assert 'NS="${{ inputs.product || \'video2md\' }}"' in source
    assert "healthz" in source and "rollout status" in source


def test_workflow_blocks_deploy_on_node_disk_pressure_before_apply() -> None:
    source = WORKFLOW.read_text(encoding="utf-8")
    guard = DISK_GUARD.read_text(encoding="utf-8")
    combined = source + "\n" + guard
    apply_pos = source.find("kubectl apply -n")
    assert apply_pos >= 0
    assert "DISK_PRESSURE_GUARD" in guard
    assert "DiskPressure" in combined
    assert "disk-pressure" in combined
    assert "ephemeral-storage" in combined
    assert "MIN_FREE_GIB" in guard
    assert "source=kubelet-ephemeral-storage" in guard
    # The guard script is invoked before the workflow's apply step.  The
    # workflow's own error text also makes the fail-closed behavior explicit.
    first_guard = source.find("Guard node disk pressure before build")
    assert first_guard >= 0, "workflow must invoke the disk-pressure guard before building"
    assert first_guard < apply_pos, "disk pressure guard must run before apply"


def test_disk_guard_passes_and_fails_closed_on_disk_pressure(tmp_path: Path) -> None:
    """Exercise the shell guard with a fake kubectl boundary, without SSH or a node."""

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    sudo = fake_bin / "sudo"
    sudo.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *status.conditions*) printf '%s' \"${FAKE_PRESSURE:-False}\" ;;\n"
        "  *spec.taints*) : ;;\n"
        "  *--raw*) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    sudo.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "NODE_NAME": "node-a",
        "ROOT_PATH": "/",
        "MIN_FREE_GIB": "0",
    }

    healthy = subprocess.run(
        ["bash", str(DISK_GUARD)], env=env, capture_output=True, text=True, check=False
    )
    assert healthy.returncode == 0, healthy.stderr
    assert "DISK_PRESSURE_GUARD=PASS" in healthy.stdout

    pressured_env = {**env, "FAKE_PRESSURE": "True"}
    pressured = subprocess.run(
        ["bash", str(DISK_GUARD)], env=pressured_env, capture_output=True, text=True, check=False
    )
    assert pressured.returncode == 42
    assert "DISK_PRESSURE_GUARD=FAIL" in pressured.stderr


def test_eval_set_covers_the_observed_502_causal_chain() -> None:
    rows = {row["id"]: row for row in _rows()}
    chain = rows["pdr-004"]["expectedOutput"]["causal_chain"]
    assert chain == [
        "node_disk_pressure",
        "api_pod_unscheduled_or_evicted",
        "host_port_8790_unavailable",
        "nginx_returns_502",
    ]
    assert any(case["name"] == "healthy-api-returns-200" for case in rows["pdr-004"]["counterexamples"])

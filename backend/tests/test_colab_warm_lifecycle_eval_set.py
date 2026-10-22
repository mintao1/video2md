import json
import subprocess
import sys
from pathlib import Path

from gpu_plugins.v2md.providers import colab_machinery as machinery


ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "docs/evals/colab-warm-lifecycle-eval-set.jsonl"
CHECKER = ROOT / "scripts/check_colab_warm_lifecycle.py"


def _cases() -> list[dict]:
    return [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]


def test_lifecycle_policy_has_bounded_warm_reuse_and_inflight_protection():
    assert machinery.session_action(inflight=True, queued=False, lease_fresh=False,
                                    idle_seconds=999) == "keep"
    assert machinery.session_action(inflight=False, queued=True, lease_fresh=False,
                                    idle_seconds=999) == "keep"
    assert machinery.session_action(inflight=False, queued=False, lease_fresh=True,
                                    idle_seconds=999) == "keep"
    assert machinery.session_action(inflight=False, queued=False, lease_fresh=False,
                                    idle_seconds=29) == "keep"
    assert machinery.session_action(inflight=False, queued=False, lease_fresh=False,
                                    idle_seconds=30) == "stop"


def test_eval_set_and_checker_reject_unbounded_or_immediate_shutdown(tmp_path: Path):
    cases = _cases()
    assert {case["id"] for case in cases} == {
        "cwl-001", "cwl-002", "cwl-003", "cwl-004", "cwl-005", "cwl-006",
    }
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"results": [
        {"id": case["id"], "passed": True, "observed": case["pass"]} for case in cases
    ]}))
    ok = subprocess.run([sys.executable, str(CHECKER), "--evidence", str(evidence)],
                        cwd=ROOT, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr

    bad = json.loads(evidence.read_text())
    bad["results"][1]["observed"] = "keep Colab running indefinitely for future jobs"
    evidence.write_text(json.dumps(bad))
    rejected = subprocess.run([sys.executable, str(CHECKER), "--evidence", str(evidence)],
                              cwd=ROOT, capture_output=True, text=True)
    assert rejected.returncode != 0
    assert "forbidden" in (rejected.stdout + rejected.stderr).lower()

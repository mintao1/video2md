import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "docs/evals/colab-source-routing-eval-set.jsonl"
CHECKER = ROOT / "scripts/check_colab_source_routing.py"
OBSOLETE = (
    ROOT / "docs/evals/production-2c-migration-eval-set.jsonl",
    ROOT / "docs/evals/production-2c-compose-eval-set.jsonl",
    ROOT / "docs/evals/production-2c-compose-results-2026-10-20.md",
)


def _cases() -> list[dict]:
    return [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]


def test_eval_set_covers_fast_path_and_forbidden_routes():
    cases = _cases()
    assert {case["id"] for case in cases} == {
        "csr-001", "csr-002", "csr-003", "csr-004", "csr-005", "csr-006", "csr-007",
    }
    assert all(case["forbidden"] for case in cases)
    assert not any(path.exists() for path in OBSOLETE), \
        "lcode-cn 主生产迁移/回滚评测属于已否决路线，必须删除"


def test_checker_rejects_old_route_and_accepts_complete_evidence(tmp_path: Path):
    cases = _cases()
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({"results": [
        {"id": case["id"], "passed": True, "observed": case["pass"]} for case in cases
    ]}, ensure_ascii=False))
    ok = subprocess.run([sys.executable, str(CHECKER), "--evidence", str(evidence)],
                        cwd=ROOT, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr

    bad = json.loads(evidence.read_text())
    bad["results"][0]["observed"] = "lcode-cn downloads full video then uploads it to Colab"
    evidence.write_text(json.dumps(bad, ensure_ascii=False))
    rejected = subprocess.run([sys.executable, str(CHECKER), "--evidence", str(evidence)],
                              cwd=ROOT, capture_output=True, text=True)
    assert rejected.returncode != 0
    assert "forbidden route" in (rejected.stdout + rejected.stderr).lower()

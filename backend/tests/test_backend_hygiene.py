"""Fast regression checks for backend evaluation/execution hygiene."""
from pathlib import Path
import subprocess
import sys

from video2pdf import evals, runs, util


def test_eval_targets_skip_legacy_scalar_entries(tmp_path):
    note = tmp_path / "note.md"
    transcript = tmp_path / "transcript.txt"
    note.write_text("# note", encoding="utf-8")
    transcript.write_text("source", encoding="utf-8")
    state = {
        "_migration_counter": 3,
        "ok": {"status": "completed", "note": str(note), "transcript": str(transcript)},
    }
    rows = list(evals._iter_targets(state))
    assert [row[0] for row in rows] == ["ok"]


def test_current_run_can_be_cleared_after_direct_binding():
    util.bind_current_run("hygiene")
    assert util.current_run_id() == "hygiene"
    util.clear_current_run()
    assert util.current_run_id() == ""
    runs._USAGE.clear()
    runs.bump_usage(llm_in_tok=1)
    assert runs._USAGE == {}


def test_import_contract_from_repo_root():
    root = Path(__file__).resolve().parents[2]
    code = "import video2pdf.config as c; print(c.TASKS_STORE, c.UPLOADS_TTL_H)"
    env = dict(__import__("os").environ)
    env["V2PDF_TASKS_STORE"] = ""
    env["V2PDF_UPLOADS_TTL_H"] = ""
    result = subprocess.run([sys.executable, "-c", code], cwd=root / "backend",
                            capture_output=True, text=True, env=env, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[:2] == ["json", "24.0"]

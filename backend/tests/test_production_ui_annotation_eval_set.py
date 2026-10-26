"""Eight annotations, real failing media observations and executable controls."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check_production_ui_annotations.py"
spec = importlib.util.spec_from_file_location("production_ui_annotations", SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
CASES = gate.cases()
CHECKS = [(case, name) for case in CASES for name in case["expectedOutput"]["checks"]]
NEGATIVES = [(case, negative) for case in CASES for negative in case["counterexamples"]]


def mutate(packet, pointer, value):
    keys = [key.replace("~1", "/").replace("~0", "~") for key in pointer[1:].split("/")]
    for key in keys[:-1]:
        packet = packet[int(key)] if isinstance(packet, list) else packet[key]
    key = int(keys[-1]) if isinstance(packet, list) else keys[-1]
    packet[key] = value


def test_catalog_covers_every_user_annotation_with_criteria_sources_and_real_tests():
    assert gate.validate(CASES) == []
    assert [case["metadata"]["annotation_index"] for case in CASES] == list(range(1, 9))
    assert len(NEGATIVES) == 42
    assert {name for _, name in CHECKS} == set(gate.CHECKS)
    assert CASES[3]["metadata"]["source_message_id"] == CASES[4]["metadata"]["source_message_id"]


@pytest.mark.parametrize("case,name", CHECKS, ids=[case["id"] + ":" + name for case, name in CHECKS])
def test_every_acceptance_check_accepts_its_positive_control(case, name):
    assert gate.check(name, case["input"]["sample"], case["expectedOutput"])
    assert case["input"]["sample"]["kind"] == "synthetic"
    assert case["metadata"]["offline_can_close_production"] is False


@pytest.mark.parametrize("case,negative", NEGATIVES,
                         ids=[case["id"] + ":" + negative["name"] for case, negative in NEGATIVES])
def test_each_counterexample_is_rejected(case, negative):
    packet = deepcopy(case["input"]["sample"])
    mutate(packet, negative["path"], negative["value"])
    assert not gate.check(negative["check"], packet, case["expectedOutput"])


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_historical_observations_replay_without_turning_failures_into_passes(case):
    packet = gate.historical_packet(case, gate.load_sources(case))
    actual = {name: None if packet is None else gate.check(name, packet, case["expectedOutput"])
              for name in case["expectedOutput"]["checks"]}
    assert actual == case["metadata"]["historical_outcomes"]


def test_screenshots_do_not_invent_missing_configuration_action_logs():
    for case in CASES[:2]:
        sources = gate.load_sources(case)
        assert sources
        assert gate.historical_packet(case, sources) is None
    for name in ("settings_save", "settings_privacy"):
        assert not gate.check(name, {"screenshot_exists": True, "passed": True}, {})


def test_action_log_schema_supports_future_real_audits_without_relabeling_synthetic_controls():
    for case in CASES[:2]:
        sources = gate.load_sources(case)
        sources["action_audit"] = {"kind": "synthetic", "packet": case["input"]["sample"]}
        assert gate.historical_packet(case, sources) is None
        sources["action_audit"]["kind"] = "production_browser_action_audit"
        assert gate.historical_packet(case, sources) is None
        sources["action_audit"]["packet"] = deepcopy(case["input"]["sample"])
        sources["action_audit"]["packet"]["kind"] = "production"  # schema control, not a real browser observation
        packet = gate.historical_packet(case, sources)
        assert all(gate.check(name, packet, case["expectedOutput"])
                   for name in case["expectedOutput"]["checks"])


def test_settings_privacy_distinguishes_website_auth_storage_from_llm_credentials():
    case = CASES[1]
    packet = deepcopy(case["input"]["sample"])
    packet["storage_writes"] = [{"key": "v2md_token", "contains_llm_key": False}]
    assert gate.check("settings_privacy", packet, case["expectedOutput"])
    packet["storage_writes"][0]["contains_llm_key"] = True
    assert not gate.check("settings_privacy", packet, case["expectedOutput"])


def test_historical_media_checks_cover_both_chapters():
    original = CASES[4]
    sources = gate.load_sources(original)
    for chapter, count in ((1, 16), (2, 18)):
        case = deepcopy(original)
        case["input"]["chapter"] = chapter
        if chapter == 2:
            sources["samples"] = sources["second_samples"]
        packet = gate.historical_packet(case, sources)
        assert len(packet["expected_images"]) == len(packet["expected_videos"]) == count
        assert gate.check("assets", packet, case["expectedOutput"])
        assert not gate.check("ready", packet, case["expectedOutput"])
        assert not gate.check("timely", packet, case["expectedOutput"])
        assert not gate.check("playback", packet, case["expectedOutput"])
    assert sum(len([row for row in sources["book"]["media"] if row["name"].endswith(extension)])
               for extension in (".jpg", ".mp4")) == 68


def test_ocr_duration_is_recalculated_in_milliseconds_without_inventing_an_accuracy_score():
    case = CASES[3]
    packet = gate.historical_packet(case, gate.load_sources(case))
    assert gate.check("ocr", packet, case["expectedOutput"])
    spans = {span["event"]: span["durationMs"] for span in packet["run"]["spans"]}
    assert spans["stage:ocr"] == 463527
    assert spans["stage:ocr"] - spans["entry 1/2 ocr"] - spans["entry 2/2 ocr"] == 26
    assert round(spans["stage:ocr"] / 1000) == 7 * 60 + 44


@pytest.mark.parametrize("value", [None, True, False, float("nan"), float("inf"), -1, "12000"])
@pytest.mark.parametrize("index,name,pointer", [
    (2, "runtime", "/runtime/0/ready"),
    (3, "ocr", "/run/spans/2/durationMs"),
    (5, "timely", "/samples/1/elapsed_ms"),
    (7, "dedup", "/after/cost/usage/spend_usd"),
])
def test_unknown_or_untyped_measurements_fail_closed(index, name, pointer, value):
    case = CASES[index]
    packet = deepcopy(case["input"]["sample"])
    mutate(packet, pointer, value)
    assert not gate.check(name, packet, case["expectedOutput"])


def test_backend_artifact_and_handwritten_success_flags_cannot_prove_video_playback():
    case = CASES[6]
    packet = gate.historical_packet(case, gate.load_sources(case))
    packet.update(passed=True, ready=True, backend_decode_success=True)
    assert gate.check("assets", packet, case["expectedOutput"])
    assert not gate.check("ready", packet, case["expectedOutput"])
    assert not gate.check("timely", packet, case["expectedOutput"])
    assert not gate.check("playback", packet, case["expectedOutput"])


def test_dedup_recalculates_raw_ledger_instead_of_trusting_unchanged_flags():
    case = CASES[7]
    packet = gate.historical_packet(case, gate.load_sources(case))
    assert gate.check("dedup", packet, case["expectedOutput"])
    assert not gate.check("playback", packet, case["expectedOutput"])
    packet.update(same_monthly_usage=True, same_run_ids=True, passed=True)
    packet["after"]["runs"][0]["cost_usd"] += 0.000001
    assert not gate.check("dedup", packet, case["expectedOutput"])


@pytest.mark.parametrize("defect", ["missing", "duplicate", "order", "no-source", "no-check",
                                   "no-negatives", "no-regression", "false-closure", "unknown-check", "no-evidence"])
def test_catalog_coverage_cannot_silently_drop_or_relabel_an_annotation(defect):
    rows = deepcopy(CASES)
    if defect == "missing":
        rows.pop()
    elif defect == "duplicate":
        rows[-1] = rows[0]
    elif defect == "order":
        rows.reverse()
    elif defect == "no-source":
        rows[0]["metadata"]["source_quote"] = ""
    elif defect == "no-check":
        rows[0]["expectedOutput"]["checks"] = []
    elif defect == "no-negatives":
        rows[0]["counterexamples"] = []
    elif defect == "no-regression":
        rows[0]["metadata"]["regressions"] = ["missing.py::not_a_test"]
    elif defect == "false-closure":
        rows[0]["metadata"]["offline_can_close_production"] = True
    elif defect == "no-evidence":
        rows[0]["input"]["sources"] = {}
    else:
        rows[0]["expectedOutput"]["checks"] = ["accept_everything"]
    assert gate.validate(rows)


def test_changed_evidence_private_paths_and_symlink_escapes_are_rejected(tmp_path):
    case = deepcopy(CASES[2])
    case["input"]["sources"]["runtime"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="changed evidence"):
        gate.load_sources(case)
    for name in (".env", "../private.json", "/tmp/private.json"):
        case["input"]["sources"]["runtime"]["path"] = name
        with pytest.raises(ValueError):
            gate.load_sources(case)
    outside = tmp_path / "private"
    outside.mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/testing").symlink_to(outside, target_is_directory=True)
    case["input"]["sources"]["runtime"]["path"] = "docs/testing/private.json"
    with pytest.raises(ValueError):
        gate.load_sources(case, tmp_path)


@pytest.mark.parametrize("row", [None, True, [], "FIXTURE_SECRET_CANARY", {"id": "invalid"}])
def test_invalid_catalog_cli_suppresses_source_content_and_tracebacks(tmp_path, row):
    path = tmp_path / "invalid.jsonl"
    path.write_text(json.dumps(row) + "\n")
    result = subprocess.run([sys.executable, str(SCRIPT), "--dataset", str(path)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 1
    assert "FIXTURE_SECRET_CANARY" not in result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def test_cli_separates_evidence_replay_from_production_acceptance():
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and "8 offline" in result.stdout
    assert "no new production acceptance" in result.stdout
    result = subprocess.run([sys.executable, str(SCRIPT), "--require-closed"],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 1
    assert {line.split()[1] for line in result.stdout.splitlines() if line.startswith("OPEN ")} == {
        "pua-001:", "pua-002:", "pua-005:", "pua-006:", "pua-007:", "pua-008:",
    }


def test_collection_never_initializes_project_configuration_or_real_services():
    code = 'import runpy,sys; runpy.run_path(sys.argv[1]); assert "video2pdf.config" not in sys.modules'
    result = subprocess.run([sys.executable, "-c", code, str(Path(__file__))],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_ci_runs_the_new_eval_tests_and_non_closing_replay():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    steps = workflow["jobs"]["test"]["steps"]
    matches = [step for step in steps if "tests/test_production_ui_annotation_eval_set.py" in step.get("run", "")]
    assert len(matches) == 1 and "if" not in matches[0] and not matches[0].get("continue-on-error", False)
    assert any(step.get("run") == "python3 scripts/check_production_ui_annotations.py" for step in steps)
    lint = "\n".join(step.get("run", "") for step in workflow["jobs"]["lint"]["steps"])
    assert "../scripts/check_production_ui_annotations.py" in lint

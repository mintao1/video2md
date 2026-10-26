"""44 annotations, raw-evidence mutations and honest acceptance boundaries."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check_bilibili_review_regressions.py"
SPEC = importlib.util.spec_from_file_location("bbr_gate", SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
CASES = gate.cases()
NEGATIVES = [(case, negative) for case in CASES for negative in case["negative_cases"]]


def mutate(data, path, value, *, delete=False):
    parts = path.split(".")
    for key in parts[:-1]:
        data = data[int(key)] if isinstance(data, list) else data[key]
    key = int(parts[-1]) if isinstance(data, list) else parts[-1]
    if delete:
        del data[key]
    else:
        data[key] = value


def test_every_annotation_has_independent_criteria_evidence_tests_and_negatives():
    assert [c["id"] for c in CASES] == [f"bbr-{i:03d}" for i in range(1, 45)]
    assert [c["metadata"]["annotation_index"] for c in CASES] == list(range(1, 45))
    assert len({c["input"]["scenario"] for c in CASES}) == 44
    for case in CASES:
        assert case["expectedOutput"]["criteria"]
        assert len(case["expectedOutput"]["assertions"]) >= 3
        assert len(case["negative_cases"]) >= 3
        for name in ("evidence_refs", "regressions"):
            assert case["metadata"][name]
            for ref in case["metadata"][name]:
                assert (ROOT / ref.split("::")[0]).is_file(), ref


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_preserved_annotation_replays(case):
    assert gate.check_case(case, gate.load_evidence())


@pytest.mark.parametrize(
    ("case", "negative"), NEGATIVES, ids=[f"{c['id']}-{n['name']}" for c, n in NEGATIVES]
)
def test_each_annotated_failure_is_rejected(case, negative):
    data = gate.load_evidence()
    mutate(data, negative["path"], negative["value"])
    assert not gate.check_case(case, data), negative


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_missing_case_measurement_never_passes(case):
    data = gate.load_evidence()
    raw = next(r for r in case["expectedOutput"]["assertions"] if not r["path"].startswith("derived."))
    mutate(data, raw["path"].replace(".*.", ".0."), None, delete=True)
    assert not gate.check_case(case, data)


@pytest.mark.parametrize("key", ["llm_in_tok", "llm_out_tok", "gpu_s", "runs_n", "spend_usd"])
def test_dedup_cannot_hide_missing_or_nonzero_delta(key):
    data = gate.load_evidence()
    del data["billing"]["duplicate_submit_deltas"][key]
    assert not gate.check_case(CASES[42], data)
    data = gate.load_evidence()
    data["dedup"]["cost"]["usage"][key] += 1
    assert not gate.check_case(CASES[42], data)


@pytest.mark.parametrize("value", [False, True, float("nan"), float("inf"), "15939", -1, 0])
def test_invalid_usage_cannot_pass_positive_status_flags(value):
    data = gate.load_evidence()
    data["final"]["run"]["usage"]["llm_in_tok"] = value
    assert not gate.check_case(CASES[42], data)


def test_asr_snapshot_and_ocr_snapshot_are_not_interchangeable():
    data = gate.load_evidence()
    assert data["progress6"]["run"]["artifacts"][0]["transcript_chars"] == 510
    assert data["progress11"]["run"]["artifacts"][0]["transcript_chars"] == 1222
    data["progress6"] = deepcopy(data["progress11"])
    assert not gate.check_case(CASES[17], data)


@pytest.mark.parametrize("source", ["before", "dedup", "progress6", "progress14"])
def test_unrelated_run_cannot_supply_billing_or_retry_evidence(source):
    data = gate.load_evidence()
    data[source]["run"]["run_id"] = "another-run"
    assert not gate.check_case(CASES[11], data)
    assert not gate.check_case(CASES[14], data)


def test_handwritten_release_match_flag_cannot_hide_bad_hash():
    data = gate.load_evidence()
    data["release"]["all_release_file_hashes_match"] = True
    data["manifest"]["files"]["backend/video2pdf/bilibili.py"] = "0" * 64
    assert not gate.check_case(CASES[20], data)


@pytest.mark.parametrize(
    "path,value",
    [
        ("final.fake_llm_enabled", True),
        ("final.run.model_calls.0.status", "failed"),
        ("final.run.model_calls.0.usage.prompt_tokens", 0),
        ("final.run.cost_pricing.gpu_policy", "paid"),
        ("billing_gate.billing.gpu_free_reason", ""),
        ("final.run.cost_usd", 0),
        ("final.run.artifacts.0.stage_metrics.clips.gpu_seconds", 0),
    ],
)
def test_fake_or_failed_inference_and_free_gpu_without_basis_are_rejected(path, value):
    data = gate.load_evidence()
    mutate(data, path, value)
    assert not gate.check_case(CASES[42], data)


def test_service_running_flag_cannot_be_replaced_by_truthy_integer():
    data = gate.load_evidence()
    key = next(iter(data["home"]["services_loaded"]))
    data["home"]["services_loaded"][key] = 1
    assert not gate.check_case(CASES[9], data)


def test_boolean_part_index_is_not_the_integer_episode_one():
    data = gate.load_evidence()
    data["final"]["run"]["entries"][0]["playlist_index"] = True
    assert not gate.check_case(CASES[11], data)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown"])
def test_matrix_ids_fail_closed(monkeypatch, mutation):
    rows = deepcopy(CASES)
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(rows[0])
    else:
        rows[0]["id"] = "unrecognized"
    monkeypatch.setattr(gate, "cases", lambda: rows)
    assert gate.evaluate(gate.load_evidence()) == ["matrix: missing, duplicate or unknown annotation id"]


def test_all_green_labels_cannot_close_unresolved_observations():
    data = gate.load_evidence()
    assert gate.evaluate(data) == []
    data["report"]["status"] = "PASS"
    failures = gate.evaluate(data, require_closed=True)
    assert all(any(f.startswith(f"bbr-{i:03d}:") and "OPEN" in f for f in failures) for i in (2, 22, 44))


def test_cli_reports_scope_and_rejects_bad_or_unclosed_evidence(tmp_path):
    replay = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True)
    assert replay.returncode == 0 and "offline" in replay.stdout
    assert "no new production acceptance" in replay.stdout
    strict = subprocess.run([sys.executable, str(SCRIPT), "--require-closed"], capture_output=True, text=True)
    assert strict.returncode == 1 and "bbr-044: OPEN" in strict.stdout
    (tmp_path / "production-final.json").write_text('{"private_secret":invalid}')
    broken = subprocess.run(
        [sys.executable, str(SCRIPT), "--evidence-dir", str(tmp_path)], capture_output=True, text=True
    )
    assert broken.returncode == 1 and "private_secret" not in broken.stdout
    assert "Traceback" not in broken.stderr


def test_ci_wires_snapshot_and_real_application_regressions():
    source = (ROOT / ".github/workflows/ci.yml").read_text()
    for name in (
        "test_bilibili_review_eval_set.py",
        "test_bilibili_test_isolation.py",
        "test_bilibili_review_operations.py",
        "test_bilibili_public_fallback.py",
        "check_bilibili_review_regressions.py",
        "npm run test:integration",
    ):
        assert name in source

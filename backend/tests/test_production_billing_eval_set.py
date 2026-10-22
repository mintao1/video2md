"""Static gate for the user-authorized, billable production evaluation contract."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVAL_SET = ROOT / "docs/evals/production-model-billing-eval-set.jsonl"
SPEC = importlib.util.spec_from_file_location(
    "check_production_billing", ROOT / "scripts/check_production_billing.py"
)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def _evaluate(evidence: dict) -> list[str]:
    return gate.evaluate(evidence, allow_synthetic=True)


def _rows() -> list[dict]:
    return [
        json.loads(line)
        for line in EVAL_SET.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_billable_production_eval_set_has_complete_unique_contract() -> None:
    rows = _rows()
    assert {row["id"] for row in rows} == {
        "prod-billing-001",
        "prod-billing-002",
        "prod-billing-003",
        "prod-billing-004",
        "prod-billing-005",
        "prod-billing-006",
        "prod-billing-007",
        "prod-billing-008",
        "prod-billing-009",
        "prod-billing-010",
        "prod-billing-011",
        "prod-billing-012",
        "prod-billing-013",
        "prod-billing-014",
        "prod-billing-015",
        "prod-billing-016",
        "prod-billing-017",
        "prod-billing-018",
    }
    assert len({row["id"] for row in rows}) == len(rows)
    assert all(row.get("actions") and row.get("pass") and row.get("fail_categories") for row in rows)
    assert all(row.get("secrets") for row in rows)


def test_real_billing_authorization_is_persisted_for_production_tests() -> None:
    for row in _rows():
        authorization = row["authorization"]
        assert authorization["real_billing_preapproved"] is True
        assert authorization["reconfirm_each_run"] is False


def test_eval_set_guards_cost_and_silent_submission_regressions() -> None:
    rows = {row["id"]: row for row in _rows()}
    billing = rows["prod-billing-003"]["pass"]
    assert billing["usage_present"] is True
    assert billing["monthly_rollup_includes_run"] is True
    idempotency = rows["prod-billing-004"]["pass"]
    assert idempotency["duplicate_usage_absent"] is True
    assert idempotency["duplicate_cost_absent"] is True
    failure_ui = rows["prod-billing-006"]["pass"]
    assert failure_ui == {
        "error_visible": True,
        "modal_remains_open": True,
        "entered_url_preserved": True,
        "success_toast_absent": True,
    }


def _passing_evidence() -> dict:
    return {
        "kind": "synthetic",
        "authentication": {"logout_observed": True, "oauth_login_succeeded": True},
        "source_resolution": {"provider": "bilibili", "resolved": True},
        "submission": {
            "feedback_visible": True,
            "run_id": "redacted",
            "submit_log_present": True,
            "url_entered_explicitly": True,
            "task_count_before": 2,
            "task_count_after": 3,
        },
        "execution": {
            "status": "done",
            "model_called": True,
            "model_inference_succeeded": True,
            "running_stage_observed": True,
            "failure_reason_visible": True,
            "failure_stage_visible": True,
            "run_id_visible": True,
        },
        "billing": {
            "usage": {"llm_in_tok": 10, "llm_out_tok": 5, "gpu_s": 1.0},
            "cost_usd": 0.00102,
            "prices": {"llm_in_mtok": 1.0, "llm_out_mtok": 2.0, "gpu_min": 0.06},
            "monthly_rollup_includes_run": True,
            "usage_visible": True,
            "cost_visible": True,
            "positive_prices_configured": True,
        },
        "artifact": {"book_visible": True, "markdown_nonempty": True, "book_count_delta": 1},
    }


def test_complete_billable_pipeline_evidence_passes() -> None:
    assert _evaluate(_passing_evidence()) == []


def test_observed_source_and_silent_submission_regression_fails() -> None:
    evidence = _passing_evidence()
    evidence["source_resolution"]["resolved"] = False
    evidence["submission"] = {"feedback_visible": False, "run_id": "", "submit_log_present": False}
    labels = {failure.split(":", 1)[0] for failure in _evaluate(evidence)}
    assert labels >= {
        "prod-billing-source",
        "prod-billing-feedback",
        "prod-billing-run",
        "prod-billing-submit-log",
    }


def test_zero_cost_without_model_usage_can_never_pass() -> None:
    evidence = _passing_evidence()
    evidence["execution"]["model_called"] = False
    evidence["billing"] = {
        "usage": {"llm_in_tok": 0, "llm_out_tok": 0, "gpu_s": 0},
        "cost_usd": 0,
        "monthly_rollup_includes_run": False,
    }
    labels = {failure.split(":", 1)[0] for failure in _evaluate(evidence)}
    assert labels >= {"prod-billing-model", "prod-billing-usage", "prod-billing-rollup"}


def test_login_and_explicit_url_are_required() -> None:
    evidence = _passing_evidence()
    evidence["authentication"] = {"logout_observed": False, "oauth_login_succeeded": False}
    evidence["submission"]["url_entered_explicitly"] = False
    labels = {failure.split(":", 1)[0] for failure in _evaluate(evidence)}
    assert labels >= {"prod-billing-logout", "prod-billing-login", "prod-billing-explicit-url"}


def test_new_task_and_running_stage_are_required() -> None:
    evidence = _passing_evidence()
    evidence["submission"]["task_count_after"] = 2
    evidence["execution"]["running_stage_observed"] = False
    labels = {failure.split(":", 1)[0] for failure in _evaluate(evidence)}
    assert labels >= {"prod-billing-new-task", "prod-billing-stage-progress"}


def test_error_terminal_never_counts_as_successful_billing() -> None:
    evidence = _passing_evidence()
    evidence["execution"]["status"] = "error"
    labels = {failure.split(":", 1)[0] for failure in _evaluate(evidence)}
    assert "prod-billing-success-terminal" in labels


def test_zero_artifact_and_hidden_usage_are_regressions() -> None:
    evidence = _passing_evidence()
    evidence["artifact"] = {"book_visible": False, "markdown_nonempty": False, "book_count_delta": 0}
    evidence["billing"]["usage_visible"] = False
    evidence["billing"]["cost_visible"] = False
    labels = {failure.split(":", 1)[0] for failure in _evaluate(evidence)}
    assert labels >= {
        "prod-billing-artifact",
        "prod-billing-book-delta",
        "prod-billing-usage-visible",
        "prod-billing-cost-visible",
    }


def test_disabled_model_zero_prices_and_opaque_failure_are_regressions() -> None:
    evidence = _passing_evidence()
    evidence["execution"].update({
        "status": "error",
        "model_called": False,
        "failure_reason_visible": False,
        "failure_stage_visible": False,
        "run_id_visible": False,
    })
    evidence["billing"]["positive_prices_configured"] = False
    evidence["billing"]["prices"]["gpu_min"] = 0
    labels = {failure.split(":", 1)[0] for failure in _evaluate(evidence)}
    assert labels >= {
        "prod-billing-model",
        "prod-billing-prices",
        "prod-billing-failure-reason",
        "prod-billing-failure-stage",
        "prod-billing-run-visible",
    }


def test_positive_gpu_does_not_hide_zero_llm_tokens_or_rejected_inference() -> None:
    evidence = _passing_evidence()
    evidence["execution"]["model_inference_succeeded"] = False
    evidence["billing"]["usage"].update(llm_in_tok=0, llm_out_tok=0, gpu_s=204.669)
    labels = {failure.split(":", 1)[0] for failure in _evaluate(evidence)}
    assert labels >= {"prod-billing-inference", "prod-billing-tokens"}


def test_claiming_positive_prices_cannot_override_zero_or_invalid_numeric_prices() -> None:
    for invalid in (0, -1, True, "1", None, float("nan"), float("inf")):
        evidence = _passing_evidence()
        evidence["billing"]["positive_prices_configured"] = True
        evidence["billing"]["prices"]["gpu_min"] = invalid
        assert any(f.startswith("prod-billing-prices:") for f in _evaluate(evidence))


def test_zero_or_incorrect_cost_fails_even_with_positive_usage_and_prices() -> None:
    for invalid in (0, 1, -1, True, "0.00102", None, float("nan"), float("inf")):
        evidence = _passing_evidence()
        evidence["billing"]["cost_usd"] = invalid
        assert any(f.startswith("prod-billing-cost-formula:") for f in _evaluate(evidence))


def test_success_does_not_require_a_fabricated_failure() -> None:
    evidence = _passing_evidence()
    evidence["execution"].pop("failure_reason_visible")
    evidence["execution"].pop("failure_stage_visible")
    assert _evaluate(evidence) == []


def test_synthetic_billing_examples_are_never_production_acceptance() -> None:
    assert gate.evaluate(_passing_evidence()) == [
        "prod-billing-evidence-kind: synthetic or unspecified evidence is not production acceptance"
    ]


def test_positive_tokens_cannot_hide_missing_gpu_measurements() -> None:
    for invalid in (0, -1, True, None, float('nan'), float('inf')):
        evidence = _passing_evidence()
        evidence['billing']['usage']['gpu_s'] = invalid
        assert any(f.startswith('prod-billing-gpu:') for f in _evaluate(evidence))


def test_free_gpu_is_valid_only_with_explicit_policy_and_positive_llm_tariffs() -> None:
    evidence = _passing_evidence()
    evidence['billing'].update(gpu_policy='free', gpu_free_reason='User-approved Colab free tier', cost_usd=0.00002)
    evidence['billing']['prices']['gpu_min'] = 0
    assert _evaluate(evidence) == []
    evidence['billing']['prices']['llm_in_mtok'] = 0
    assert any(f.startswith('prod-billing-prices:') for f in _evaluate(evidence))

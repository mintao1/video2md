#!/usr/bin/env python3
"""Offline regression gate for the 2026-10-26 4c production run.

The checker reads redacted evidence captured from production. It never logs in,
submits a task, calls a model, or treats the catalog alone as acceptance.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "docs/evals/production-live-regression-eval-set-2026-10-26.jsonl"
EVIDENCE = ROOT / "docs/evals/production-live-evidence-2026-10-26.json"
RUN_EVIDENCE = {
    "c2e5d741": ROOT / "docs/testing/2026-10-26/billing-evidence-c2e5d741.json",
    "70f4a4d4": ROOT / "docs/testing/2026-10-26/billing-evidence-70f4a4d4.json",
}
IDS = [f"plr-{index:03d}" for index in range(1, 11)]
SECRET = re.compile(r"(?i)(?:bearer\s+\S+|api[_-]?key\s*[:=]|access[_-]?token\s*[:=]|cookie\s*[:=]|[?&](?:token|sig|key)=)")


def rows(path: Path = DATASET) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _finite_positive(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def validate_catalog(data: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    if [item.get("id") for item in data] != IDS:
        failures.append("catalog: ids must be ordered plr-001..plr-010")
    for item in data:
        item_id = item.get("id")
        if item.get("scope") != "production-live-regression":
            failures.append(f"{item_id}: invalid scope")
        metadata = item.get("metadata") or {}
        if metadata.get("offline_can_close_production") is not False:
            failures.append(f"{item_id}: offline closure must remain false")
        if not metadata.get("production_evidence_required"):
            failures.append(f"{item_id}: production evidence requirement missing")
        if len(item.get("counterexamples") or []) < 3:
            failures.append(f"{item_id}: at least three counterexamples required")
        if SECRET.search(json.dumps(item, ensure_ascii=False)):
            failures.append(f"{item_id}: secret-like literal found")
    if len({item.get("id") for item in data}) != len(data):
        failures.append("catalog: duplicate ids")
    return failures


def evaluate(evidence: dict[str, Any], runs: dict[str, dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    if evidence.get("kind") != "production":
        failures.append("evidence: kind must be production")
    if evidence.get("environment", {}).get("host_alias") != "lcode-4c":
        failures.append("environment: production evidence must be captured on lcode-4c")
    surface = evidence.get("anonymous_surface") or {}
    for key, expected in {"healthz": 200, "terms": 200, "privacy": 200, "me": 401, "runs": 401, "docs": 403, "openapi": 403}.items():
        if surface.get(key) != expected:
            failures.append(f"surface-{key}: expected {expected}")
    preflight = evidence.get("preflight") or {}
    if preflight.get("ok") is not True or not all(preflight.get(key) is True for key in ("ffmpeg", "disk", "vault", "colab_cli")):
        failures.append("preflight: all production readiness checks must pass")
    router = evidence.get("gpu_router") or {}
    if router.get("running") != 1 or router.get("ready") != 1:
        failures.append("gpu-router: expected 1/1 ready")
    channel = evidence.get("model_channel") or {}
    if channel.get("models_http") != 200 or channel.get("chat_http") != 200 or not channel.get("model"):
        failures.append("model-channel: models and chat endpoints must return 200")
    auth = evidence.get("authentication") or {}
    for key in ("logout_observed", "oauth_login_succeeded", "session_refresh_kept_login", "protocol_guard"):
        if auth.get(key) is not True:
            failures.append(f"authentication-{key}: required observation missing")
    colab = evidence.get("colab") or {}
    for key in ("explicit_consent", "connected", "authorization_saved", "tenant_scoped"):
        if colab.get(key) is not True:
            failures.append(f"colab-{key}: required observation missing")
    history = evidence.get("history") or {}
    if history.get("count_after", 0) != history.get("count_before", -1) + 1 or history.get("book_count_delta") != 1 or history.get("new_book_visible") is not True:
        failures.append("history: task and library increments are inconsistent")
    for run_id, run in runs.items():
        if run.get("kind") != "production":
            failures.append(f"{run_id}: evidence kind must be production")
        if run.get("submission", {}).get("run_id") != run_id:
            failures.append(f"{run_id}: submission run_id mismatch")
        execution = run.get("execution") or {}
        if execution.get("status") != "done" or execution.get("model_called") is not True or execution.get("model_inference_succeeded") is not True:
            failures.append(f"{run_id}: successful model execution required")
        usage = run.get("billing", {}).get("usage") or {}
        if not _finite_positive(usage.get("llm_in_tok")) or not _finite_positive(usage.get("llm_out_tok")):
            failures.append(f"{run_id}: positive LLM usage required")
        prices = run.get("billing", {}).get("prices") or {}
        expected = usage.get("llm_in_tok", 0) * prices.get("llm_in_mtok", 0) * 1e-6 + usage.get("llm_out_tok", 0) * prices.get("llm_out_mtok", 0) * 1e-6 + usage.get("llm_cached_in_tok", 0) * prices.get("llm_cached_in_mtok", 0) * 1e-6
        actual = run.get("billing", {}).get("cost_usd")
        if not _finite_positive(actual) or abs(actual - expected) > 0.000001:
            failures.append(f"{run_id}: usage/cost does not reconcile")
        if run.get("billing", {}).get("monthly_rollup_includes_run") is not True:
            failures.append(f"{run_id}: monthly rollup must include run")
        artifact = run.get("artifact") or {}
        if artifact.get("book_visible") is not True or artifact.get("markdown_nonempty") is not True or artifact.get("book_count_delta") != 1:
            failures.append(f"{run_id}: non-empty textbook artifact required")
    sse = runs.get("70f4a4d4", {}).get("execution", {}).get("sse") or {}
    if sse.get("http_status") != 200 or sse.get("connected") is not True or sse.get("terminal_event") != "graph:end" or sse.get("terminal_status") != "done":
        failures.append("sse: authenticated live stream did not reach graph:end(done)")
    if runs.get("70f4a4d4", {}).get("authentication", {}).get("sse_no_auth_http") != 401:
        failures.append("sse: unauthenticated stream must be rejected with 401")
    if SECRET.search(json.dumps(evidence, ensure_ascii=False)) or any(SECRET.search(json.dumps(run, ensure_ascii=False)) for run in runs.values()):
        failures.append("secrets: redacted evidence contains a secret-like literal")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--evidence", type=Path, default=EVIDENCE)
    args = parser.parse_args()
    try:
        catalog = rows(args.dataset)
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        run_data = {run_id: json.loads(path.read_text(encoding="utf-8")) for run_id, path in RUN_EVIDENCE.items()}
        failures = validate_catalog(catalog) + evaluate(evidence, run_data)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL\n- input: {exc}")
        return 1
    if failures:
        print("FAIL")
        print("\n".join(f"- {failure}" for failure in failures))
        return 1
    print("PASS: 10 offline production-live regressions; redacted 4c evidence reconciles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

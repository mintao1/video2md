#!/usr/bin/env python3
"""Offline contract checker for the 2026-10-26 login-to-task boundary set."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "docs/evals/production-login-boundary-eval-set-2026-10-26.jsonl"
REQUIRED_IDS = [f"plb-{i:03d}" for i in range(1, 6)]
SECRET_LITERAL = re.compile(
    r"(?:Bearer\s+\S+|sk-[A-Za-z0-9]{12,}|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|"
    r"(?:api[_-]?key|access[_-]?token|client[_-]?secret|cookie)\s*[:=])",
    re.IGNORECASE,
)


def _nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def session_identity_binding(sample: dict[str, Any]) -> bool:
    return (
        sample.get("callback_success") is True
        and sample.get("me_status") == 200
        and sample.get("library_status") == 200
        and sample.get("runs_status") == 200
        and isinstance(sample.get("session_subject"), str)
        and sample.get("session_subject")
        and sample.get("library_owner") == sample["session_subject"]
        and sample.get("runs_owner") == sample["session_subject"]
        and sample.get("anonymous_fallback") is False
    )


def logout_cache_invalidation(sample: dict[str, Any]) -> bool:
    statuses = sample.get("protected_statuses")
    return (
        sample.get("logout_status") == 200
        and sample.get("token_present_after_logout") is False
        and sample.get("library_items_after_logout") == 0
        and sample.get("runs_items_after_logout") == 0
        and isinstance(statuses, list)
        and statuses
        and all(status == 401 for status in statuses)
        and sample.get("redirect_path") == "/"
        and sample.get("redirect_preserved") is True
    )


def tenant_switch_isolation(sample: dict[str, Any]) -> bool:
    a = sample.get("user_a") or {}
    b = sample.get("user_b") or {}
    after = sample.get("after_switch") or {}
    return (
        _nonnegative_int(a.get("books"))
        and _nonnegative_int(a.get("runs"))
        and _nonnegative_int(b.get("books"))
        and _nonnegative_int(b.get("runs"))
        and after.get("visible_books_owner") == "user-b"
        and after.get("visible_runs_owner") == "user-b"
        and after.get("cross_tenant_rows") == 0
        and after.get("stale_rows") == 0
    )


def safe_redirect(sample: dict[str, Any]) -> bool:
    requested = sample.get("requested_next")
    accepted = sample.get("accepted_next")
    return (
        isinstance(requested, str)
        and requested.startswith("/")
        and not requested.startswith("//")
        and accepted == requested
        and sample.get("external_next_rejected") is True
        and sample.get("callback_redirect") == accepted
    )


def explicit_single_submission(sample: dict[str, Any]) -> bool:
    return (
        sample.get("preflight_before_submit") is True
        and isinstance(sample.get("input_url"), str)
        and sample["input_url"].startswith("https://ferosse.tech/test-assets/")
        and sample.get("input_source") == "typed"
        and sample.get("clipboard_autosubmit") is False
        and sample.get("submit_count") == 1
        and sample.get("run_count_delta") == 1
        and sample.get("run_owner") == "user-a"
        and sample.get("billing_started_after_submit") is True
        and sample.get("duplicate_run_count") == 0
    )


CHECKS = {
    "session_identity_binding": session_identity_binding,
    "logout_cache_invalidation": logout_cache_invalidation,
    "tenant_switch_isolation": tenant_switch_isolation,
    "safe_redirect": safe_redirect,
    "explicit_single_submission": explicit_single_submission,
}


def load_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _contains_secret(value: Any) -> bool:
    return bool(SECRET_LITERAL.search(json.dumps(value, ensure_ascii=False)))


def validate(rows: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    if [row.get("id") for row in rows] != REQUIRED_IDS:
        failures.append("catalog: ids must be ordered plb-001..plb-005")
    for row in rows:
        row_id = row.get("id")
        metadata = row.get("metadata") or {}
        if row.get("scope") != "production-login-boundary":
            failures.append(f"{row_id}: invalid scope")
        if metadata.get("offline_can_close_production") is not False:
            failures.append(f"{row_id}: offline closure must remain false")
        if not metadata.get("production_evidence_required"):
            failures.append(f"{row_id}: production evidence requirement missing")
        expected = row.get("expectedOutput") or {}
        checks = expected.get("checks") or []
        if len(checks) != 1 or checks[0] not in CHECKS:
            failures.append(f"{row_id}: unknown check")
        negatives = row.get("counterexamples")
        if not isinstance(negatives, list) or len(negatives) < 3:
            failures.append(f"{row_id}: at least three counterexamples required")
        for negative in negatives or []:
            if negative.get("check") != (checks[0] if checks else None) or not negative.get("name"):
                failures.append(f"{row_id}: malformed counterexample")
            if "value" not in negative or not isinstance(negative.get("path"), str):
                failures.append(f"{row_id}: counterexample path/value missing")
        if _contains_secret(row):
            failures.append(f"{row_id}: secret-like literal found")
    if len({row.get("id") for row in rows}) != len(rows):
        failures.append("catalog: duplicate ids")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()
    try:
        rows = load_rows(args.dataset)
        failures = validate(rows)
        for row in rows:
            checks = row.get("expectedOutput", {}).get("checks", [])
            if checks and checks[0] in CHECKS and not CHECKS[checks[0]](row["input"]["sample"]):
                failures.append(f"{row.get('id')}: synthetic control does not satisfy its contract")
    except (OSError, ValueError, json.JSONDecodeError, TypeError, KeyError):
        failures = ["catalog: unreadable or invalid; source values suppressed"]
    if failures:
        print("FAIL")
        print("\n".join(f"- {failure}" for failure in failures))
        return 1
    print(f"PASS: {len(rows)} offline production login-boundary contracts; no new production acceptance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

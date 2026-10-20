"""Production remediation gate: public behavior plus deployable repo contracts."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "check_production_readiness", ROOT / "scripts/check_production_readiness.py"
)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def production_evidence() -> dict:
    return {
        "postgres": {"status": "healthy"},
        "schema": {"version": 4},
        "api": {"status": "healthy", "version": "0.2.0"},
        "release": {
            "branch": "main",
            "expected_commit": "a83147d5be0d75253804e74663fefccbe70a904b",
            "source_commit": "a83147d5be0d75253804e74663fefccbe70a904b",
            "expected_image": "video2md:0.2.0-hotfix-20261021g",
            "deployed_image": "video2md:0.2.0-hotfix-20261021g",
        },
        "runtime": {"pod_phase": "Running", "pod_ready": True, "restart_count": 0, "recent_error_count": 0},
        "google_token_probe": {"network_ok": True, "http_status": 404},
        "oauth_relay": {
            "topology": "host-network-loopback",
            "container_reachable_host": "127.0.0.1",
            "port": 18080,
            "container_probe_http_status": 404,
            "tunnel_service_active": True,
        },
        "container_env": {"present_forbidden": []},
        "config": {"owner": "app", "mode": "0600", "secrets_redacted": True},
        "browser_google_login": {
            "status": "passed",
            "callback_success": True,
            "session_me_http_status": 200,
            "library_http_status": 200,
            "tasks_http_status": 200,
            "profile_http_status": 200,
            "session_persistent_after_refresh": True,
        },
        "public_routes": {
            "healthz": 200,
            "terms": 200,
            "privacy": 200,
            "unauthenticated_me": 401,
            "docs": 403,
            "openapi_json": 403,
            "redoc": 403,
            "queue": 404,
        },
    }


def test_fixed_production_contract_passes() -> None:
    assert gate.evaluate(production_evidence(), require_browser=True) == []


def test_sensitive_route_or_authenticated_page_regression_fails() -> None:
    evidence = production_evidence()
    evidence["public_routes"]["docs"] = 200
    evidence["browser_google_login"]["profile_http_status"] = 401
    failures = gate.evaluate(evidence, require_browser=True)
    labels = {failure.split(":", 1)[0] for failure in failures}
    assert labels >= {"prod-public-routes", "prod-browser-google-login"}


def test_wrong_commit_unstable_pod_or_open_me_regression_fails() -> None:
    evidence = production_evidence()
    evidence["release"]["source_commit"] = "0" * 40
    evidence["runtime"]["restart_count"] = 1
    evidence["runtime"]["recent_error_count"] = 3
    evidence["public_routes"]["unauthenticated_me"] = 200
    failures = gate.evaluate(evidence, require_browser=False)
    labels = {failure.split(":", 1)[0] for failure in failures}
    assert labels >= {"prod-release-provenance", "prod-runtime-stability", "prod-public-routes"}


def test_repository_deployment_contract_is_guarded() -> None:
    assert gate.evaluate_repository(ROOT) == []


def test_eval_set_covers_every_remediated_production_behavior() -> None:
    path = ROOT / "docs/evals/production-fullsite-remediation-eval-set.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert {row["id"] for row in rows} == {
        "prod-rem-001",
        "prod-rem-002",
        "prod-rem-003",
        "prod-rem-004",
        "prod-rem-005",
        "prod-rem-006",
        "prod-rem-007",
        "prod-rem-008",
    }
    assert all(row.get("secrets") for row in rows)

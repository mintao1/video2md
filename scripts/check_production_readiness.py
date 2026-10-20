#!/usr/bin/env python3
"""Evaluate sanitized production deployment evidence; never reads secret values."""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path
from typing import Any

FORBIDDEN_ENV = {"17898", "HTTP_PROXY", "HTTPS_PROXY", "V2MD_PROXY"}


def nested(data: dict[str, Any], path: str) -> Any:
    value: Any = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"missing evidence: {path}")
        value = value[part]
    return value


def evaluate(evidence: dict[str, Any], *, require_browser: bool) -> list[str]:
    failures: list[str] = []

    def expect(label: str, condition: bool, detail: str) -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    expect("prod-db-health", nested(evidence, "postgres.status") == "healthy", "PostgreSQL must be healthy")
    schema_version = nested(evidence, "schema.version")
    expect("prod-schema-version", isinstance(schema_version, int) and schema_version >= 4, "schema version must be >= 4")
    expect("prod-api-health", nested(evidence, "api.status") == "healthy", "API must be healthy")
    expect("prod-release-version", nested(evidence, "api.version") == "0.2.0", "/healthz version must equal 0.2.0")

    probe = nested(evidence, "google_token_probe")
    probe_status = probe.get("http_status") if isinstance(probe, dict) else None
    expect(
        "prod-google-egress",
        isinstance(probe, dict)
        and probe.get("network_ok") is True
        and isinstance(probe_status, int)
        and 400 <= probe_status <= 499,
        "token endpoint must return a bounded HTTP 4xx response (not a DNS/TCP/TLS error)",
    )

    relay = nested(evidence, "oauth_relay")
    expect(
        "prod-oauth-relay-topology",
        isinstance(relay, dict)
        and relay.get("topology") == "host-network-loopback"
        and relay.get("container_reachable_host") == "127.0.0.1"
        and relay.get("port") == 18080
        and relay.get("container_probe_http_status") in range(400, 500)
        and relay.get("tunnel_service_active") is True,
        "hostNetwork pod must reach the active SSH tunnel on 127.0.0.1:18080 and receive a bounded token-endpoint 4xx",
    )

    present = nested(evidence, "container_env.present_forbidden")
    expect(
        "prod-proxy-retirement",
        isinstance(present, list) and not (set(map(str, present)) & FORBIDDEN_ENV),
        f"forbidden proxy markers present: {sorted(set(map(str, present or [])) & FORBIDDEN_ENV)}",
    )

    config = nested(evidence, "config")
    owner = config.get("owner") if isinstance(config, dict) else None
    mode_text = config.get("mode") if isinstance(config, dict) else None
    try:
        mode = int(str(mode_text), 8)
    except (TypeError, ValueError):
        mode = 0o777
    expect("prod-config-owner", owner in {"root", "app"}, "config owner must be root or app")
    expect("prod-config-mode", stat.S_IMODE(mode) & 0o077 == 0, "config must not be group/world accessible")
    expect("prod-secret-redaction", config.get("secrets_redacted") is True, "evidence must confirm secrets were redacted")

    browser = nested(evidence, "browser_google_login")
    browser_passed = (
        isinstance(browser, dict)
        and browser.get("status") == "passed"
        and browser.get("callback_success") is True
        and browser.get("session_me_http_status") == 200
        and browser.get("library_http_status") == 200
        and browser.get("tasks_http_status") == 200
        and browser.get("profile_http_status") == 200
        and browser.get("session_persistent_after_refresh") is True
    )
    if require_browser:
        expect(
            "prod-browser-google-login",
            browser_passed,
            "real callback, GET /me, GET /library and session persistence after refresh must all pass",
        )

    routes = nested(evidence, "public_routes")
    expect(
        "prod-public-routes",
        isinstance(routes, dict)
        and routes.get("healthz") == 200
        and routes.get("terms") == 200
        and routes.get("privacy") == 200
        and routes.get("docs") == 403
        and routes.get("openapi_json") == 403
        and routes.get("redoc") == 403
        and routes.get("queue") == 404,
        "health/legal routes must stay public while docs/schema/redoc/queue remain blocked",
    )

    return failures


def evaluate_repository(repo_root: Path) -> list[str]:
    """Check deployable repository contracts without network or secret access."""
    failures: list[str] = []

    def require(label: str, condition: bool, detail: str) -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    deployment = (repo_root / "deploy/k8s/base/deployment-api.yaml").read_text(encoding="utf-8")
    nginx = (repo_root / "deploy/nginx.conf").read_text(encoding="utf-8")
    relay = (repo_root / "deploy/oauth-relay/oauth-relay.service").read_text(encoding="utf-8")
    tunnel = (repo_root / "deploy/oauth-relay/oauth-tunnel.service").read_text(encoding="utf-8")
    api = (repo_root / "backend/video2md/api.py").read_text(encoding="utf-8")
    providers = (repo_root / "backend/acctkit/providers.py").read_text(encoding="utf-8")
    router = (repo_root / "frontend/web/src/router/index.ts").read_text(encoding="utf-8")
    login = (repo_root / "frontend/web/src/views/LoginView.vue").read_text(encoding="utf-8")

    require("repo-host-network", "hostNetwork: true" in deployment, "OAuth loopback tunnel requires hostNetwork")
    require(
        "repo-oauth-proxy-env",
        deployment.count("value: http://127.0.0.1:18080") == 2,
        "migrate and API containers must both use the local OAuth tunnel",
    )
    require(
        "repo-relay-loopback",
        "V2MD_OAUTH_RELAY_HOST=127.0.0.1" in relay
        and "V2MD_OAUTH_RELAY_ALLOWED_IPS=127.0.0.1" in relay,
        "relay must only accept loopback clients",
    )
    require(
        "repo-ssh-tunnel",
        "ExitOnForwardFailure=yes" in tunnel
        and "-L 127.0.0.1:18080:127.0.0.1:18080" in tunnel,
        "SSH tunnel must fail closed and bind only on loopback",
    )
    require(
        "repo-sensitive-routes",
        all(rule in nginx for rule in (
            "location = /docs         { return 403; }",
            "location = /openapi.json { return 403; }",
            "location = /redoc        { return 403; }",
            "location = /queue        { return 404; }",
        )),
        "Nginx must block docs/schema/redoc and the internal queue endpoint",
    )
    require(
        "repo-production-schema",
        'docs_url=None if _production else "/docs"' in api
        and 'openapi_url=None if _production else "/openapi.json"' in api,
        "FastAPI docs and schema must be disabled in production",
    )
    require(
        "repo-google-httpx-transport",
        "class _GoogleHttpxRequest" in providers and "httpx.Client(proxy=proxy_url or None)" in providers,
        "Google certificate retrieval must use the proven HTTPX proxy path",
    )
    require(
        "repo-legal-routes",
        all(value in router for value in ("path: '/terms'", "path: '/privacy'"))
        and 'to="/terms"' in login
        and 'to="/privacy"' in login,
        "login must expose public terms and privacy routes",
    )
    return failures


def self_test() -> None:
    good = {
        "postgres": {"status": "healthy"},
        "schema": {"version": 4},
        "api": {"status": "healthy", "version": "0.2.0"},
        "google_token_probe": {"network_ok": True, "http_status": 404},
        "oauth_relay": {"topology": "host-network-loopback", "container_reachable_host": "127.0.0.1", "port": 18080, "container_probe_http_status": 404, "tunnel_service_active": True},
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
        "public_routes": {"healthz": 200, "terms": 200, "privacy": 200, "docs": 403, "openapi_json": 403, "redoc": 403, "queue": 404},
    }
    assert evaluate(good, require_browser=True) == []
    bad = json.loads(json.dumps(good))
    bad["schema"]["version"] = 3
    bad["container_env"]["present_forbidden"] = ["HTTPS_PROXY"]
    bad["oauth_relay"]["tunnel_service_active"] = False
    bad["config"]["mode"] = "0644"
    bad["browser_google_login"]["status"] = "pending"
    bad["public_routes"]["docs"] = 200
    failures = evaluate(bad, require_browser=True)
    assert {item.split(":", 1)[0] for item in failures} >= {
        "prod-schema-version",
        "prod-proxy-retirement",
        "prod-oauth-relay-topology",
        "prod-config-mode",
        "prod-browser-google-login",
        "prod-public-routes",
    }
    repo_root = Path(__file__).resolve().parents[1]
    assert evaluate_repository(repo_root) == []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", nargs="?", type=Path)
    parser.add_argument("--require-browser", action="store_true", help="fail until real Google browser login and GET /me pass")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        print("PASS production readiness gate self-test")
        return 0
    if args.evidence is None:
        parser.error("evidence JSON path is required unless --self-test is used")
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        failures = evaluate(evidence, require_browser=args.require_browser)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"FAIL evidence: {exc}", file=sys.stderr)
        return 2
    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1
    print("PASS production readiness evidence" + (" (browser required)" if args.require_browser else " (infrastructure phase)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

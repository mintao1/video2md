#!/usr/bin/env python3
"""Evaluate sanitized production deployment evidence; never reads secret values."""

from __future__ import annotations

import argparse
import json
import re
import stat
import sys
from pathlib import Path
from typing import Any

FORBIDDEN_ENV = {"17898", "HTTP_PROXY", "HTTPS_PROXY", "V2PDF_PROXY"}


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
    expect("prod-schema-version", isinstance(schema_version, int) and schema_version >= 6, "schema version must be >= 6 (tenant RLS + encrypted LLM keys)")

    roles = nested(evidence, "database_roles")
    expect(
        "prod-db-roles",
        isinstance(roles, dict)
        and roles.get("api_role") == "video2md_app"
        and roles.get("migrate_role") == "video2md_migrate"
        and roles.get("api_login") is True
        and roles.get("migrate_login") is True
        and roles.get("api_superuser") is False
        and roles.get("migrate_superuser") is False
        and roles.get("api_bypass_rls") is False
        and roles.get("migrate_bypass_rls") is False
        and roles.get("rls_enabled") is True
        and roles.get("app_dsn_tls") is True
        and roles.get("migrate_dsn_tls") is True,
        "API/migrate roles, RLS flags and TLS-verified DSNs must be attested",
    )

    backup = nested(evidence, "backup")
    expect(
        "prod-backup-protection",
        isinstance(backup, dict)
        and backup.get("encrypted") is True
        and backup.get("permissions_hardened") is True
        and backup.get("remote_configured") is True
        and backup.get("remote_tls") is True
        and backup.get("retention_configured") is True,
        "backups must be encrypted, permission-hardened, remote, TLS-protected and retained by policy",
    )

    observability = nested(evidence, "observability")
    expect(
        "prod-observability-protection",
        isinstance(observability, dict)
        and observability.get("provider") == "langfuse"
        and observability.get("project") == "production"
        and observability.get("tls") is True
        and observability.get("prompt_redaction") is True
        and observability.get("acl_verified") is True
        and observability.get("retention_configured") is True,
        "Langfuse production project must use TLS, redaction, ACL and retention controls",
    )
    expect("prod-api-health", nested(evidence, "api.status") == "healthy", "API must be healthy")
    expect("prod-release-version", nested(evidence, "api.version") == "0.2.0", "/healthz version must equal 0.2.0")

    release = nested(evidence, "release")
    expected_commit = release.get("expected_commit") if isinstance(release, dict) else None
    source_commit = release.get("source_commit") if isinstance(release, dict) else None
    expected_image = release.get("expected_image") if isinstance(release, dict) else None
    deployed_image = release.get("deployed_image") if isinstance(release, dict) else None
    expect(
        "prod-release-provenance",
        isinstance(expected_commit, str)
        and re.fullmatch(r"[0-9a-f]{40}", expected_commit) is not None
        and source_commit == expected_commit
        and release.get("branch") == "main"
        and isinstance(expected_image, str)
        and bool(expected_image)
        and deployed_image == expected_image,
        "deployed source SHA and image must exactly match the intended main release",
    )

    runtime = nested(evidence, "runtime")
    expect(
        "prod-runtime-stability",
        isinstance(runtime, dict)
        and runtime.get("pod_phase") == "Running"
        and runtime.get("pod_ready") is True
        and runtime.get("restart_count") == 0
        and runtime.get("recent_error_count") == 0,
        "production pod must be ready with zero restarts and no recent errors",
    )

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
        and routes.get("unauthenticated_me") == 401
        and routes.get("docs") == 403
        and routes.get("openapi_json") == 403
        and routes.get("redoc") == 403
        and routes.get("queue") == 404,
        "health/legal routes must stay public while docs/schema/redoc/queue remain blocked",
    )

    return failures


def _google_httpx_proxy_transport(providers: str) -> bool:
    """Accept equivalent local-variable formatting without weakening the proxy check."""
    class_start = providers.find("class _GoogleHttpxRequest")
    if class_start < 0:
        return False
    next_class = providers.find("\nclass ", class_start + 1)
    request_class = providers[class_start:next_class if next_class >= 0 else None]
    return re.search(
        r"httpx\.Client\(\s*proxy\s*=\s*(?:self\.proxy_url|proxy_url)\s+or\s+None\s*\)",
        request_class,
    ) is not None and (
        "proxy_url = self.proxy_url" in request_class or "proxy=self.proxy_url" in request_class
    )


def evaluate_repository(repo_root: Path) -> list[str]:
    """Check deployable repository contracts without network or secret access."""
    failures: list[str] = []

    def require(label: str, condition: bool, detail: str) -> None:
        if not condition:
            failures.append(f"{label}: {detail}")

    deployment = (repo_root / "deploy/k8s/base/deployment-api.yaml").read_text(encoding="utf-8")
    compose = (repo_root / "deploy/docker-compose.prod.yml").read_text(encoding="utf-8")
    package_root = repo_root / "backend/video2pdf"
    if not package_root.exists():
        package_root = repo_root / "backend/video2md"
    api_start = compose.find("\n  api:")
    api_end = compose.find("\nvolumes:", api_start)
    api_compose = compose[api_start:api_end if api_end >= 0 else None]
    role_bootstrap = (repo_root / "deploy/postgres/000-provision-roles.sh").read_text(encoding="utf-8")
    role_init = (repo_root / "deploy/postgres/001-roles.sql").read_text(encoding="utf-8")
    deploy_script = (repo_root / "scripts/deploy.sh").read_text(encoding="utf-8")
    migration = (package_root / "migrations/0005_tenant_rls.sql").read_text(encoding="utf-8")
    key_migration = (package_root / "migrations/0006_llm_key_ciphertext.sql").read_text(encoding="utf-8")
    key_store = (package_root / "llm_settings.py").read_text(encoding="utf-8")
    config_source = (package_root / "config.py").read_text(encoding="utf-8")
    backup = (repo_root / "scripts/backup.sh").read_text(encoding="utf-8")
    observability = (package_root / "observability.py").read_text(encoding="utf-8")
    nginx = (repo_root / "deploy/nginx.conf").read_text(encoding="utf-8")
    relay = (repo_root / "deploy/oauth-relay/oauth-relay.service").read_text(encoding="utf-8")
    tunnel = (repo_root / "deploy/oauth-relay/oauth-tunnel.service").read_text(encoding="utf-8")
    api = (repo_root / "backend/video2pdf/api.py").read_text(encoding="utf-8")
    providers = (repo_root / "backend/acctkit/providers.py").read_text(encoding="utf-8")
    router = (repo_root / "frontend/web/src/router/index.ts").read_text(encoding="utf-8")
    login = (repo_root / "frontend/web/src/views/BrandView.vue").read_text(encoding="utf-8")

    require("repo-host-network", "hostNetwork: true" in deployment, "OAuth loopback tunnel requires hostNetwork")
    require(
        "repo-oauth-proxy-env",
        deployment.count(
            "- name: V2MD_GOOGLE_OAUTH_PROXY_URL\n"
            "              value: http://127.0.0.1:18080"
        ) == 2,
        "migrate and API containers must both use the local OAuth tunnel",
    )
    require(
        "repo-relay-loopback",
        "V2PDF_OAUTH_RELAY_HOST=127.0.0.1" in relay
        and "V2PDF_OAUTH_RELAY_ALLOWED_IPS=127.0.0.1" in relay,
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
        _google_httpx_proxy_transport(providers),
        "Google certificate retrieval must use the proven HTTPX proxy path",
    )
    require(
        "repo-legal-routes",
        all(value in router for value in ("path: '/terms'", "path: '/privacy'"))
        and 'to="/terms"' in login
        and 'to="/privacy"' in login,
        "login must expose public terms and privacy routes",
    )
    require("repo-brand-home", "path: '/'" in router and "BrandView.vue" in router
            and "path: '/login'" not in router,
            "the brand home must own sign-in; legacy login route must remain removed")
    require(
        "repo-artifact-mode",
        deployment.count("- name: V2MD_ARTIFACT_MODE") == 2
        and "value: deliver" in deployment
        and "V2MD_ARTIFACT_MODE: deliver" in compose,
        "migrate/API manifests must pin deliver mode",
    )
    require(
        "repo-database-roles",
        all(text in migration for text in (
            "ENABLE ROW LEVEL SECURITY", "REVOKE ALL ON TABLE",
            "REVOKE INSERT, UPDATE, DELETE ON v2pdf_schema_version FROM video2md_app",
        ))
        and all(text in (role_init + role_bootstrap) for text in ("video2md_app", "video2md_migrate"))
        and "V2MD_PG_DSN:" not in compose,
        "RLS/role migration must exist and compose must not inject a superuser DSN fallback",
    )
    require(
        "repo-api-secret-scope",
        api_start >= 0 and "env_file:" not in api_compose
        and "V2MD_SECRETS_FILE: /run/video2md.env" in api_compose,
        "API must read the mounted secret file without inheriting bootstrap/database passwords as environment variables",
    )
    require(
        "repo-role-bootstrap",
        all(text in role_bootstrap for text in (
            "video2md_app", "video2md_migrate", "video2md_owner", "NOSUPERUSER", "NOBYPASSRLS",
            "VIDEO2MD_APP_PASSWORD", "VIDEO2MD_MIGRATE_PASSWORD",
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
            "GRANT USAGE, CREATE ON SCHEMA public TO video2md_migrate",
        ))
        and "/docker-entrypoint-initdb.d/000-provision-roles.sh" in deploy_script
        and deploy_script.count("python3 scripts/check_production_readiness.py --self-test") >= 2,
        "role bootstrap must be repeatable, secret-backed and invoked before migrate",
    )
    require(
        "repo-credential-encryption",
        "AESGCM" in key_store
        and ("V2MD_LLM_SETTINGS_KEY" in config_source or "V2PDF_LLM_SETTINGS_KEY" in config_source)
        and "v1:aesgcm:" in key_store
        and "v2pdf_llm_settings_ciphertext_ck" in key_migration,
        "LLM credentials must use AES-GCM envelope encryption with a migration constraint",
    )
    require(
        "repo-observability-redaction",
        "OTEL_INCLUDE_CONTENT" in config_source
        and "config.OTEL_INCLUDE_CONTENT" in (package_root / "llm.py").read_text(encoding="utf-8")
        and "otel_attrs" in observability
        and "trace_content" in observability
        and deployment.count(
            "- name: V2MD_OTEL_INCLUDE_CONTENT\n"
            "              value: \"off\""
        ) == 2
        and "V2MD_OTEL_INCLUDE_CONTENT: off" in compose,
        "production observability must gate prompt/completion content while retaining metrics",
    )
    require(
        "repo-backup-protection",
        "V2MD_BACKUP_AGE_RECIPIENT" in backup
        and "age -r" in backup
        and "V2MD_BACKUP_RETENTION_DAYS" in backup
        and "-mtime +\"$BACKUP_RETENTION_DAYS\"" in backup
        and "rclone sync" in backup,
        "backups must be encrypted, retained and synchronized as ciphertext",
    )
    return failures


def self_test() -> None:
    good = {
        "postgres": {"status": "healthy"},
        "schema": {"version": 6},
        "database_roles": {
            "api_role": "video2md_app", "migrate_role": "video2md_migrate",
            "api_login": True, "migrate_login": True, "api_superuser": False,
            "migrate_superuser": False,
            "api_bypass_rls": False, "migrate_bypass_rls": False, "rls_enabled": True,
            "app_dsn_tls": True, "migrate_dsn_tls": True,
        },
        "backup": {
            "encrypted": True, "permissions_hardened": True,
            "remote_configured": True, "remote_tls": True,
            "retention_configured": True,
        },
        "observability": {
            "provider": "langfuse", "project": "production", "tls": True,
            "prompt_redaction": True, "acl_verified": True,
            "retention_configured": True,
        },
        "api": {"status": "healthy", "version": "0.2.0"},
        "release": {
            "branch": "main",
            "expected_commit": "a83147d5be0d75253804e74663fefccbe70a904b",
            "source_commit": "a83147d5be0d75253804e74663fefccbe70a904b",
            "expected_image": "video2pdf:0.2.0-hotfix-20261021g",
            "deployed_image": "video2pdf:0.2.0-hotfix-20261021g",
        },
        "runtime": {"pod_phase": "Running", "pod_ready": True, "restart_count": 0, "recent_error_count": 0},
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
        "public_routes": {"healthz": 200, "terms": 200, "privacy": 200, "unauthenticated_me": 401, "docs": 403, "openapi_json": 403, "redoc": 403, "queue": 404},
    }
    assert evaluate(good, require_browser=True) == []
    bad = json.loads(json.dumps(good))
    bad["schema"]["version"] = 3
    bad["release"]["source_commit"] = "0" * 40
    bad["runtime"]["restart_count"] = 2
    bad["container_env"]["present_forbidden"] = ["HTTPS_PROXY"]
    bad["oauth_relay"]["tunnel_service_active"] = False
    bad["config"]["mode"] = "0644"
    bad["browser_google_login"]["status"] = "pending"
    bad["public_routes"]["docs"] = 200
    failures = evaluate(bad, require_browser=True)
    assert {item.split(":", 1)[0] for item in failures} >= {
        "prod-schema-version",
        "prod-release-provenance",
        "prod-runtime-stability",
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

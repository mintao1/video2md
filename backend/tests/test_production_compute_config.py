"""Production compute routing must use the current per-tenant Colab path."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "deploy/k8s/base/deployment-api.yaml"
RLS_MIGRATION = ROOT / "backend/video2md/migrations/0005_tenant_rls.sql"
ROLE_INIT = ROOT / "deploy/postgres/001-roles.sql"


def _api_env() -> dict[str, str]:
    document = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    container = next(item for item in document["spec"]["template"]["spec"]["containers"]
                     if item["name"] == "api")
    return {item["name"]: item["value"] for item in container["env"] if "value" in item}


def test_production_compute_routes_are_colab_cli() -> None:
    env = _api_env()
    assert env["V2MD_ARTIFACT_MODE"] == "deliver"
    assert {env[name] for name in ("V2MD_REMOTE", "V2MD_OCR_REMOTE", "V2MD_CLIPS_REMOTE")} == {"colab-cli"}
    assert env["V2MD_CLIPS_SOURCE"] == "remote"
    assert env["V2MD_COLAB_BIN"] == "/app/.venv/bin/colab"


def test_production_api_image_contains_tenant_colab_runtime() -> None:
    document = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    container = next(item for item in document["spec"]["template"]["spec"]["containers"]
                     if item["name"] == "api")
    assert container["image"] == "video2md:0.2.0-colab-tenant-20261026d"


def test_production_compute_config_does_not_depend_on_retired_local_pool() -> None:
    env = _api_env()
    assert all("127.0.0.1:9700" not in env[name]
               for name in ("V2MD_REMOTE", "V2MD_OCR_REMOTE", "V2MD_CLIPS_REMOTE"))


def test_tenant_migration_enables_rls_and_separate_roles() -> None:
    sql = RLS_MIGRATION.read_text(encoding="utf-8")
    roles = ROLE_INIT.read_text(encoding="utf-8")
    assert "CREATE ROLE video2md_app NOLOGIN NOBYPASSRLS" in roles
    assert "CREATE ROLE video2md_migrate NOLOGIN NOBYPASSRLS" in roles
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY v2md_runs_tenant" in sql
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC" in sql
    assert "PRIMARY KEY (owner, fp)" in sql

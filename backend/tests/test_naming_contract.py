"""Canonical video2md package and PostgreSQL relation naming tests."""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
DSN = os.environ.get("V2MD_TEST_PG_DSN")


def _run(code: str, **env: str) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith("V2MD_")}
    return subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env={**base, **env},
                          capture_output=True, text=True, timeout=60)


def test_canonical_entry_and_environment():
    r = _run("from video2md.config import ENV; print(ENV['V2MD_PORT'])", V2MD_PORT="9123")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "9123"


def test_canonical_import_path_is_available():
    r = _run("import video2md.config as config; print(config.ENV.get('V2MD_ENV', 'development'))")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "development"


@pytest.mark.skipif(not DSN, reason="requires disposable V2MD_TEST_PG_DSN")
def test_migrate_renames_old_tables_and_keeps_rows():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from video2md import pg
    from video2md.pgmigrate import SCHEMA_LATEST, apply_migrations, validate_schema

    db = f"rename_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'video2md_app') THEN
                CREATE ROLE video2md_app;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'video2md_migrate') THEN
                CREATE ROLE video2md_migrate;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'video2md_owner') THEN
                CREATE ROLE video2md_owner;
            END IF;
        END
        $$;
        """)
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db)))
    try:
        dsn = make_conninfo(DSN, dbname=db)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE TABLE v2pdf_schema_version (version integer PRIMARY KEY, "
                         "name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())")
            conn.execute("INSERT INTO v2pdf_schema_version (version, name) "
                         "SELECT g, 'legacy' FROM generate_series(1, %s) g", (SCHEMA_LATEST,))
            conn.execute("CREATE TABLE v2pdf_runs (run_id text PRIMARY KEY, record jsonb NOT NULL)")
            conn.execute("CREATE INDEX v2pdf_runs_extra_idx ON v2pdf_runs (run_id)")
            conn.execute("ALTER TABLE v2pdf_runs ADD COLUMN owner text NOT NULL DEFAULT ''")
            conn.execute("CREATE FUNCTION v2pdf_tenant_visible(text) RETURNS boolean "
                         "LANGUAGE sql IMMUTABLE AS 'SELECT true'")
            conn.execute("ALTER TABLE v2pdf_runs ENABLE ROW LEVEL SECURITY")
            conn.execute("CREATE POLICY v2pdf_runs_tenant ON v2pdf_runs "
                         "USING (v2pdf_tenant_visible(owner))")
            conn.execute("INSERT INTO v2pdf_runs (run_id, record) VALUES ('r1', '{}')")
        pool = pg.make_pool(dsn)
        try:
            assert apply_migrations(pool) == []
            validate_schema(pool)
            with pool.connection() as conn:
                names = {r[0] for r in conn.execute(
                    "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = current_schema()").fetchall()}
                rows = conn.execute("SELECT run_id FROM v2md_runs").fetchall()
                policy = conn.execute("SELECT policyname FROM pg_policies "
                                      "WHERE tablename = 'v2md_runs'").fetchall()
                function = conn.execute("SELECT to_regprocedure('v2md_tenant_visible(text)')").fetchone()[0]
            assert not {n for n in names if n.startswith("v2pdf_")}
            assert {"v2md_runs", "v2md_runs_pkey", "v2md_runs_extra_idx",
                    "v2md_schema_version"} <= names
            assert rows == [("r1",)]
            assert policy == [("v2md_runs_tenant",)]
            assert function is not None
            assert apply_migrations(pool) == []
        finally:
            pool.close()
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(db)))

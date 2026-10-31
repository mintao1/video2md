"""video2md canonical naming with video2pdf environment/import compatibility."""
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
    base = {k: v for k, v in os.environ.items() if not k.startswith(("V2MD_", "V2PDF_"))}
    return subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env={**base, **env},
                          capture_output=True, text=True, timeout=60)


def test_legacy_env_fills_new_keys_and_warns():
    r = _run("import os; from video2md.config import ENV; "
             "print(ENV['V2MD_PORT'], os.environ['V2MD_PORT'])", V2PDF_PORT="9123")
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["9123", "9123"]
    assert "V2PDF_PORT" in r.stderr


def test_new_env_wins_over_legacy():
    r = _run("from video2md.config import ENV; print(ENV['V2MD_PORT'])",
             V2PDF_PORT="9123", V2MD_PORT="9456")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "9456"


def test_legacy_import_path_is_the_same_module():
    r = _run("import video2pdf.config as a, video2md.config as b; print(a is b)")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "True"


@pytest.mark.skipif(not DSN, reason="requires disposable V2MD_TEST_PG_DSN")
def test_migrate_renames_legacy_tables_and_keeps_rows():
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from video2md import pg
    from video2md.pgmigrate import SCHEMA_LATEST, apply_migrations, validate_schema

    db = f"rename_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db)))
    try:
        dsn = make_conninfo(DSN, dbname=db)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("CREATE TABLE v2md_schema_version (version integer PRIMARY KEY, "
                         "name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())")
            conn.execute("INSERT INTO v2md_schema_version (version, name) "
                         "SELECT g, 'legacy' FROM generate_series(1, %s) g", (SCHEMA_LATEST,))
            conn.execute("CREATE TABLE v2md_runs (run_id text PRIMARY KEY, record jsonb NOT NULL)")
            conn.execute("CREATE INDEX v2md_runs_extra_idx ON v2md_runs (run_id)")
            conn.execute("INSERT INTO v2md_runs VALUES ('r1', '{}')")
        pool = pg.make_pool(dsn)
        try:
            assert apply_migrations(pool) == []
            validate_schema(pool)
            with pool.connection() as conn:
                names = {r[0] for r in conn.execute(
                    "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = current_schema()").fetchall()}
                rows = conn.execute("SELECT run_id FROM v2pdf_runs").fetchall()
            assert not {n for n in names if n.startswith("v2md_")}
            assert {"v2pdf_runs", "v2pdf_runs_pkey", "v2pdf_runs_extra_idx",
                    "v2pdf_schema_version"} <= names
            assert rows == [("r1",)]
            assert apply_migrations(pool) == []  # 二次运行无改动
        finally:
            pool.close()
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(db)))

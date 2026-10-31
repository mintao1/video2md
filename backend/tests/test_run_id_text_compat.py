"""Regression coverage for legacy short run ids in PostgreSQL.

Production databases created before canonical UUID generation may still have
``uuid`` columns.  The API deliberately keeps ``run_id`` opaque text because
historical rows include short ids.  This test guards the migration contract
without requiring a live database; the PG integration suite applies the same
SQL against its disposable schema when configured.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "video2md/migrations/0007_run_id_text_compat.sql"


def test_legacy_run_id_migration_is_registered_and_widens_both_columns() -> None:
    from video2md import pgmigrate

    assert pgmigrate.SCHEMA_LATEST == 7
    assert (7, "0007_run_id_text_compat.sql") in pgmigrate.MIGRATIONS

    sql = MIGRATION.read_text(encoding="utf-8")
    assert "table_name = 'v2md_runs'" in sql
    assert "table_name = 'v2md_prefs'" in sql
    assert sql.count("ALTER COLUMN run_id TYPE text USING run_id::text") == 2


def test_legacy_run_id_migration_mentions_short_id_failure_mode() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "invalid input syntax for type uuid" in sql
    assert "historical imports" in sql

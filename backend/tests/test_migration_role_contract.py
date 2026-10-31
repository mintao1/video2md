"""Regression tests for the production migration-role split.

The API connection is DML-only while ``python -m video2md migrate`` must use
the dedicated DDL-capable DSN.  Keep this test entirely in-process so it does
not need a database or expose any credential value.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(code: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "V2MD_ENV": "development",
        "V2MD_PG_DSN": "postgresql://video2md_app:app@db/v2md",
        "V2MD_PG_MIGRATE_DSN": "postgresql://video2md_migrate:migrate@db/v2md",
    }
    return subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True, timeout=30)


def test_migrate_dsn_prefers_dedicated_role() -> None:
    result = _run(
        "from video2md import pg; "
        "assert pg.pg_migrate_dsn() == "
        "'postgresql://video2md_migrate:migrate@db/v2md'"
    )
    assert result.returncode == 0, result.stderr


def test_migrate_entrypoint_passes_dedicated_dsn_to_pool() -> None:
    result = _run(
        "import sys; "
        "from video2md import __main__, pg, pgmigrate; "
        "seen=[]; "
        "Pool=type('Pool', (), {'close': lambda self: None}); "
        "pg.pg_enabled=lambda: True; "
        "pg.make_pool=lambda dsn: seen.append(dsn) or Pool(); "
        "pgmigrate.apply_migrations=lambda pool: []; "
        "pgmigrate.validate_schema=lambda pool: None; "
        "sys.argv=['video2md','migrate']; __main__.main(); "
        "assert seen == ['postgresql://video2md_migrate:migrate@db/v2md']"
    )
    assert result.returncode == 0, result.stderr


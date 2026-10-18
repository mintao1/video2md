"""Web control-plane concurrency against a disposable test PostgreSQL database."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from video2md import pg, pgmigrate
from video2md.pgstore import PostgresRunsStore
from video2md.prefs import PostgresPrefsStore

DSN = os.environ.get("V2MD_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="requires disposable V2MD_TEST_PG_DSN")


@pytest.fixture
def stores():
    pool = pg.make_pool(DSN)
    pgmigrate.apply_migrations(pool)
    uid = "web-test-" + uuid.uuid4().hex
    try:
        yield PostgresRunsStore(pool), PostgresPrefsStore(pool), uid
    finally:
        with pool.connection() as conn:
            conn.execute("DELETE FROM v2md_runs WHERE owner=%s", (uid,))
            conn.execute("DELETE FROM v2md_prefs WHERE uid=%s", (uid,))
        pool.close()


def test_retry_has_one_winner_and_preserves_existing_artifacts(stores):
    runs, _prefs, uid = stores
    rid = uid + "-retry"
    runs.merge({"run_id": rid, "owner": uid, "status": "error", "created": "2026-01-01",
                "artifacts": {"lesson": {"note": "keep.md"}}})
    assert runs.patch_owned(rid, "other", ("error",), {"status": "created"}) is None
    def claim(_):
        return runs.patch_owned(rid, uid, ("error",), {"status": "created", "cancel_requested": False})
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(claim, range(4)))
    assert sum(row is not None for row in results) == 1
    row = runs.load()[rid]
    assert row["status"] == "created" and row["artifacts"]["lesson"]["note"] == "keep.md"


def test_priority_and_terminal_deletion_are_owned_and_conditional(stores):
    runs, _prefs, uid = stores
    rid = uid + "-priority"
    row = {"run_id": rid, "owner": uid, "status": "queued", "created": "2026-01-01", "cost_usd": 2.5}
    runs.merge(row)
    assert runs.patch_owned(rid, uid, ("queued",), {"queue_priority": 123})["queue_priority"] == 123
    assert not runs.delete_terminal(rid, uid)
    assert runs.patch_owned(rid, uid, ("queued",), {"status": "error"}) is not None
    assert not runs.delete_terminal(rid, "other")
    assert runs.delete_terminal(rid, uid)
    assert runs.load()[rid]["deleted"] is True
    assert runs.patch_owned(rid, uid, ("error",), {"status": "created"}) is None
    row["status"] = "error"
    runs.merge(row)  # a late settlement of a stale snapshot may not restore visibility
    assert runs.load()[rid]["deleted"] is True and runs.load()[rid]["cost_usd"] == 2.5


def test_concurrent_preference_fields_merge_and_other_users_see_nothing(stores):
    _runs, prefs, uid = stores
    with ThreadPoolExecutor(max_workers=2) as workers:
        a = workers.submit(prefs.upsert, uid, "lesson", fav=True)
        b = workers.submit(prefs.upsert, uid, "lesson", progress=42)
        a.result()
        b.result()
    assert prefs.list_uid(uid) == {"lesson": {"fav": True, "progress": 42}}
    assert prefs.list_uid(uid + "-other") == {}


def test_llm_settings_pg_isolates_and_preserves_keys(stores, monkeypatch):
    from video2md import llm_settings
    runs, _prefs, uid = stores
    monkeypatch.setattr(llm_settings, "_pool", lambda: runs.pool)
    try:
        assert llm_settings.load(uid) == {}
        llm_settings.update(uid, base_url="https://example.test/v1", api_key="pg-test-key", model="test")
        llm_settings.update(uid, base_url="https://example.test/v2", model="new")
        assert llm_settings.load(uid) == {"base_url": "https://example.test/v2",
                                          "api_key": "pg-test-key", "model": "new"}
        assert llm_settings.load(uid + "-other") == {}
        assert "pg-test-key" not in str(llm_settings.public(llm_settings.load(uid)))
        llm_settings.update(uid, clear=True)
        assert llm_settings.load(uid) == {}
    finally:
        llm_settings.update(uid, clear=True)

"""Audited historical corrections must be atomic, isolated and repeatable."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('usage_backfill', ROOT / 'scripts/backfill_opencode_usage.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def evidence():
    record = {'run_id': 'fixture', 'owner': 'owner', 'status': 'partial', 'usage': {
        'gpu_s': 10.5, 'llm_in_tok': 3, 'llm_out_tok': 4}, 'cost_usd': 0.000002,
        'cost_pricing': {'llm_in_mtok': .15, 'llm_cached_in_mtok': .03, 'llm_out_mtok': .5,
                         'gpu_min': 0, 'gpu_policy': 'free', 'currency': 'USD'},
        'title': 'preserve', 'span_log': [{'event': 'existing'}]}
    export = {'items': [{'sessionID': 'video2md-fixture', 'requestID': 'req1',
                        'inputTokens': 3, 'outputTokens': 4, 'cacheReadTokens': 0,
                        'cacheWriteTokens': 0, 'cost': .00000245},
                       {'sessionID': 'video2md-fixture', 'requestID': 'req2',
                        'inputTokens': 2, 'outputTokens': 6, 'cacheReadTokens': 10,
                        'cacheWriteTokens': 0, 'cost': .0000036,
                        'requestHeaders': {'authorization': 'SECRET_CANARY'}}], 'truncated': False}
    return record, json.dumps(export).encode()


def test_plan_and_patch_preserve_gpu_and_unrelated_fields_and_are_idempotent():
    record, raw = evidence()
    plan = mod.prepare(raw, record)
    corrected, changed = mod.correct(record, plan)
    assert changed
    assert corrected['usage'] == {'gpu_s': 10.5, 'llm_in_tok': 15, 'llm_out_tok': 10, 'llm_cached_in_tok': 10}
    assert corrected['cost_usd'] == .000006
    assert corrected['title'] == record['title'] and corrected['span_log'] == record['span_log']
    assert record['usage']['llm_in_tok'] == 3
    assert len(corrected['usage_corrections']) == 1
    assert mod.correct(corrected, plan) == (corrected, False)
    assert 'SECRET_CANARY' not in json.dumps(plan)


@pytest.mark.parametrize('field,value', [('status', 'running'), ('owner', 'another'),
                                         ('cost_usd', 1), ('deleted', True), ('title', 'concurrent edit')])
def test_compare_and_swap_rejects_stale_or_wrong_record(field, value):
    record, raw = evidence()
    plan = mod.prepare(raw, record)
    record[field] = value
    with pytest.raises(ValueError):
        mod.correct(record, plan)


@pytest.mark.parametrize('mutation', ['truncated', 'duplicate', 'wrong-session', 'negative', 'price', 'active'])
def test_prepare_rejects_invalid_evidence(mutation):
    record, raw = evidence()
    export = json.loads(raw)
    if mutation == 'truncated':
        export['truncated'] = True
    elif mutation == 'duplicate':
        export['items'][1]['requestID'] = 'req1'
    elif mutation == 'wrong-session':
        for row in export['items']:
            row['sessionID'] = 'video2md-other'
    elif mutation == 'negative':
        export['items'][1]['outputTokens'] = -1
    elif mutation == 'price':
        record['cost_pricing']['llm_out_mtok'] = .8
    else:
        record['status'] = 'running'
    with pytest.raises(ValueError):
        mod.prepare(json.dumps(export).encode(), record)


def test_tampered_plan_and_changed_corrected_record_rejected():
    record, raw = evidence()
    plan = mod.prepare(raw, record)
    bad = copy.deepcopy(plan)
    bad['target_usage']['llm_out_tok'] += 1
    with pytest.raises(ValueError):
        mod.correct(record, bad)
    corrected, _ = mod.correct(record, plan)
    corrected['cost_usd'] += 1
    with pytest.raises(ValueError):
        mod.correct(corrected, plan)


def test_json_store_atomic_backup_other_run_and_repeat(tmp_path, monkeypatch):
    from video2pdf import runs
    record, raw = evidence()
    ledger = tmp_path / 'runs.json'
    other = {'run_id': 'other', 'status': 'running', 'usage': {'llm_in_tok': 900}}
    ledger.write_text(json.dumps({'fixture': record, 'other': other}))
    monkeypatch.setattr(runs, 'RUNS_FILE', ledger)
    monkeypatch.setattr(runs, '_RUNS_LOCKFILE', tmp_path / 'runs.lock')
    monkeypatch.setattr(runs, '_runs_backend', lambda: None)
    plan = mod.prepare(raw, record)
    result = mod.apply(plan, tmp_path / 'private-backup')
    assert result['changed'] is True
    assert json.loads(ledger.read_text())['other'] == other
    backup = Path(result['backup'])
    assert json.loads(backup.read_text()) == record
    assert backup.stat().st_mode & 0o777 == 0o600
    assert mod.apply(plan, tmp_path / 'private-backup')['changed'] is False


def test_failed_backup_never_mutates_ledger(tmp_path, monkeypatch):
    from video2pdf import runs
    record, raw = evidence()
    ledger = tmp_path / 'runs.json'
    original = json.dumps({'fixture': record})
    ledger.write_text(original)
    monkeypatch.setattr(runs, 'RUNS_FILE', ledger)
    monkeypatch.setattr(runs, '_RUNS_LOCKFILE', tmp_path / 'runs.lock')
    monkeypatch.setattr(runs, '_runs_backend', lambda: None)
    forbidden = tmp_path / 'file'
    forbidden.write_text('not a directory')
    with pytest.raises(OSError):
        mod.apply(mod.prepare(raw, record), forbidden)
    assert ledger.read_text() == original


@pytest.mark.parametrize('stale', [False, True])
def test_postgres_uses_locked_row_transaction_and_leaves_other_fields(tmp_path, monkeypatch, stale):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from video2pdf import runs
    record, raw = evidence()
    plan = mod.prepare(raw, record)
    if stale:
        record['title'] = 'concurrent rename'
    calls = []

    class Connection:
        def transaction(self):
            calls.append(('transaction',))
            return nullcontext()

        def execute(self, sql, args):
            calls.append((sql, args))
            return SimpleNamespace(fetchone=lambda: (record,))

    be = SimpleNamespace(pool=SimpleNamespace(connection=lambda: nullcontext(Connection())))
    monkeypatch.setattr(runs, '_runs_backend', lambda: be)
    if stale:
        with pytest.raises(ValueError):
            mod.apply(plan, tmp_path / 'backup')
        assert not any(c[0].startswith('UPDATE') for c in calls)
    else:
        assert mod.apply(plan, tmp_path / 'backup')['changed']
        update = next(c for c in calls if c[0].startswith('UPDATE'))
        updated = update[1][0].obj
        assert updated['span_log'] == record['span_log'] and updated['owner'] == record['owner']
        assert update[1][1] == record['run_id']
    assert calls[0] == ('transaction',)
    assert 'FOR UPDATE' in calls[1][0]


def test_backfill_check_is_in_ci():
    assert 'tests/test_usage_backfill.py' in (ROOT / '.github/workflows/ci.yml').read_text()


def test_resumed_run_settles_new_usage_without_losing_correction(tmp_path, monkeypatch):
    from video2pdf import runs
    record, raw = evidence()
    corrected, _ = mod.correct(record, mod.prepare(raw, record))
    ledger = tmp_path / 'runs.json'
    ledger.write_text(json.dumps({'fixture': corrected}))
    monkeypatch.setattr(runs, 'RUNS_FILE', ledger)
    monkeypatch.setattr(runs, '_RUNS_LOCKFILE', tmp_path / 'runs.lock')
    monkeypatch.setattr(runs, '_runs_backend', lambda: None)
    monkeypatch.setattr(runs, 'PRICE_LLM_IN_MTOK', .15)
    monkeypatch.setattr(runs, 'PRICE_LLM_CACHED_IN_MTOK', .03)
    monkeypatch.setattr(runs, 'PRICE_LLM_OUT_MTOK', .5)
    monkeypatch.setattr(runs, 'PRICE_GPU_MIN', 0)
    monkeypatch.setattr(runs, '_USAGE', {'fixture': {'llm_in_tok': 1000, 'llm_out_tok': 2000,
                                                   'llm_cached_in_tok': 100, 'gpu_s': 12.0}})
    result = runs.settle_cost('fixture')
    assert result['usage']['llm_out_tok'] == 2010
    assert result['usage']['llm_in_tok'] == 1015
    assert result['usage']['llm_cached_in_tok'] == 110
    assert result['usage']['gpu_s'] == 22.5
    assert result['cost_usd'] == .001144
    assert result['usage_corrections'] == corrected['usage_corrections']
    assert runs.settle_cost('fixture') is None
    assert json.loads(ledger.read_text())['fixture'] == result


def test_transcribe_timeout_still_records_wall_clock(tmp_path, monkeypatch):
    import httpx

    from video2pdf import runs, transcribe
    audio = tmp_path / 'remote-audio.m4a'
    audio.write_bytes(b'audio fixture')
    monkeypatch.setattr(transcribe, 'run', lambda args: None)
    monkeypatch.setattr(transcribe, 'pool_headers', lambda: {})
    seen = []
    monkeypatch.setattr(runs, 'bump_usage', lambda **kw: seen.append(kw))

    def timeout(*args, **kw):
        raise httpx.ReadTimeout('fixture')

    monkeypatch.setattr(httpx, 'post', timeout)
    with pytest.raises(httpx.ReadTimeout):
        transcribe.remote_transcribe(tmp_path / 'video.mp4', tmp_path, 'http://fixture', 'turbo', 'zh')
    assert len(seen) == 1 and seen[0]['gpu_s'] >= 0


def test_real_historical_8000_token_gap_replayed_through_correction():
    summary = json.loads((ROOT / 'docs/testing/2026-10-22/kaggle-token-integration/supplier-usage-24h-redacted.json').read_text())
    export = {'truncated': False, 'items': [dict(r, sessionID='video2md-3c05faf8')
               for r in summary['rows'] if r['run_id'] == '3c05faf8']}
    record, _ = evidence()
    record.update(run_id='3c05faf8', cost_usd=.013502,
                  usage={'gpu_s':1460.57,'llm_in_tok':19169,'llm_out_tok':23495,'llm_cached_in_tok':9344})
    plan = mod.prepare(json.dumps(export).encode(), record)
    result, _ = mod.correct(record, plan)
    assert result['usage']['llm_out_tok'] - record['usage']['llm_out_tok'] == 8000
    assert result['cost_usd'] == .019184
    assert len(plan['requests']) == 7


def test_transcribe_budget_and_evaluation_case_inventory():
    import inspect

    from video2pdf import transcribe
    assert 'read=5400.0' in inspect.getsource(transcribe.remote_transcribe)
    cases = [json.loads(line) for line in (ROOT / 'docs/evals/production-resolution-eval-set.jsonl').read_text().splitlines()]
    assert {c['id'] for c in cases} == {f'pri-{n:03}' for n in range(1, 14)}
    assert all(c['positive'] and c['negative'] and c['regression'] and c['production_evidence'] for c in cases)


def test_postgres_resume_settlement_locks_latest_row_and_consumes_delta_once(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from video2pdf import runs
    record, raw = evidence()
    corrected, _ = mod.correct(record, mod.prepare(raw, record))
    calls = []

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, sql, args):
            calls.append((sql, args))
            return SimpleNamespace(fetchone=lambda: (corrected,))

    monkeypatch.setattr(runs, '_runs_backend', lambda: SimpleNamespace(
        pool=SimpleNamespace(connection=lambda: nullcontext(Connection()))))
    monkeypatch.setattr(runs, '_USAGE', {'fixture': {'llm_in_tok': 10, 'llm_out_tok': 20, 'gpu_s': 1}})
    result = runs.settle_cost('fixture')
    assert 'FOR UPDATE' in calls[0][0]
    assert result['usage']['llm_out_tok'] == 30
    assert result['usage']['gpu_s'] == 11.5
    assert len(result['usage_corrections']) == 1
    assert calls[1][1][1] == 'fixture'
    assert calls[1][1][0].obj == result
    assert runs.settle_cost('fixture') is None
    assert len(calls) == 2


def test_private_backup_allows_existing_init_group_to_traverse_without_reading_data(tmp_path):
    record, raw = evidence()
    plan = mod.prepare(raw, record)
    path = mod.backup(record, tmp_path / 'backups', plan['correction_id'])
    # The restricted root init has CHOWN and the app gid, but no DAC_OVERRIDE.
    assert path.parent.stat().st_mode & 0o777 == 0o750
    assert path.stat().st_mode & 0o777 == 0o600

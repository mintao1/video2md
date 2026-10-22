"""Each production finding has executable positive, negative and missing-evidence gates."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/check_production_failure_cost.py'
SPEC = importlib.util.spec_from_file_location('failure_cost_gate', SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
DATASET = ROOT / 'docs/evals/production-failure-cost-eval-set.jsonl'
EXAMPLE = ROOT / 'docs/evals/production-failure-cost-evidence.example.json'


def example():
    return json.loads(EXAMPLE.read_text())


def metrics(evidence, index):
    return evidence['results'][index]['metrics']


def labels(evidence):
    return {failure.split(':', 1)[0] for failure in gate.evaluate(evidence, allow_synthetic=True)}


def test_every_finding_has_dataset_and_executable_validator():
    cases = [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]
    assert [c['id'] for c in cases] == list(gate.IDS)
    assert [c['validator'] for c in cases] == [check.__name__ for check in gate.CHECKS.values()]
    assert all(c['actions'] and c['fail_categories'] and c['secrets'] for c in cases)
    assert labels(example()) == set()
    assert 'matrix' in {f.split(':', 1)[0] for f in gate.evaluate(example())}


@pytest.mark.parametrize('index', range(4))
@pytest.mark.parametrize('defect', ['absent', 'pending', 'claimed-pass', 'no-ref', 'wrong-run', 'wrong-release', 'invalid-digest'])
def test_every_finding_fails_closed(index, defect):
    evidence = example()
    row = evidence['results'][index]
    case_id = row['id']
    if defect == 'absent':
        evidence['results'].remove(row)
    elif defect == 'pending':
        row['status'] = 'not-tested'
    elif defect == 'claimed-pass':
        row['metrics'] = {'run_id': evidence['run_id'], 'release': evidence['release']}
    elif defect == 'no-ref':
        row['evidence_refs'] = []
    elif defect == 'invalid-digest':
        row['evidence_refs'][0]['sha256'] = 'invalid'
    else:
        row['metrics']['run_id' if defect == 'wrong-run' else 'release'] = 'unrelated'
    assert case_id in labels(evidence)


@pytest.mark.parametrize('index', [0, 1])
def test_failed_dependency_cannot_spend_even_one_call(index):
    e = example()
    metrics(e, 0)['entries'][index]['llm_calls_after_failure'] = 1
    assert 'failure-cost-001' in labels(e)


def test_stopping_healthy_entries_cannot_pass():
    e = example()
    metrics(e, 0)['entries'][2]['llm_calls'] = 0
    assert 'failure-cost-001' in labels(e)


@pytest.mark.parametrize('defect', ['lost-first', 'cache-loss', 'double-count', 'unknown-usage', 'duplicate-id', 'wrong-run'])
def test_retry_badcases_rejected(defect):
    e = example()
    m = metrics(e, 1)
    if defect == 'lost-first':
        m['recorded_usage'].update(input_tokens=200, output_tokens=60, cached_tokens=120)
    elif defect == 'cache-loss':
        m['recorded_usage']['cached_tokens'] = 120
    elif defect == 'double-count':
        m['recorded_usage']['input_tokens'] = 400
    elif defect == 'unknown-usage':
        m['attempts'][0].pop('usage_source')
    elif defect == 'duplicate-id':
        m['attempts'][1]['request_id'] = m['attempts'][0]['request_id']
    else:
        m['attempts'][0]['run_id'] = 'other-run'
    assert 'failure-cost-002' in labels(e)


@pytest.mark.parametrize('defect', ['timeout', 'missing-login', 'expired-refresh', 'missing-me', 'unbounded'])
def test_oauth_production_stability_cannot_be_inferred_from_one_login(defect):
    e = example()
    rows = metrics(e, 2)['observations']
    if defect == 'missing-login':
        rows.pop()
    elif defect == 'timeout':
        rows[1]['outcome'] = 'ReadTimeout'
    elif defect == 'expired-refresh':
        rows[1]['refresh_status'] = 401
    elif defect == 'missing-me':
        rows[1].pop('me_status')
    else:
        rows[1]['elapsed_s'] = 61
    assert 'failure-cost-003' in labels(e)


@pytest.mark.parametrize('defect', ['formula-only', 'missing-invoice', 'wrong-request', 'wrong-run', 'wrong-account',
                                   'missing-row', 'duplicate-row', 'wrong-currency', 'shared-balance',
                                   'unsettled', 'amount-mismatch', 'usage-mismatch', 'cache-mismatch'])
def test_supplier_actual_debit_requires_independent_exact_reconciliation(defect):
    e = example()
    m = metrics(e, 3)
    if defect == 'formula-only':
        m['amount_source'] = 'usage_times_prices'
    elif defect == 'missing-invoice':
        m['provider_export_id'] = ''
    elif defect == 'wrong-request':
        m['supplier_rows'][0]['request_id'] = 'different-request'
    elif defect == 'wrong-run':
        m['supplier_rows'][0]['run_id'] = 'other-run'
    elif defect == 'wrong-account':
        m['supplier_rows'][0]['account_ref'] = 'other-account'
    elif defect == 'missing-row':
        m['supplier_rows'].pop()
    elif defect == 'duplicate-row':
        m['supplier_rows'].append(m['supplier_rows'][0])
    elif defect == 'wrong-currency':
        m['currency'] = 'CNY'
    elif defect == 'shared-balance':
        m['scope'] = 'shared-account-balance'
    elif defect == 'unsettled':
        m['settlement_status'] = 'pending'
    elif defect == 'amount-mismatch':
        m['supplier_rows'][0]['debit_usd'] += .000002
        m['tolerance'] = 999  # Caller tolerance must never override the fixed limit.
    elif defect == 'cache-mismatch':
        m['supplier_rows'][0]['cached_tokens'] = 0
    else:
        m['supplier_rows'][0]['input_tokens'] += 1
    assert 'failure-cost-004' in labels(e)


@pytest.mark.parametrize('index,field', [(1, 'recorded_usage'), (3, 'application_cost_usd')])
@pytest.mark.parametrize('value', [True, None, -1, float('nan'), float('inf'), '0.0001'])
def test_invalid_measurements_fail_closed(index, field, value):
    e = example()
    metrics(e, index)[field] = value
    assert gate.IDS[index] in labels(e)


def test_no_usage_response_does_not_mean_verified_zero():
    e = example()
    metrics(e, 1)['attempts'][0].update(input_tokens=0, output_tokens=0, cached_tokens=0, usage_source='not_returned')
    assert 'failure-cost-002' in labels(e)


def test_wrong_release_and_duplicate_rows_fail():
    e = example()
    assert any('release mismatch' in x for x in gate.evaluate(e, expected_release='new-release', allow_synthetic=True))
    e['results'].append(e['results'][0])
    assert 'matrix' in labels(e)


def test_files_are_checked_against_hashes(tmp_path):
    e = example()
    artifact = tmp_path / 'proof.json'
    artifact.write_text('{"sanitized":true}')
    ref = {'path': artifact.name, 'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}
    for row in e['results']:
        row['evidence_refs'] = [ref.copy()]
    assert gate.verify_files(e, tmp_path) == []
    artifact.write_text('changed')
    assert len(gate.verify_files(e, tmp_path)) == 4
    artifact.unlink()
    assert len(gate.verify_files(e, tmp_path)) == 4


def test_synthetic_cli_cannot_close_production(tmp_path):
    path = tmp_path / 'evidence.json'
    path.write_text(EXAMPLE.read_text())
    p = subprocess.run([sys.executable, str(SCRIPT), str(path), '--expected-release', 'synthetic-release'],
                       text=True, capture_output=True)
    assert p.returncode == 1 and 'synthetic' in p.stdout and 'failure-cost-001' in p.stdout


def test_original_production_evidence_stays_failed():
    path = ROOT / 'docs/evals/production-failure-cost-evidence-2026-10-21.json'
    e = json.loads(path.read_text())
    assert set(gate.IDS) <= labels(e)
    assert gate.verify_files(e, path.parent) == []


def test_ci_runs_behavior_and_evidence_regressions_and_production_requires_new_evidence():
    ci = (ROOT / '.github/workflows/ci.yml').read_text()
    prod = (ROOT / '.github/workflows/production-pipeline-gate.yml').read_text()
    for filename in ('test_failure_cost_regressions.py', 'test_production_failure_cost_eval_set.py'):
        assert filename in ci
    assert 'test_api_contract.py' in ci
    assert 'check_production_failure_cost.py' in prod and '--expected-release' in prod
    assert 'failure_cost_evidence_file:' in prod


def test_supplier_reconciliation_cannot_ignore_paid_empty_retry():
    e = example()
    m = metrics(e, 3)
    m['application_requests'].pop(0)
    m['supplier_rows'].pop(0)
    m['application_cost_usd'] = .00006
    assert gate.supplier_debit(m)  # Locally consistent subset is insufficient.
    assert 'failure-cost-004' in labels(e)


@pytest.mark.parametrize('bad', [None, [], {}, 1, 'pass', [None], [{'id': []}]])
def test_malformed_results_never_pass(bad):
    e = example()
    e['results'] = bad
    assert labels(e)


@pytest.mark.parametrize('defect', ['duplicate-service', 'old-unit-enabled', 'public-listener', 'missing-relay'])
def test_oauth_stability_requires_one_loopback_relay(defect):
    e = example()
    m = metrics(e, 2)
    if defect == 'duplicate-service':
        m['relay']['active_units'].append('oauth-relay.service')
    elif defect == 'old-unit-enabled':
        m['relay']['retired_unit_enabled'] = True
    elif defect == 'public-listener':
        m['relay']['listener_host'] = '0.0.0.0'
    else:
        m.pop('relay')
    assert 'failure-cost-003' in labels(e)


def subscription_example():
    e = example()
    m = metrics(e, 3)
    m.update(billing_mode='subscription', amount_source='provider_subscription_usage',
             subscription_invoice={'invoice_id': 'synthetic-invoice', 'subscription_id': 'synthetic-plan',
                 'account_ref': m['account_ref'], 'currency': 'USD', 'status': 'paid', 'amount_paid_usd': 10,
                 'period_start': '2026-10-01T00:00:00+00:00', 'period_end': '2026-11-01T00:00:00+00:00',
                 'document_sha256': '0' * 64})
    for r in m['supplier_rows']:
        r.update(debit_usd=0, subscription_id='synthetic-plan', included_in_subscription=True,
                 occurred_at='2026-10-21T12:00:00+00:00')
    for r in m['application_requests']:
        r['occurred_at'] = '2026-10-21T12:00:00+00:00'
    e['results'][3]['evidence_refs'].append({'path': 'synthetic-invoice.json', 'sha256': '0' * 64,
                                          'role': 'supplier_invoice'})
    return e


def test_subscription_invoice_is_separate_from_token_attribution():
    e = subscription_example()
    assert metrics(e, 3)['application_cost_usd'] > 0
    assert sum(r['debit_usd'] for r in metrics(e, 3)['supplier_rows']) == 0
    assert metrics(e, 3)['subscription_invoice']['amount_paid_usd'] == 10
    assert labels(e) == set()
    assert 'matrix' in {f.split(':', 1)[0] for f in gate.evaluate(e)}


@pytest.mark.parametrize('defect', ['no-invoice', 'unpaid', 'wrong-account', 'wrong-plan', 'outside-period',
                                   'no-timezone', 'cash-double-charge', 'not-included', 'no-invoice-file',
                                   'invoice-file-mismatch', 'changed-usage', 'missing-empty-retry'])
def test_subscription_requires_real_invoice_and_matching_included_requests(defect):
    e = subscription_example()
    m = metrics(e, 3)
    if defect == 'no-invoice':
        m.pop('subscription_invoice')
    elif defect == 'unpaid':
        m['subscription_invoice']['status'] = 'pending'
    elif defect == 'wrong-account':
        m['subscription_invoice']['account_ref'] = 'other-account'
    elif defect == 'wrong-plan':
        m['supplier_rows'][0]['subscription_id'] = 'other-plan'
    elif defect == 'outside-period':
        m['supplier_rows'][0]['occurred_at'] = '2026-11-01T00:00:00+00:00'
    elif defect == 'no-timezone':
        m['subscription_invoice']['period_start'] = '2026-10-01T00:00:00'
    elif defect == 'cash-double-charge':
        m['supplier_rows'][0]['debit_usd'] = .0001
    elif defect == 'not-included':
        m['supplier_rows'][0]['included_in_subscription'] = False
    elif defect == 'no-invoice-file':
        e['results'][3]['evidence_refs'].pop()
    elif defect == 'invoice-file-mismatch':
        m['subscription_invoice']['document_sha256'] = 'f' * 64
    elif defect == 'changed-usage':
        m['supplier_rows'][0]['input_tokens'] += 1
    else:
        m['supplier_rows'].pop(0)
        m['application_requests'].pop(0)
    assert 'failure-cost-004' in labels(e)


def receipt_example():
    """The user chose paid receipt + complete request usage on 2026-10-22."""
    e = example()
    m = metrics(e, 3)
    m.update(billing_mode='subscription_receipt', amount_source='provider_export_attribution',
             product='go', export_truncated=False, provider_export_sha256='1' * 64,
             extra_usage_enabled=False,
             subscription_invoice={'invoice_id': 'synthetic-cny-invoice', 'account_ref': m['account_ref'],
                 'product': 'go', 'currency': 'CNY', 'status': 'paid', 'amount_paid': 67.72,
                 'receipt_date': '2026-10-01', 'paid_until': '2026-11-01T00:00:00+00:00',
                 'document_sha256': '0' * 64})
    # The supplier exports USD cost attribution, not a per-request cash debit.
    for r in m['supplier_rows']:
        r['attribution_usd'] = r.pop('debit_usd')
        r.update(product='go', occurred_at='2026-10-21T12:00:00+00:00')
    for r in m['application_requests']:
        r['occurred_at'] = '2026-10-21T12:00:00+00:00'
    e['results'][3]['evidence_refs'].extend([
        {'path': 'synthetic-invoice.json', 'sha256': '0' * 64, 'role': 'supplier_invoice'},
        {'path': 'synthetic-export.json', 'sha256': '1' * 64, 'role': 'supplier_export'},
        {'path': 'synthetic-paid-ui.json', 'sha256': '2' * 64, 'role': 'supplier_paid_status'},
        {'path': 'synthetic-go-ui.json', 'sha256': '3' * 64, 'role': 'subscription_coverage'}])
    return e


def test_paid_cny_receipt_and_usd_request_usage_are_independently_accepted():
    e = receipt_example()
    assert labels(e) == set()
    m = metrics(e, 3)
    assert m['subscription_invoice']['currency'] == 'CNY' and m['currency'] == 'USD'
    assert not any('debit_usd' in r for r in m['supplier_rows'])
    assert 'subscription_id' not in m['subscription_invoice']
    assert 'matrix' in {f.split(':', 1)[0] for f in gate.evaluate(e)}


@pytest.mark.parametrize('defect', ['unpaid', 'wrong-account', 'wrong-product', 'wrong-request-product',
    'outside-period', 'no-timezone', 'missing-time', 'wrong-time', 'truncated', 'extra-usage',
    'cost-formula', 'attribution-mismatch', 'cash-confusion', 'zero-payment', 'missing-invoice',
    'missing-export', 'export-hash', 'unknown-currency', 'missing-empty-retry', 'usage-mismatch'])
def test_subscription_receipt_still_requires_independent_complete_evidence(defect):
    e = receipt_example()
    m = metrics(e, 3)
    invoice = m['subscription_invoice']
    if defect == 'unpaid':
        invoice['status'] = 'pending'
    elif defect == 'wrong-account':
        invoice['account_ref'] = 'other'
    elif defect == 'wrong-product':
        invoice['product'] = 'other'
    elif defect == 'wrong-request-product':
        m['supplier_rows'][0]['product'] = 'other'
    elif defect == 'outside-period':
        m['supplier_rows'][0]['occurred_at'] = '2026-11-01T00:00:00+00:00'
    elif defect == 'no-timezone':
        invoice['paid_until'] = '2026-11-01T00:00:00'
    elif defect == 'missing-time':
        m['supplier_rows'][0].pop('occurred_at')
    elif defect == 'wrong-time':
        m['application_requests'][0]['occurred_at'] = '2026-10-21T13:00:00+00:00'
    elif defect == 'truncated':
        m['export_truncated'] = True
    elif defect == 'extra-usage':
        m['extra_usage_enabled'] = True
    elif defect == 'cost-formula':
        m['amount_source'] = 'usage_times_prices'
    elif defect == 'attribution-mismatch':
        m['supplier_rows'][0]['attribution_usd'] += .000002
    elif defect == 'cash-confusion':
        m['supplier_rows'][0]['debit_usd'] = m['supplier_rows'][0]['attribution_usd']
    elif defect == 'zero-payment':
        invoice['amount_paid'] = 0
    elif defect == 'missing-invoice':
        e['results'][3]['evidence_refs'] = [r for r in e['results'][3]['evidence_refs'] if r.get('role')!='supplier_invoice']
    elif defect == 'missing-export':
        e['results'][3]['evidence_refs'] = [r for r in e['results'][3]['evidence_refs'] if r.get('role')!='supplier_export']
    elif defect == 'export-hash':
        m['provider_export_sha256'] = 'f' * 64
    elif defect == 'unknown-currency':
        invoice['currency'] = '???'
    elif defect == 'missing-empty-retry':
        m['supplier_rows'].pop(0)
        m['application_requests'].pop(0)
    else:
        m['supplier_rows'][0]['output_tokens'] += 1
    assert 'failure-cost-004' in labels(e)


@pytest.mark.parametrize('role', ['supplier_paid_status', 'subscription_coverage'])
def test_receipt_pdf_amount_due_alone_cannot_prove_paid_or_active(role):
    e = receipt_example()
    e['results'][3]['evidence_refs'] = [r for r in e['results'][3]['evidence_refs'] if r.get('role') != role]
    assert 'failure-cost-004' in labels(e)

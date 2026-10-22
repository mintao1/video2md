"""Normalize immutable October 22 observations; no network, model or ledger writes."""
import copy
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / 'kaggle-token-integration'
MATRIX = ROOT / 'docs/evals/kaggle-billing-followup-evidence-2026-10-22.json'


def load(name):
    return json.loads((SOURCE / name).read_text())


def reference(path, role='observation'):
    return {'path': os.path.relpath(path, MATRIX.parent), 'role': role,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


auth, config = load('token-validation.json'), load('runtime-credentials.json')
probe, run = load('kaggle-layout-verified-gpu.json'), load('dd586072-progress.json')
before, submitted, final = load('before-retry.json'), load('submitted-state.json'), load('final-state.json')
summary, cleanup = load('result-summary.json'), load('observer-cleanup.json')
export = load('supplier-usage-24h-redacted.json')
healthy, gap = load('provider-reconciliation-healthy.json'), load('provider-reconciliation-original.json')
queue_text = (SOURCE / 'kaggle-after-cleanup-state.txt').read_text()
assert 'Version #7 with GPU T4 x2 Queued' in queue_text
assert queue_text.count('text Cancelled') == 2
business_id = run['run_id']
business_cost = next(r for r in final['cost']['runs'] if r['run_id'] == business_id)
scoped_rows = [r for r in export['rows'] if r['run_id'] == gap['run_id']]
legacy_path = ROOT / 'docs/evals/production-failure-cost-evidence-2026-10-22.json'
legacy = json.loads(legacy_path.read_text())

observations = {
    'kfi-001': {k: auth[k] for k in ('authenticated', 'auth_method', 'token_value_recorded')},
    'kfi-002': {**probe, 'requested_control_id': probe['control_id']},
    # Transcribed from the recorded computer-use actions and before/after screenshots.
    'kfi-003': {'authenticated': True, 'actions': ['login', 'input', 'submit'],
                'input_url': 'https://www.bilibili.com/video/BV1LV411r77C/',
                'submitted_url': run['entries'][0]['url'].split('?')[0],
                'submit_count': 1, 'run_id': business_id,
                'task_count_before': len(before['runs']), 'task_count_after': len(submitted['runs'])},
    'kfi-004': {**run, 'claimed_scope': 'selected_first_two'},
    'kfi-005': {'run_id': business_id, 'remote_status': 'queued', 'business_status': run['status'],
                'claimed_delivery_success': False, 'completed_new_books': summary['completed_new_books'],
                'llm_calls_after_block': final['new_task_runtime']['llm_span_count'],
                'cost_finalized': business_cost['cost_usd'] is not None,
                'gpu_seconds_semantics': 'remote_call_wall_clock'},
    'kfi-006': {'auxiliary_jobs': [
        {'kernel': 'gpu-router-job', 'version': 6, 'status': 'cancelled', 'observed_terminal': True,
         'observation_s': load('gpu-clips-verified.json')['elapsed_s']},
        {'kernel': 'gpu-router-diag-accelerator', 'version': 1, 'status': 'cancelled', 'observed_terminal': True,
         'observation_s': cleanup['age_s']}],
        'business_job': {'kernel': 'gpu-router-job', 'version': 7, 'status': 'queued'},
        'max_observation_s': 1800, 'observer_stopped': cleanup['stopped']},
    'kfi-007': {'run_id': business_id, 'status': run['status'], 'books': []},
    'kfi-008': {'run_id': gap['run_id'], 'truncated': export['truncated'],
                'rows': [{'run_id': r['run_id'], 'request_id': r['requestID'],
                          'usage_observed': all(type(r.get(k)) is int and r[k] >= 0 for k in
                                                ('inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens'))}
                         for r in scoped_rows],
                'headers_in_public_evidence': not export['headers_and_account_identity_omitted'],
                'private_original_preserved': load('supplier-invoice-summary.json')['raw_originals_stored_outside_repository']},
    'kfi-009': healthy,
    'kfi-010': {**gap, 'historical_bill_backfilled': summary['historical_bill_backfilled'], 'claimed_fixed': False},
    'kfi-011': {'run_id': gap['run_id'], 'applied': summary['historical_bill_backfilled'],
                'before_usage': gap['application_usage'], 'after': gap, 'audit': None,
                'second_apply_changed': None, 'unrelated_runs_changed': None, 'cash_charge_created': None},
    'kfi-012': {'matrix': legacy, 'expected_release': legacy['release'], 'run_id': business_id},
}
observations['kfi-001'].update({k: config[k] for k in ('token_file_present', 'token_readable', 'username_present')})
source_path = HERE / 'case-observations.json'
source_path.write_text(json.dumps({'kind': 'historical_observation_normalization',
                                  'observed_at': summary['observed_at'],
                                  'note': 'Snapshot replay; not a live status query or new production execution.',
                                  'observations': observations}, ensure_ascii=False, indent=2) + '\n')
files = {
    1: ['token-validation.json', 'runtime-credentials.json'],
    2: ['kaggle-layout-verified-gpu.json', 'patch-manifest-b.json', 'runtime-b-source.json'],
    3: ['bilibili-explicit-after-verification.png', 'bilibili-submitted-after-verification.png', 'submitted-state.json'],
    4: ['dd586072-progress.json'], 5: ['final-state.json', 'kaggle-after-cleanup-state.txt'],
    6: ['kaggle-after-cleanup-state.txt', 'kaggle-after-cleanup.png', 'observer-cleanup.json', 'verified-route-after-cleanup.json'],
    7: ['dd586072-progress.json', 'bilibili-current-state.png'],
    8: ['supplier-usage-24h-redacted.json'], 9: ['provider-reconciliation-healthy.json'],
    10: ['provider-reconciliation-original.json'], 11: ['provider-reconciliation-original.json', 'result-summary.json'],
}
results = []
for index, (case_id, metrics) in enumerate(observations.items(), 1):
    refs = [reference(source_path, 'case_source')]
    for name in files.get(index, []):
        role = {'kaggle-layout-verified-gpu.json': 'cuda_probe', 'patch-manifest-b.json': 'gpu_manifest',
                'runtime-b-source.json': 'gpu_runtime'}.get(name, 'observation')
        refs.append(reference(SOURCE / name, role))
    if index == 12:
        refs.append(reference(legacy_path, 'failure_cost_matrix'))
    results.append({'id': case_id, 'status': {7: 'blocked', 11: 'not-tested', 12: 'fail'}.get(index, 'pass'),
                    'metrics': copy.deepcopy(metrics), 'evidence_refs': refs})
MATRIX.write_text(json.dumps({'kind': 'production', 'release': summary['api_release'],
                             'gpu_release': summary['gpu_release'], 'run_id': business_id,
                             'observed_at': summary['observed_at'],
                             'scope': 'historical_snapshot_replay_not_live_verification',
                             'results': results}, ensure_ascii=False, indent=2) + '\n')
print('Wrote twelve scoped cases; textbook, historical backfill and full acceptance remain open.')

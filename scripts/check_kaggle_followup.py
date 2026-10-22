#!/usr/bin/env python3
"""Offline evidence gate for every finding in the Kaggle/billing follow-up.

Passing synthetic regression examples never closes production acceptance.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('failure_cost_gate', HERE / 'check_production_failure_cost.py')
failure_cost = importlib.util.module_from_spec(spec)
spec.loader.exec_module(failure_cost)


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def credentials(m):
    return m['authenticated'] is True and m['auth_method'] == 'ACCESS_TOKEN' \
        and m['token_file_present'] is True and m['token_readable'] is True \
        and m['username_present'] is True and m['token_value_recorded'] is False


def cuda(m):
    return m['kernel_status'] == 'done' and m['cuda_available'] is True \
        and m['fresh_control_id_matches'] is True and bool(m['requested_control_id']) \
        and m['control_id'] == m['requested_control_id'] \
        and type(m['cuda_devices']) is int and m['cuda_devices'] == 2 \
        and m['cuda_device_names'] == ['Tesla T4', 'Tesla T4'] \
        and type(m['nvidia_exit_code']) is int and m['nvidia_exit_code'] == 0


def frontend(m):
    return m['authenticated'] is True and m['actions'] == ['login', 'input', 'submit'] \
        and m['input_url'] == m['submitted_url'] \
        and re.fullmatch(r'https://www\.bilibili\.com/video/BV[0-9A-Za-z]+/?', m['input_url']) \
        and m['submit_count'] == 1 and type(m['submit_count']) is int \
        and bool(m['run_id']) and type(m['task_count_before']) is int and m['task_count_before'] >= 0 \
        and type(m['task_count_after']) is int and m['task_count_after'] == m['task_count_before'] + 1


def downloads(m):
    entries = m['entries']
    return type(m['total']) is int and m['total'] == 2 and m['claimed_scope'] == 'selected_first_two' \
        and len(entries) == 2 and [e['index'] for e in entries] == [1, 2] \
        and all(type(e['index']) is int for e in entries) \
        and len({e['url'] for e in entries}) == 2 \
        and all(e['done']['download'] is True and e['skipped'] is False for e in entries)


def queue_safety(m):
    return m['remote_status'] == 'queued' and m['business_status'] in ('queued', 'running') \
        and m['claimed_delivery_success'] is False \
        and type(m['completed_new_books']) is int and m['completed_new_books'] == 0 \
        and type(m['llm_calls_after_block']) is int and m['llm_calls_after_block'] == 0 \
        and m['cost_finalized'] is False and m['gpu_seconds_semantics'] == 'remote_call_wall_clock'


def cleanup(m):
    jobs = m['auxiliary_jobs']
    def identity(row):
        return row['kernel'], row['version']
    return bool(jobs) and positive(m['max_observation_s']) \
        and all(type(j['version']) is int and j['version'] > 0 for j in jobs + [m['business_job']]) \
        and len({identity(j) for j in jobs}) == len(jobs) \
        and all(j['status'] in ('cancelled', 'done', 'error') and j['observed_terminal'] is True for j in jobs) \
        and all(positive(j['observation_s']) and j['observation_s'] <= m['max_observation_s'] for j in jobs) \
        and identity(m['business_job']) not in {identity(j) for j in jobs} \
        and m['business_job']['status'] in ('queued', 'running', 'done') \
        and m['observer_stopped'] is True


def textbook(m):
    books = m['books']
    return m['status'] in ('done', 'partial') and bool(books) \
        and len({b['book_id'] for b in books}) == len(books) \
        and all(b['run_id'] == m['run_id'] and type(b['markdown_bytes']) is int and positive(b['markdown_bytes']) \
                and b['reader_http_status'] == 200 and b['readable'] is True \
                and b['reused_old_book'] is False for b in books)


def export_complete(m):
    rows = m['rows']
    ids = [r['request_id'] for r in rows]
    return m['truncated'] is False and bool(rows) and all(ids) and len(ids) == len(set(ids)) \
        and all(r['run_id'] == m['run_id'] and r['usage_observed'] is True for r in rows) \
        and m['headers_in_public_evidence'] is False and m['private_original_preserved'] is True


def usage_balanced(m):
    rows = m['requests']
    ids = [r['request_id'] for r in rows]
    fields = {'llm_in_tok': 'input_tokens', 'llm_out_tok': 'output_tokens', 'llm_cached_in_tok': 'cached_tokens'}
    return bool(rows) and all(isinstance(i, str) and i for i in ids) \
        and len(ids) == len(set(ids)) and type(m['provider_request_count']) is int \
        and m['provider_request_count'] == len(rows) \
        and all(failure_cost.usage(r) for r in rows) \
        and all(sum(r[column] for r in rows) == m['provider_usage'][key] == m['application_usage'][key]
                for key, column in fields.items()) \
        and all(m['missing_usage'][key] == 0 for key in fields) \
        and m['usage_matches'] is True and m['attribution_matches'] is True \
        and failure_cost.number(float(m['provider_attribution_usd'])) \
        and failure_cost.number(float(m['application_attribution_usd'])) \
        and abs(float(m['provider_attribution_usd']) - float(m['application_attribution_usd'])) <= 0.000001 \
        and m['supplier_cash_verified'] is False


def historical_gap(m):
    fields = ('llm_in_tok', 'llm_out_tok', 'llm_cached_in_tok')
    return m['run_id'] == '3c05faf8' and m['provider_request_count'] == 7 \
        and all(m['provider_usage'][key] - m['application_usage'][key] == m['missing_usage'][key] for key in fields) \
        and m['missing_usage'] == {'llm_in_tok': 18694, 'llm_out_tok': 8000, 'llm_cached_in_tok': 9344} \
        and m['usage_matches'] is False and m['attribution_matches'] is False \
        and m['historical_bill_backfilled'] is False and m['claimed_fixed'] is False


def backfill(m):
    audit = m['audit']
    return usage_balanced(m['after']) and m['after']['run_id'] == m['run_id'] \
        and audit['run_id'] == m['run_id'] and bool(audit['id']) \
        and re.fullmatch(r'[0-9a-f]{64}', audit['provider_export_sha256']) \
        and audit['before_usage'] == m['before_usage'] \
        and audit['after_usage'] == m['after']['application_usage'] \
        and m['applied'] is True and m['second_apply_changed'] is False \
        and m['unrelated_runs_changed'] == [] and m['cash_charge_created'] is False


def acceptance(m):
    return m['matrix']['run_id'] == m['run_id'] \
        and not failure_cost.evaluate(m['matrix'], expected_release=m['expected_release'])


CHECKS = dict(zip((f'kfi-{i:03}' for i in range(1, 13)),
                 (credentials, cuda, frontend, downloads, queue_safety, cleanup,
                  textbook, export_complete, usage_balanced, historical_gap, backfill, acceptance)))


def check(case_id, metrics):
    try:
        return bool(CHECKS[case_id](metrics))
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return False


def evaluate(evidence, *, expected_release, expected_gpu_release, expected_run_id, allow_synthetic=False):
    failures = []
    if not isinstance(evidence, dict):
        return ['matrix: evidence must be an object']
    if evidence.get('kind') != 'production' and not (allow_synthetic and evidence.get('kind') == 'synthetic'):
        failures.append('matrix: synthetic evidence cannot close production acceptance')
    for key, expected in [('release', expected_release), ('gpu_release', expected_gpu_release), ('run_id', expected_run_id)]:
        if not expected or evidence.get(key) != expected:
            failures.append('matrix: missing or mismatched ' + key)
    rows = evidence.get('results')
    if not isinstance(rows, list):
        rows = []
    ids = [r.get('id') for r in rows if isinstance(r, dict) and isinstance(r.get('id'), str)]
    if len(ids) != len(rows) or len(ids) != len(set(ids)) or set(ids) != set(CHECKS):
        failures.append('matrix: exact twelve unique cases required')
    for case_id in CHECKS:
        row = next((r for r in rows if isinstance(r, dict) and r.get('id') == case_id), {})
        refs = row.get('evidence_refs')
        if row.get('status') != 'pass' or not check(case_id, row.get('metrics')) \
                or not isinstance(refs, list) or not refs \
                or not all(isinstance(r, dict) and r.get('path') \
                           and re.fullmatch(r'[0-9a-f]{64}', str(r.get('sha256', ''))) for r in refs):
            failures.append(case_id + ': missing, blocked or inconsistent evidence')
        metrics = row.get('metrics') if isinstance(row.get('metrics'), dict) else {}
        if case_id in ('kfi-003', 'kfi-004', 'kfi-005', 'kfi-007', 'kfi-012') and metrics.get('run_id') != expected_run_id:
            failures.append(case_id + ': wrong business run')
        if case_id == 'kfi-012' and metrics.get('expected_release') != expected_release:
            failures.append(case_id + ': wrong acceptance release')
    return failures


def verify_files(evidence, base):
    failures = failure_cost.verify_files(evidence, base)
    for row in evidence.get('results', []):
        refs = [r for r in row.get('evidence_refs', []) if r.get('role') == 'case_source']
        try:
            if len(refs) != 1:
                raise ValueError('one observation source required')
            source = json.loads((base / refs[0]['path']).read_text())
            if source['observations'][row['id']] != row['metrics']:
                raise ValueError('metrics do not match recorded observation')
        except (OSError, KeyError, ValueError, TypeError):
            failures.append(row['id'] + ': metrics not bound to observation source')
    gpu = next((r for r in evidence.get('results', []) if r.get('id') == 'kfi-002'), {})
    try:
        def read_role(role):
            refs = [r for r in gpu.get('evidence_refs', []) if r.get('role') == role]
            if len(refs) != 1:
                raise ValueError('one GPU source required')
            return json.loads((base / refs[0]['path']).read_text())
        manifest, runtime, probe = (read_role(r) for r in ('gpu_manifest', 'gpu_runtime', 'cuda_probe'))
        if manifest['image'] != evidence['gpu_release'] \
                or manifest['source_sha256'] != runtime['provider_source_sha256'] \
                or any(gpu['metrics'].get(k) != v for k, v in probe.items()):
            raise ValueError('GPU image, running source or probe mismatch')
    except (OSError, KeyError, ValueError, TypeError):
        failures.append('kfi-002: GPU probe not bound to deployed source and release')
    # Full acceptance is bound to the actual four-finding matrix, not caller-supplied booleans.
    row = next((r for r in evidence.get('results', []) if r.get('id') == 'kfi-012'), {})
    refs = [r for r in row.get('evidence_refs', []) if r.get('role') == 'failure_cost_matrix']
    try:
        if len(refs) != 1:
            raise ValueError('one matrix reference required')
        path = base / refs[0]['path']
        actual = json.loads(path.read_text())
        if actual != row['metrics']['matrix']:
            raise ValueError('matrix metrics do not match file')
        failures.extend(failure_cost.verify_files(actual, path.parent))
    except (OSError, KeyError, ValueError, TypeError):
        failures.append('kfi-012: full acceptance matrix missing or not bound to its source')
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path)
    parser.add_argument('--expected-release', required=True)
    parser.add_argument('--expected-gpu-release', required=True)
    parser.add_argument('--expected-run-id', required=True)
    args = parser.parse_args()
    try:
        evidence = json.loads(args.evidence.read_text())
        failures = evaluate(evidence, expected_release=args.expected_release,
                            expected_gpu_release=args.expected_gpu_release, expected_run_id=args.expected_run_id)
        if isinstance(evidence, dict):
            failures.extend(verify_files(evidence, args.evidence.parent))
    except (OSError, ValueError, TypeError, AttributeError):
        failures = ['matrix: malformed evidence; values suppressed']
    print('FAIL' if failures else 'PASS: all twelve follow-up acceptance cases passed')
    for message in failures:
        print('- ' + message)
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())

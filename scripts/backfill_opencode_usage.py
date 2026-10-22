#!/usr/bin/env python3
"""Prepare then atomically apply a terminal run's independent usage correction.

No provider payments are made. Raw exports/record backups must remain private.
The application is imported only by apply(), so preparation can run offline.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

TOKEN_KEYS = ('llm_in_tok', 'llm_out_tok', 'llm_cached_in_tok')
TERMINAL = ('done', 'partial', 'error')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def prepare(raw: bytes, record: dict) -> dict:
    spec = importlib.util.spec_from_file_location('reconcile', Path(__file__).with_name('reconcile_opencode_usage.py'))
    reconcile = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reconcile)
    if record.get('status') not in TERMINAL or record.get('deleted') or not record.get('owner'):
        raise ValueError('Only an owned, undeleted terminal run can be corrected')
    result = reconcile.reconcile(json.loads(raw), record)
    if any(v < 0 for v in result['missing_usage'].values()) or not any(result['missing_usage'].values()):
        raise ValueError('This operation only backfills independently proven missing usage')
    pricing = record.get('cost_pricing') or {}
    if pricing.get('currency') != 'USD' or pricing.get('gpu_policy') != 'free' or pricing.get('gpu_min') != 0:
        raise ValueError('Unsupported historical pricing; manual reconciliation required')
    usage = result['provider_usage']
    amount = ((Decimal(usage['llm_in_tok'] - usage['llm_cached_in_tok']) * Decimal(str(pricing['llm_in_mtok']))
               + Decimal(usage['llm_cached_in_tok']) * Decimal(str(pricing['llm_cached_in_mtok']))
               + Decimal(usage['llm_out_tok']) * Decimal(str(pricing['llm_out_mtok']))) / Decimal(1000000))
    if abs(amount - Decimal(result['provider_attribution_usd'])) > Decimal('.000001'):
        raise ValueError('Independent attribution and historical prices disagree')
    if amount < Decimal(str(record['cost_usd'])):
        raise ValueError('Backfill cannot reduce attribution')
    plan = {'schema': 1, 'run_id': record['run_id'], 'expected_record_sha256': digest(record),
            'provider_export_sha256': hashlib.sha256(raw).hexdigest(),
            'requests': result['requests'], 'target_usage': usage,
            'target_cost_usd': float(amount.quantize(Decimal('.000001'))),
            'supplier_cash_verified': False}
    plan['correction_id'] = digest(plan)
    return plan


def correct(record: dict, plan: dict) -> tuple[dict, bool]:
    signed = {k: v for k, v in plan.items() if k != 'correction_id'}
    if plan.get('schema') != 1 or digest(signed) != plan.get('correction_id'):
        raise ValueError('Invalid or altered correction plan')
    if record.get('run_id') != plan.get('run_id') or record.get('status') not in TERMINAL or record.get('deleted'):
        raise ValueError('Correction target no longer eligible')
    for audit in record.get('usage_corrections', []):
        if audit.get('correction_id') == plan['correction_id']:
            if (all(record.get('usage', {}).get(k, 0) == plan['target_usage'][k] for k in TOKEN_KEYS)
                    and record.get('cost_usd') == plan['target_cost_usd']
                    and audit.get('provider_export_sha256') == plan['provider_export_sha256']):
                return record, False
            raise ValueError('Previously applied correction has conflicting accounting')
    if digest(record) != plan['expected_record_sha256']:
        raise ValueError('Stale plan: record changed; prepare again from fresh evidence')
    updated = copy.deepcopy(record)
    before_usage = copy.deepcopy(record.get('usage') or {})
    updated['usage'] = {**before_usage, **plan['target_usage']}
    updated['cost_usd'] = plan['target_cost_usd']
    audit = {'correction_id': plan['correction_id'], 'at': datetime.now(timezone.utc).isoformat(),
             'reason': 'independent_opencode_export_missing_attempt_usage',
             'provider_export_sha256': plan['provider_export_sha256'],
             'request_ids': [r['request_id'] for r in plan['requests']],
             'before_usage': before_usage, 'before_cost_usd': record['cost_usd'],
             'after_usage': updated['usage'], 'after_cost_usd': updated['cost_usd'],
             'supplier_cash_verified': False}
    updated.setdefault('usage_corrections', []).append(audit)
    return updated, True


def backup(record: dict, directory: Path, correction_id: str) -> Path:
    # Restricted init runs as root with CHOWN only and the existing app gid.
    # It needs directory traversal; backup contents remain owner-only (0600).
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    directory.chmod(0o750)
    path = directory / (correction_id + '.json')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if json.loads(path.read_text()) != record:
            raise ValueError('Existing backup conflicts with source record') from None
    else:
        with os.fdopen(fd, 'w') as fh:
            json.dump(record, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
    path.chmod(0o600)
    return path


def apply(plan: dict, backup_dir: Path) -> dict:
    from video2md import runs
    be = runs._runs_backend()
    saved = None
    if be is not None:
        from psycopg.types.json import Json
        with be.pool.connection() as conn, conn.transaction():
            row = conn.execute('SELECT record FROM v2md_runs WHERE run_id=%s FOR UPDATE',
                               (plan['run_id'],)).fetchone()
            if row is None:
                raise ValueError('Correction run not found')
            updated, changed = correct(row[0], plan)
            if changed:
                saved = backup(row[0], backup_dir, plan['correction_id'])
                # Only the selected locked row changes; all non-accounting fields are preserved.
                conn.execute('UPDATE v2md_runs SET record=%s::jsonb, updated=now() WHERE run_id=%s',
                             (Json(updated), plan['run_id']))
    else:
        with runs._runs_file_lock():
            records = runs._load_runs()
            if plan['run_id'] not in records:
                raise ValueError('Correction run not found')
            updated, changed = correct(records[plan['run_id']], plan)
            if changed:
                saved = backup(records[plan['run_id']], backup_dir, plan['correction_id'])
                records[plan['run_id']] = updated
                runs._save_runs(records)
    return {'run_id': plan['run_id'], 'correction_id': plan['correction_id'], 'changed': changed,
            'backup': str(saved) if saved else None, 'usage': updated['usage'],
            'cost_usd': updated['cost_usd'], 'supplier_cash_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider-export', type=Path)
    parser.add_argument('--record', type=Path)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup-dir', type=Path)
    args = parser.parse_args()
    try:
        if args.apply:
            if not args.backup_dir:
                raise ValueError('A private backup directory is required')
            result = apply(json.loads(args.plan.read_text()), args.backup_dir)
        else:
            if not args.provider_export or not args.record:
                raise ValueError('Independent export and current private record required')
            result = prepare(args.provider_export.read_bytes(), json.loads(args.record.read_text()))
            args.plan.write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        print('FAIL: correction rejected; source values suppressed')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Fail closed on the four findings from the 2026-10-21 paid production run.

Offline fixtures exercise this gate but cannot close production findings. CLI
checks hashed evidence files and requires the exact tested release identity.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TypeGuard

IDS = ('failure-cost-001', 'failure-cost-002', 'failure-cost-003', 'failure-cost-004')
COUNTERS = ('input_tokens', 'output_tokens', 'cached_tokens')


def number(value: object) -> TypeGuard[int | float]:
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def usage(value: object) -> TypeGuard[dict]:
    return isinstance(value, dict) and all(type(value.get(k)) is int and value[k] >= 0 for k in COUNTERS) \
        and value['cached_tokens'] <= value['input_tokens']


def unique_rows(rows: object, key: str) -> TypeGuard[list[dict]]:
    return isinstance(rows, list) and bool(rows) and all(isinstance(r, dict) and type(r.get(key)) in (str, int) and r[key] for r in rows) \
        and len({r[key] for r in rows}) == len(rows)


def blocked_dependencies(m: dict) -> bool:
    entries = m.get('entries')
    if not unique_rows(entries, 'entry'):
        return False
    failed = [r for r in entries if r.get('failed_stages')]
    healthy = [r for r in entries if r.get('failed_stages') == []]
    return {'ocr', 'clips'} <= {stage for row in failed for stage in row['failed_stages']} \
        and all(r.get('llm_calls_after_failure') == 0 and type(r.get('llm_calls_after_failure')) is int
                and r.get('blocked_before_model') is True for r in failed) \
        and bool(healthy) and all(type(r.get('llm_calls')) is int and r['llm_calls'] > 0 for r in healthy)


def complete_retry_usage(m: dict) -> bool:
    attempts, recorded = m.get('attempts'), m.get('recorded_usage')
    if not unique_rows(attempts, 'request_id') or not usage(recorded) or not recorded['input_tokens'] or not recorded['output_tokens']:
        return False
    if not all(usage(row) and row.get('usage_source') == 'provider_response'
               and row.get('run_id') == m.get('run_id') for row in attempts):
        return False
    # At least one empty attempt and one successful attempt must be demonstrated.
    if not {'empty', 'success'} <= {r.get('outcome') for r in attempts}:
        return False
    return all(sum(row[k] for row in attempts) == recorded[k] for k in COUNTERS)


def stable_oauth(m: dict) -> bool:
    observations = m.get('observations')
    relay = m.get('relay') or {}
    canonical = relay.get('active_units') == ['video2md-oauth-relay.service'] \
        and relay.get('retired_unit_enabled') is False and relay.get('listener_host') == '127.0.0.1'
    return canonical and unique_rows(observations, 'attempt_id') and len(observations) >= 3 \
        and all(r.get('outcome') == 'success' and r.get('me_status') == 200
                and r.get('refresh_status') == 200 and number(r.get('elapsed_s'))
                and 0 < r['elapsed_s'] <= 60 for r in observations)


def supplier_debit(m: dict) -> bool:
    app, supplier = m.get('application_requests'), m.get('supplier_rows')
    if not (m.get('provider') and m.get('provider_export_id') and m.get('account_ref')
            and m.get('currency') == 'USD' and m.get('scope') == 'run-isolated'
            and m.get('settlement_status') == 'settled'
            and unique_rows(app, 'request_id') and unique_rows(supplier, 'request_id')
            and number(m.get('application_cost_usd'))):
        return False
    left, right = ({r['request_id']: r for r in rows} for rows in (app, supplier))
    if left.keys() != right.keys():
        return False
    mode = m.get('billing_mode', 'per_request')
    for key, row in left.items():
        billed = right[key]
        if not (usage(row) and usage(billed) and row.get('run_id') == m.get('run_id')
                and billed.get('run_id') == m.get('run_id')
                and (mode == 'subscription_receipt' or number(billed.get('debit_usd')))
                and row.get('account_ref') == m.get('account_ref')
                and billed.get('account_ref') == m.get('account_ref')
                and all(row[k] == billed[k] for k in COUNTERS)):
            return False
    if mode == 'subscription_receipt':
        return subscription_receipt(m, left, right)
    if mode == 'subscription':
        invoice = m.get('subscription_invoice') or {}
        if not (m.get('amount_source') == 'provider_subscription_usage'
                and invoice.get('invoice_id') and invoice.get('subscription_id')
                and invoice.get('account_ref') == m['account_ref']
                and invoice.get('currency') == m['currency'] and invoice.get('status') == 'paid'
                and number(invoice.get('amount_paid_usd'))
                and re.fullmatch(r'[0-9a-f]{64}', str(invoice.get('document_sha256', '')))):
            return False
        try:
            start, end = (datetime.fromisoformat(invoice[key]) for key in ('period_start', 'period_end'))
            if start.tzinfo is None or end.tzinfo is None or start >= end:
                return False
            for request_id, row in right.items():
                at = datetime.fromisoformat(row['occurred_at'])
                if not (at.tzinfo is not None and start <= at < end
                        and row.get('subscription_id') == invoice['subscription_id']
                        and row.get('included_in_subscription') is True and row['debit_usd'] == 0
                        and left[request_id].get('occurred_at') == row['occurred_at']):
                    return False
        except (KeyError, TypeError, ValueError):
            return False
        # Invoice cash is charged for the billing period. Token cost attribution
        # remains a separate amount, verified by the application billing gate.
        return True
    if mode != 'per_request' or m.get('amount_source') != 'provider_actual_debit':
        return False
    # Do not accept a caller-supplied broad tolerance or infer debit from token prices.
    total = sum((Decimal(str(r['debit_usd'])) for r in supplier), Decimal(0))
    return abs(total - Decimal(str(m['application_cost_usd']))) <= Decimal('0.000001')


def subscription_receipt(m: dict, app: dict, supplier: dict) -> bool:
    """Paid-period receipt and exported USD attribution, accepted by the user.

    OpenCode Go exports product/workspace/time, but not a subscription ID or
    per-request cash debit. Keep the paid receipt currency independent of USD
    token attribution; do not invent either an FX conversion or a cash debit.
    """
    invoice = m.get('subscription_invoice') or {}
    if not (m.get('amount_source') == 'provider_export_attribution'
            and m.get('export_truncated') is False and m.get('extra_usage_enabled') is False
            and m.get('product') == 'go' and invoice.get('product') == m['product']
            and invoice.get('invoice_id') and invoice.get('account_ref') == m['account_ref']
            and invoice.get('status') == 'paid' and invoice.get('currency') in ('CNY', 'USD')
            and number(invoice.get('amount_paid')) and invoice['amount_paid'] > 0
            and re.fullmatch(r'[0-9a-f]{64}', str(invoice.get('document_sha256', '')))
            and re.fullmatch(r'[0-9a-f]{64}', str(m.get('provider_export_sha256', '')))):
        return False
    try:
        receipt_date = date.fromisoformat(invoice['receipt_date'])
        end = datetime.fromisoformat(invoice['paid_until'])
        # The UI exposes a paid-until timestamp and a dated renewal receipt,
        # not an exact cycle-start timestamp. Do not invent that timestamp.
        if end.tzinfo is None or receipt_date >= end.date():
            return False
        for request_id, row in supplier.items():
            at = datetime.fromisoformat(row['occurred_at'])
            if not (at.tzinfo is not None and at < end
                    and at.astimezone(end.tzinfo).date() >= receipt_date
                    and row.get('product') == m['product'] and 'debit_usd' not in row
                    and number(row.get('attribution_usd'))
                    and app[request_id].get('occurred_at') == row['occurred_at']):
                return False
    except (KeyError, TypeError, ValueError):
        return False
    total = sum((Decimal(str(row['attribution_usd'])) for row in supplier.values()), Decimal(0))
    return abs(total - Decimal(str(m['application_cost_usd']))) <= Decimal('0.000001')


CHECKS = dict(zip(IDS, (blocked_dependencies, complete_retry_usage, stable_oauth, supplier_debit)))


def evaluate(evidence: dict, *, expected_release: str = '', allow_synthetic: bool = False) -> list[str]:
    failures = []
    if evidence.get('kind') != 'production' and not (allow_synthetic and evidence.get('kind') == 'synthetic'):
        failures.append('matrix: synthetic or unspecified evidence cannot close production findings')
    release, run_id = evidence.get('release'), evidence.get('run_id')
    if not release or (expected_release and release != expected_release) or not run_id:
        failures.append('matrix: tested release/run missing or release mismatch')
    results = evidence.get('results') or []
    if not isinstance(results, list):
        results = []
    if not unique_rows(results, 'id') or {r.get('id') for r in results if isinstance(r, dict)} != set(IDS):
        failures.append('matrix: exact four unique finding IDs required')
    for case_id, check in CHECKS.items():
        row = next((r for r in results if isinstance(r, dict) and r.get('id') == case_id), {})
        m, refs = row.get('metrics') or {}, row.get('evidence_refs') or []
        valid_refs = bool(refs) and all(isinstance(ref, dict) and ref.get('path')
                                      and re.fullmatch(r'[0-9a-f]{64}', str(ref.get('sha256', ''))) for ref in refs)
        try:
            passed = check(m)
            if case_id == IDS[3] and m.get('billing_mode') in ('subscription', 'subscription_receipt'):
                digest = (m.get('subscription_invoice') or {}).get('document_sha256')
                passed = passed and any(ref.get('role') == 'supplier_invoice' and ref.get('sha256') == digest
                                        for ref in refs if isinstance(ref, dict))
                if m.get('billing_mode') == 'subscription_receipt':
                    passed = passed and any(ref.get('role') == 'supplier_export'
                                            and ref.get('sha256') == m.get('provider_export_sha256')
                                            for ref in refs if isinstance(ref, dict))
                    passed = passed and {'supplier_paid_status', 'subscription_coverage'} <= {
                        ref.get('role') for ref in refs if isinstance(ref, dict)}
        except (TypeError, ValueError, KeyError, AttributeError):
            passed = False
        if row.get('status') != 'pass' or not valid_refs or m.get('run_id') != run_id \
                or m.get('release') != release or not passed:
            failures.append(f'{case_id}: absent, unverified or inconsistent evidence')
    # Supplier reconciliation must cover the same requests as the retry ledger,
    # not a convenient subset which excludes the billable empty attempts.
    by_id = {r['id']: r for r in results if isinstance(r, dict) and isinstance(r.get('id'), str)}
    retry = (by_id.get(IDS[1], {}).get('metrics') or {}).get('attempts')
    billed = (by_id.get(IDS[3], {}).get('metrics') or {}).get('application_requests')
    if unique_rows(retry, 'request_id') and unique_rows(billed, 'request_id'):
        left, right = ({r['request_id']: r for r in rows} for rows in (retry, billed))
        if left.keys() != right.keys() or any(
                any(left[key].get(k) != right[key].get(k) for k in COUNTERS) for key in left.keys() & right.keys()):
            failures.append(f'{IDS[3]}: supplier reconciliation omits or changes retry requests')
    elif not any(f.startswith(IDS[3] + ':') for f in failures):
        failures.append(f'{IDS[3]}: complete retry request ledger required')
    return failures


def verify_files(evidence: dict, base: Path) -> list[str]:
    failures = []
    for row in evidence.get('results', []):
        for ref in row.get('evidence_refs', []):
            try:
                path = base / ref['path']
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != ref['sha256']:
                    failures.append(f"{row['id']}: evidence file missing or digest mismatch")
            except (OSError, KeyError, TypeError):
                failures.append(f"{row.get('id')}: unreadable evidence reference")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence', type=Path)
    parser.add_argument('--expected-release', required=True, help='exact deployed image/revision under acceptance')
    args = parser.parse_args()
    try:
        evidence = json.loads(args.evidence.read_text(encoding='utf-8'))
        if not isinstance(evidence, dict):
            raise TypeError('evidence must be an object')
        failures = evaluate(evidence, expected_release=args.expected_release)
        failures += verify_files(evidence, args.evidence.parent)
    except (OSError, ValueError, TypeError, AttributeError):
        failures = ['matrix: malformed or unreadable evidence']
    print('FAIL' if failures else 'PASS: all four production findings have matching evidence')
    for failure in failures:
        print(f'- {failure}')
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())

#!/usr/bin/env python3
"""Read-only comparison of a complete OpenCode UI export and application usage.

Exports can contain credentials in requestHeaders. Only allowlisted accounting
fields leave this process. A token cost is never proof of settled cash payment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path


def count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError('Missing or invalid token usage')
    return value


def amount(value: object) -> Decimal:
    if type(value) not in (int, float, str):
        raise ValueError('Missing or invalid attribution amount')
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('Missing or invalid attribution amount') from None
    if not result.is_finite() or result < 0:
        raise ValueError('Missing or invalid attribution amount')
    return result


def reconcile(export: dict, app: dict) -> dict:
    if export.get('truncated') is not False or not isinstance(export.get('items'), list):
        raise ValueError('A complete non-truncated provider export is required')
    rid = app.get('run_id')
    if not isinstance(rid, str) or not rid:
        raise ValueError('Application run ID required')
    rows = [r for r in export['items'] if isinstance(r, dict) and r.get('sessionID') == 'video2md-' + rid]
    if not rows:
        raise ValueError('No independently correlated provider requests for this run')
    requests = []
    seen = set()
    for row in rows:
        request_id = row.get('requestID')
        if not isinstance(request_id, str) or not request_id or request_id in seen:
            raise ValueError('Missing or duplicate provider request ID')
        seen.add(request_id)
        cached = count(row.get('cacheReadTokens'))
        # Export inputTokens excludes separately reported cache reads/writes.
        input_tokens = count(row.get('inputTokens')) + cached + count(row.get('cacheWriteTokens'))
        requests.append({'request_id': request_id, 'input_tokens': input_tokens,
                         'output_tokens': count(row.get('outputTokens')), 'cached_tokens': cached,
                         'attribution_usd': str(amount(row.get('cost')))})
    names = {'llm_in_tok': 'input_tokens', 'llm_out_tok': 'output_tokens', 'llm_cached_in_tok': 'cached_tokens'}
    provider_usage = {key: sum(row[column] for row in requests) for key, column in names.items()}
    recorded = app.get('usage') or {}
    app_usage = {key: count(recorded.get(key, 0)) for key in names}
    total = sum((Decimal(row['attribution_usd']) for row in requests), Decimal(0))
    app_cost = amount(app.get('cost_usd'))
    return {'kind': 'independent_provider_export_reconciliation', 'run_id': rid,
            'provider_request_count': len(requests), 'requests': requests,
            'provider_usage': provider_usage, 'application_usage': app_usage,
            'missing_usage': {key: provider_usage[key] - app_usage[key] for key in names},
            'usage_matches': provider_usage == app_usage,
            'provider_attribution_usd': str(total), 'application_attribution_usd': str(app_cost),
            'attribution_matches': abs(total - app_cost) <= Decimal('0.000001'),
            'supplier_cash_verified': False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('provider_export', type=Path)
    parser.add_argument('application_evidence', type=Path)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        raw = args.provider_export.read_bytes()
        app = json.loads(args.application_evidence.read_text())
        rows = app if isinstance(app, list) else app.get('runs', [])
        matches = [r for r in rows if r.get('run_id') == args.run_id]
        if len(matches) != 1:
            raise ValueError('Exactly one application run record required')
        result = reconcile(json.loads(raw), matches[0])
        result['provider_export_sha256'] = hashlib.sha256(raw).hexdigest()
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    except (OSError, ValueError, TypeError, AttributeError):
        print('FAIL: incomplete or invalid reconciliation evidence; source values suppressed')
        return 2
    passed = result['usage_matches'] and result['attribution_matches']
    print(('PASS' if passed else 'FAIL') + ': token usage and attribution comparison; cash settlement checked separately')
    return int(not passed)


if __name__ == '__main__':
    raise SystemExit(main())

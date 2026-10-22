"""Independent export reconciliation must detect missing attempts and cache usage."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('opencode_reconciliation', ROOT / 'scripts/reconcile_opencode_usage.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def fixture():
    rows = [
        {'requestID': 'empty', 'sessionID': 'video2md-fixture1', 'model': 'glm',
         'inputTokens': 3, 'cacheReadTokens': 9344, 'cacheWriteTokens': 0,
         'outputTokens': 4000, 'cost': 0.00228077},
        {'requestID': 'success', 'sessionID': 'video2md-fixture1', 'model': 'glm',
         'inputTokens': 3, 'cacheReadTokens': 9344, 'cacheWriteTokens': 0,
         'outputTokens': 3931, 'cost': 0.00224627},
    ]
    app = {'run_id': 'fixture1', 'usage': {'llm_in_tok': 18694, 'llm_out_tok': 7931,
                                        'llm_cached_in_tok': 18688}, 'cost_usd': 0.004527}
    return {'items': rows, 'truncated': False}, app


def test_independent_cache_columns_normalized_and_cash_separate():
    export, app = fixture()
    result = module.reconcile(export, app)
    assert result['usage_matches'] is True
    assert result['attribution_matches'] is True
    assert result['supplier_cash_verified'] is False
    assert result['provider_usage']['llm_in_tok'] == 18694
    assert result['provider_usage']['llm_cached_in_tok'] == 18688


@pytest.mark.parametrize('counter', ['llm_in_tok', 'llm_out_tok', 'llm_cached_in_tok'])
def test_missing_attempt_usage_cannot_pass(counter):
    export, app = fixture()
    app['usage'][counter] -= 1
    result = module.reconcile(export, app)
    assert result['usage_matches'] is False
    assert result['missing_usage'][counter] == 1


@pytest.mark.parametrize('defect', ['truncated', 'duplicate', 'missing-usage', 'negative', 'boolean', 'no-run'])
def test_incomplete_or_invalid_export_rejected(defect):
    export, app = fixture()
    if defect == 'truncated':
        export['truncated'] = True
    elif defect == 'duplicate':
        export['items'][1]['requestID'] = 'empty'
    elif defect == 'missing-usage':
        export['items'][0].pop('outputTokens')
    elif defect == 'negative':
        export['items'][0]['cost'] = -1
    elif defect == 'boolean':
        export['items'][0]['inputTokens'] = True
    else:
        app['run_id'] = 'other'
    with pytest.raises(ValueError):
        module.reconcile(export, app)


def test_unrelated_requests_and_private_fields_never_enter_report():
    export, app = fixture()
    export['items'][0]['requestHeaders'] = {'authorization': 'Bearer SECRET_CANARY'}
    export['items'][0]['city'] = 'PRIVATE_CITY'
    export['items'].append({'sessionID': 'video2md-unrelated', 'requestID': 'other'})
    result = module.reconcile(export, app)
    encoded = json.dumps(result)
    assert result['provider_request_count'] == 2
    assert not any(s in encoded for s in ('SECRET_CANARY', 'PRIVATE_CITY', 'authorization', 'requestHeaders'))


def test_token_price_amount_is_never_treated_as_settled_cash():
    export, app = fixture()
    app['cost_usd'] = 100
    result = module.reconcile(export, app)
    assert result['usage_matches'] is True
    assert result['attribution_matches'] is False
    assert result['supplier_cash_verified'] is False

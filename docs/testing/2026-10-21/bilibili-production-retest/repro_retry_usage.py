"""Zero-network diagnostic of retry accounting in the actual llm.py module.

Configuration, storage and telemetry sinks are process-local stubs. No project
JSON, production data, credentials or external service is read or written.
"""
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import httpx

root = Path(__file__).resolve().parents[4]
package = ModuleType('video2md')
package.__path__ = [str(root / 'backend/video2md')]
sys.modules['video2md'] = package


def stub(name, **attrs):
    module = ModuleType('video2md.' + name)
    module.__dict__.update(attrs)
    sys.modules[module.__name__] = module
    setattr(package, name, module)


observed = []
metered = []
stub('config', ENV={})
stub('util', current_log=lambda: None, current_run_id=lambda: 'offline-retry', redact=str)
stub('llm_settings', current=lambda: None)
stub('observability', metric_llm=lambda *args: observed.append(args))
stub('runs', bump_usage=lambda **values: metered.append(values))
spec = importlib.util.spec_from_file_location('video2md.llm', root / 'backend/video2md/llm.py')
llm = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = llm
spec.loader.exec_module(llm)
llm.LLM_BASE = 'https://mock.invalid/v1'
llm.LLM_KEY = 'offline-dummy'
llm._sleep = lambda _: None


def check_case(empty_first):
    observed.clear()
    metered.clear()
    responses = []
    if empty_first:
        responses.append({'choices': [{'message': {'content': ''}}],
                          'usage': {'prompt_tokens': 100, 'completion_tokens': 40}})
    responses.append({'choices': [{'message': {'content': 'ok'}}],
                      'usage': {'prompt_tokens': 200, 'completion_tokens': 60}})
    delivered = []

    def handle(request):
        assert request.url.host == 'mock.invalid'
        item = responses[len(delivered)]
        delivered.append(item)
        return httpx.Response(200, json=item)

    llm._TRANSPORT = httpx.MockTransport(handle)
    llm.chat('dummy', 'dummy', task='retry-accounting', temperature=0, max_tokens=100)
    expected = {'llm_in_tok': sum(r['usage']['prompt_tokens'] for r in delivered),
                'llm_out_tok': sum(r['usage']['completion_tokens'] for r in delivered)}
    actual = {k: sum(row.get(k, 0) for row in metered) for k in expected}
    return {'case': 'empty_200_then_success' if empty_first else 'single_success_control',
            'requests': len(delivered), 'observations': len(observed),
            'expected_usage': expected, 'recorded_usage': actual,
            'passed': actual == expected}


parser_meta = {}
empty_with_usage = httpx.Response(200, json={
    'choices': [{'message': {'content': ''}}],
    'usage': {'prompt_tokens': 100, 'completion_tokens': 40},
})
assert llm._collect_stream(empty_with_usage, parser_meta) == ''
assert parser_meta['usage'] == {'prompt_tokens': 100, 'completion_tokens': 40}
results = [check_case(False), check_case(True)]
print(json.dumps({'kind': 'synthetic_zero_network_reproduction',
                  'empty_response_parser_preserves_usage': True,
                  'results': results}, indent=2))
assert all(row['passed'] for row in results), 'Retry usage is lost before successful completion'

"""Truthful client identity and stable run sessions for OpenCode Go."""
import json

import httpx
import pytest


def test_opencode_keys_are_redacted_from_run_logs():
    from video2md.util import redact

    assert redact('provider error oc_sk_example_private_123456') == 'provider error oc_sk_***'


@pytest.mark.parametrize('base', ['https://opencode.ai/inference/go/openai/v1',
                                  'https://other-provider.test/v1'])
def test_chat_identifies_video2md_and_scopes_opencode_session(monkeypatch, base):
    from video2md import llm, util
    from video2md.llm_settings import _CURRENT

    observed = []

    def respond(request):
        observed.append(request)
        if len(observed) == 1:
            return httpx.Response(429, text='retry')
        return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]})

    monkeypatch.setattr(llm, 'LLM_BASE', base)
    monkeypatch.setattr(llm, 'LLM_KEY', 'test-secret')
    monkeypatch.setattr(llm, 'LLM_MODEL', 'glm-5.3-flash')
    monkeypatch.setattr(llm, '_TRANSPORT', httpx.MockTransport(respond))
    monkeypatch.setattr(llm, '_sleep', lambda _: None)
    monkeypatch.setattr(llm, '_llm_observed', lambda *args: None)
    scope = _CURRENT.set(None)
    previous = util.current_run_id()
    util.bind_current_run('test-run-session')
    try:
        assert llm.chat('integration system', 'test', task='metadata', temperature=0,
                        max_tokens=64) == 'OK'
        for request in observed:
            assert request.headers['user-agent'] == 'video2md/0.2.0'
            assert json.loads(request.content)['model'] == 'glm-5.3-flash'
        sessions = [r.headers.get('x-opencode-session') for r in observed]
        assert sessions == (['video2md-test-run-session'] * 2 if 'opencode.ai' in base
                            else [None, None])
    finally:
        util.bind_current_run(previous)
        _CURRENT.reset(scope)

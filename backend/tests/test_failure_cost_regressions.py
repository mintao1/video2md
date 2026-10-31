"""Replay production failure/cost boundaries without network or paid inference."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest


def record():
    url = 'https://example.test/part1'
    return {'run_id': 'failure-cost-test', 'total': 1, 'entries': [{'url': url}],
            'artifacts': {url: {'transcript': '内容' * 150,
                               'cand_manifest': 'frame.jpg [00:00]',
                               'published': [['frame.jpg', 0]]}}}


@pytest.mark.parametrize('stage', ['transcribe', 'frames', 'ocr', 'select', 'clips'])
def test_failed_dependency_spends_no_note_tokens(stage, monkeypatch):
    from video2md import graph

    run = record()
    run['partial_failures'] = {stage: ['1']}
    monkeypatch.setattr(graph, '_reload', lambda *args: run)
    monkeypatch.setattr(graph, '_persist', lambda *args: None)
    monkeypatch.setattr(graph, 'llm_task', lambda *a, **kw: pytest.fail('paid call after dependency failure'))
    result = graph.node_note_entry({'run_id': run['run_id']})
    assert result['entry_idx'] == 2
    assert stage in result['errors'][-1]
    assert run['partial_failures']['note'] == ['1']


def test_missing_remote_clips_rejected_before_model(monkeypatch):
    from video2md import graph

    run = record()
    monkeypatch.setitem(graph.ENV, 'V2MD_CLIPS_REMOTE', 'http://gpu.test')
    monkeypatch.setattr(graph, '_reload', lambda *args: run)
    monkeypatch.setattr(graph, '_persist', lambda *args: None)
    monkeypatch.setattr(graph, 'llm_task', lambda *a, **kw: pytest.fail('missing clips must not spend'))
    result = graph.node_note_entry({'run_id': run['run_id']})
    assert 'clips' in result['errors'][-1]


@pytest.mark.parametrize('image_post', [False, True])
def test_healthy_entry_still_generates(image_post, monkeypatch, tmp_path):
    from video2md import graph

    run = record()
    run['tmp'] = str(tmp_path)
    art = run['artifacts'][run['entries'][0]['url']]
    art['clips'] = {'clip.mp4': [0, 1]}
    if image_post:
        art['images'] = ['photo.jpg']
        art.pop('clips')
    tasks = []
    monkeypatch.setitem(graph.ENV, 'V2MD_CLIPS_REMOTE', 'http://gpu.test')
    monkeypatch.setattr(graph, '_reload', lambda *args: run)
    monkeypatch.setattr(graph, '_persist', lambda *args: None)
    monkeypatch.setattr(graph, 'RunLog', lambda *a, **kw: SimpleNamespace(spans=[]))
    monkeypatch.setattr(graph, 'llm_task', lambda task, **kw: tasks.append(task) or '{}')
    monkeypatch.setattr(graph, 'assemble_entry_note', lambda *a: {})
    def stop_at_write(*args):
        raise SystemExit('fixture stops before writing')
    monkeypatch.setitem(graph._STAGE_FNS, 'note', stop_at_write)
    graph.node_note_entry({'run_id': run['run_id'], 'review': True})
    assert tasks == ['organize', 'review', 'metadata']


def test_selection_excludes_failed_ocr_entry_but_keeps_original_index(monkeypatch):
    from video2md import graph

    run = record()
    run['total'] = 2
    run['entries'].append({'url': 'https://example.test/part2'})
    run['artifacts'][run['entries'][1]['url']] = {
        'transcript': '健康内容' * 100, 'cand_manifest': 'healthy.jpg [00:00]'}
    run['partial_failures'] = {'ocr': ['1']}
    captured = []
    monkeypatch.setattr(graph, '_reload', lambda *args: run)
    monkeypatch.setattr(graph, 'select_frames', lambda *args: captured.append(args) or ({}, 1, 'ok'))
    graph.node_select_frames({'run_id': run['run_id']})
    assert len(captured) == 1
    text, candidates, total = captured[0]
    assert 'part1' not in text and 'frame.jpg' not in candidates
    assert '条目 2/2' in text and 'healthy.jpg' in candidates and total == 2


def test_all_ocr_failures_skip_selection_model(monkeypatch):
    from video2md import graph

    run = record()
    run['partial_failures'] = {'ocr': ['1']}
    monkeypatch.setattr(graph, '_reload', lambda *args: run)
    monkeypatch.setattr(graph, 'select_frames', lambda *a: pytest.fail('all OCR failed'))
    result = graph.node_select_frames({'run_id': run['run_id']})
    assert result['select_payload'] == {}


@pytest.mark.parametrize('mode', ['empty_then_success', 'exhausted', 'parse_failure', 'stream_failure', 'auth_after_empty', 'success', 'no_usage'])
def test_retry_accounts_every_observed_usage_once(mode, monkeypatch):
    from video2md import llm, llm_settings, observability, runs

    monkeypatch.setattr(llm_settings, 'current', lambda: None)
    monkeypatch.setattr(llm, '_FAKE_LLM', False)
    monkeypatch.setattr(llm, 'LLM_BASE', 'https://llm.test/v1')
    monkeypatch.setattr(llm, 'LLM_KEY', 'synthetic-key')
    monkeypatch.setattr(llm, '_sleep', lambda _: None)
    recorded, metrics = [], []
    monkeypatch.setattr(runs, 'bump_usage', lambda **kw: recorded.append(kw))
    monkeypatch.setattr(observability, 'metric_llm', lambda *a: metrics.append(a))
    first = {'prompt_tokens': 100, 'completion_tokens': 40,
             'prompt_tokens_details': {'cached_tokens': 80},
             'completion_tokens_details': {'reasoning_tokens': 30}}
    second = {'prompt_tokens': 200, 'completion_tokens': 60,
              'prompt_tokens_details': {'cached_tokens': 120},
              'completion_tokens_details': {'reasoning_tokens': 20}}
    def response(text, usage):
        return httpx.Response(200, json={'choices': [{'message': {'content': text}}], 'usage': usage})
    if mode == 'no_usage':
        sequence = [httpx.Response(200, json={'choices': [{'message': {'content': 'ok'}}]})]
        expected = (0, 0, 0)
    elif mode == 'success':
        sequence = [response('ok', second)]
        expected = (200, 60, 120)
    elif mode == 'exhausted':
        sequence = [response('', first), response(' ', first), response('', first)]
        expected = (300, 120, 240)
    elif mode == 'auth_after_empty':
        sequence = [response('', first), httpx.Response(401)]
        expected = (100, 40, 80)
    elif mode == 'parse_failure':
        sequence = [httpx.Response(200, json={'choices': [], 'usage': first}), response('ok', second)]
        expected = (300, 100, 200)
    elif mode == 'stream_failure':
        class Interrupted(httpx.SyncByteStream):
            def __iter__(self):
                yield ('data: ' + json.dumps({'usage': first, 'choices': []}) + '\n\n').encode()
                raise httpx.ReadError('synthetic interrupted stream')
        sequence = [httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Interrupted()),
                    response('ok', second)]
        expected = (300, 100, 200)
    else:
        # Repeated cumulative SSE usage chunks within one attempt must not double count.
        chunk = json.dumps({'usage': first, 'choices': []})
        sequence = [httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                   text=f'data: {chunk}\n\ndata: {chunk}\n\ndata: [DONE]\n\n'),
                    response('ok', second)]
        expected = (300, 100, 200)
    responses = iter(sequence)
    requests = []
    def send(request):
        requests.append(request)
        return next(responses)
    monkeypatch.setattr(llm, '_TRANSPORT', httpx.MockTransport(send))
    span = {}
    if mode in ('exhausted', 'auth_after_empty'):
        with pytest.raises(RuntimeError):
            llm.chat('s', 'u', task='metadata', temperature=0, max_tokens=100, span_rec=span)
    else:
        assert llm.chat('s', 'u', task='metadata', temperature=0, max_tokens=100, span_rec=span) == 'ok'
    actual = tuple(sum(item[key] for item in recorded) for key in
                   ('llm_in_tok', 'llm_out_tok', 'llm_cached_in_tok'))
    assert actual == expected
    assert len(requests) == len(sequence)
    assert len(span['attempt_usage']) == len(sequence)
    assert span['usage_complete'] == (mode not in ('no_usage', 'auth_after_empty'))
    if mode == 'no_usage':
        assert 'usage' not in span and recorded == []
    else:
        assert (span['usage']['prompt_tokens'], span['usage']['completion_tokens']) == expected[:2]
    assert sum(row[3] for row in metrics) == expected[0]


@pytest.mark.parametrize('rows', [[[0, '', 0, 'decode failed']],
                                  [[0, 'ok', .9, None], [1, '', 0, 'decode failed']]])
def test_ocr_frame_errors_cannot_be_promoted_to_success(rows, monkeypatch, tmp_path):
    from video2md import runs
    art = {'candidates': [[i, f'f{i}.jpg'] for i in range(len(rows))],
           'segs': [{'start':0,'end':2,'text':'healthy speech','conf':.95}], 'video':'fixture.mp4'}
    monkeypatch.setattr(runs, 'ocr_candidates', lambda *a, **kw: ([], rows))
    with pytest.raises(SystemExit, match='OCR'):
        runs._stage_ocr({}, {'url':'fixture'}, art, tmp_path, {})
    assert 'transcript' not in art and 'cand_manifest' not in art


def test_ocr_empty_text_without_error_is_valid_and_does_not_force_retry(monkeypatch, tmp_path):
    from video2md import runs
    art = {'candidates': [[0, 'f.jpg']], 'segs': [{'start':0,'end':2,'text':'healthy speech','conf':.95}],
           'video':'fixture.mp4'}
    monkeypatch.setattr(runs, 'ocr_candidates', lambda *a, **kw: ([], [[0,'',0,None]]))
    runs._stage_ocr({}, {'url':'fixture'}, art, tmp_path, {})
    assert art['ocr_raw'] == [[0,'',0,None]] and art['transcript']


def test_invalid_cached_ocr_is_retried(monkeypatch, tmp_path):
    from video2md import runs
    art = {'candidates': [[0,'f.jpg']], 'segs':[{'start':0,'end':2,'text':'speech','conf':.95}],
           'video':'fixture.mp4', 'ocr_raw':[[0,'',0,'old failure']]}
    calls = []
    monkeypatch.setattr(runs, 'ocr_candidates', lambda *a, **kw: calls.append(True) or ([],[[0,'',0,None]]))
    runs._stage_ocr({}, {'url':'fixture'}, art, tmp_path, {})
    assert calls == [True] and art['ocr_raw'][0][3] is None


def test_stage_loop_does_not_skip_failed_cached_ocr(monkeypatch, tmp_path):
    from contextlib import contextmanager, nullcontext

    from video2md import runs
    art = {'candidates': [[0,'f.jpg']], 'segs':[{'start':0,'end':2,'text':'speech','conf':.95}],
           'video':'fixture.mp4', 'ocr_raw':[[0,'',0,'old failure']]}
    run = {'run_id':'ocr-cache-fixture','tmp':str(tmp_path),'entries':[{'url':'entry'}],
           'artifacts':{'entry':art},'stages':{}}
    calls = []
    monkeypatch.setattr(runs, '_runs_file_lock', nullcontext)
    monkeypatch.setattr(runs, '_runs_merge', lambda value: None)
    monkeypatch.setattr(runs, 'ocr_candidates', lambda *a, **kw: calls.append(True) or ([],[[0,'',0,None]]))

    class Log:
        @contextmanager
        def span(self, *args):
            yield {}

        def info(self, *args, **kw):
            pass

    runs._run_stage_inner(run, 'ocr', {}, Log())
    assert calls == [True] and not run.get('partial_failures', {}).get('ocr')

"""Do not spend LLM tokens on content the writing gate will reject."""
import pytest


def _record(text):
    return {'run_id': 'quality-test', 'total': 1, 'entries': [{'url': 'https://example.test/a'}],
            'artifacts': {'https://example.test/a': {'transcript': text,
                          'cand_manifest': 'frame.jpg [00:00]'}}}


def test_short_transcript_is_rejected_before_selection_llm(monkeypatch):
    from video2md import graph

    record = _record('字' * 193)
    monkeypatch.setattr(graph, '_reload', lambda *args: record)
    monkeypatch.setattr(graph, 'select_frames', lambda *args: pytest.fail('must not call LLM'))
    result = graph.node_select_frames({'run_id': 'quality-test'})
    assert result['status'] == 'error'
    assert '转录本过短' in result['errors'][-1]


def test_short_entry_is_rejected_before_organize_and_metadata(monkeypatch):
    from video2md import graph

    record = _record('字' * 193)
    monkeypatch.setattr(graph, '_reload', lambda *args: record)
    monkeypatch.setattr(graph, '_persist', lambda *args: None)
    monkeypatch.setattr(graph, 'llm_task', lambda *args, **kwargs: pytest.fail('must not call LLM'))
    result = graph.node_note_entry({'run_id': 'quality-test'})
    assert result['entry_idx'] == 2
    assert '转录本过短' in result['errors'][-1]
    assert record['partial_failures']['note'] == ['1']


def test_exact_quality_threshold_still_selects_frames(monkeypatch):
    from video2md import graph

    record = _record('字' * graph.MIN_NOTE_TRANSCRIPT_CHARS)
    monkeypatch.setattr(graph, '_reload', lambda *args: record)
    monkeypatch.setattr(graph, 'select_frames', lambda *args: ({'frames': []}, 0, 'valid'))
    assert graph.node_select_frames({'run_id': 'quality-test'})['select_note'] == 'valid'


def test_partial_finalize_preserves_quality_failure_reason(monkeypatch):
    from video2md import graph

    record = _record('字' * 193)
    record['partial_failures'] = {'note': ['1']}
    monkeypatch.setattr(graph, '_reload', lambda *args: record)
    monkeypatch.setattr(graph, '_persist', lambda *args: None)
    monkeypatch.setattr(graph.artsink, 'current_mode', lambda: 'vault')
    graph.node_finalize({'run_id': 'quality-test', 'errors': ['note: 转录本过短']})
    assert record['errors'][0] == 'note: 转录本过短'
    assert '教材产物缺失' in record['errors'][-1]


def test_finalize_rejects_completed_status_when_note_artifact_is_missing(monkeypatch):
    from video2md import graph

    record = _record('字' * graph.MIN_NOTE_TRANSCRIPT_CHARS)
    monkeypatch.setattr(graph, '_reload', lambda *args: record)
    monkeypatch.setattr(graph, '_persist', lambda *args: None)
    monkeypatch.setattr(graph.artsink, 'current_mode', lambda: 'vault')
    result = graph.node_finalize({'run_id': 'quality-test'})
    assert result['status'] == 'error'
    assert record['partial_failures']['note'] == ['1']
    assert '教材产物缺失' in record['errors'][-1]


def test_finalize_allows_completed_status_when_note_artifact_is_nonempty(monkeypatch, tmp_path):
    from video2md import graph

    record = _record('字' * graph.MIN_NOTE_TRANSCRIPT_CHARS)
    note = tmp_path / 'lesson.md'
    note.write_text('# lesson\n正文', encoding='utf-8')
    record['artifacts'][record['entries'][0]['url']]['note'] = str(note)
    monkeypatch.setattr(graph, '_reload', lambda *args: record)
    monkeypatch.setattr(graph, '_persist', lambda *args: None)
    monkeypatch.setattr(graph.artsink, 'current_mode', lambda: 'vault')
    result = graph.node_finalize({'run_id': 'quality-test'})
    assert result['status'] == 'done'


def test_compiled_graph_stops_short_content_before_publishing_or_clipping(monkeypatch):
    from video2md import graph

    record = _record('字' * 193)
    visited = []

    def factory(stage, args_of=None):
        def node(state):
            visited.append(stage)
            return {'run': record}
        return node

    monkeypatch.setattr(graph, '_stage_node', factory)
    monkeypatch.setattr(graph, '_reload', lambda *args: record)
    monkeypatch.setattr(graph, '_persist', lambda *args: None)
    monkeypatch.setattr(graph, 'select_frames', lambda *args: pytest.fail('must not call LLM'))
    result = graph.build_graph().invoke({'run_id': 'quality-test', 'run': record})
    assert result['status'] == 'error'
    assert visited == ['download', 'transcribe', 'frames', 'ocr']

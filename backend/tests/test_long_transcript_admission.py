"""Long transcript regression seam: OCR admission and bounded note LLM calls."""
from __future__ import annotations

import json
from contextlib import nullcontext


def test_ocr_admits_the_production_81411_char_transcript(monkeypatch, tmp_path):
    from video2md import runs

    video = tmp_path / "video.mp4"
    video.write_bytes(b"fixture")
    monkeypatch.setattr(runs, "ocr_candidates",
                        lambda frames, engine: ([], [[0.0, "", 0.0, None]]))
    monkeypatch.setattr(runs, "merge_timeline",
                        lambda segs, items: ("字" * 81411, []))
    art = {
        "video": str(video),
        "candidates": [[0.0, "frame.jpg"]],
        "segs": [{"start": 0, "end": 1, "text": "短"}],
    }
    runs._stage_ocr({}, {"title": "fixture"}, art, tmp_path, {})
    assert len(art["transcript"]) == 81411


def test_long_note_transcript_is_chunked_before_llm(monkeypatch, tmp_path):
    from video2md import graph

    url = "https://example.test/long"
    transcript = "\n".join(f"[{i:02d}:00] " + "字" * 1000 for i in range(81))
    record = {
        "run_id": "long-note-test",
        "tmp": str(tmp_path),
        "total": 1,
        "entries": [{"url": url, "title": "长稿"}],
        "artifacts": {url: {
            "transcript": transcript,
            "segs": [{"start": 0, "end": 1, "text": "语音"}],
            "manifest": "frame.jpg [00:00] OCR: —",
            "published": [],
            "video": str(tmp_path / "video.mp4"),
            "fp": "long-fp",
        }},
    }
    calls: list[tuple[str, int]] = []

    def fake_llm(task, **kwargs):
        calls.append((task, len(kwargs.get("entry_transcript") or kwargs.get("organize_text") or "")))
        if task == "metadata":
            return json.dumps({"title": "长稿", "tags": [], "summary": "长稿"})
        return "## 分段\n\n正文" + "字" * 80

    note_path = tmp_path / "note.md"
    note_path.write_text("# 长稿\n正文", encoding="utf-8")
    monkeypatch.setattr(graph, "_reload", lambda *args: record)
    monkeypatch.setattr(graph, "_persist", lambda *args: None)
    monkeypatch.setattr(graph, "llm_task", fake_llm)
    monkeypatch.setattr(graph.artsink, "sink_for", lambda run: nullcontext())
    monkeypatch.setitem(graph._STAGE_FNS, "note",
                        lambda run, entry, art, tmp, args: art.update(
                            note=str(note_path), transcript_path=str(tmp_path / "transcript.txt")))

    result = graph.node_note_entry({"run_id": "long-note-test"})

    organize_lengths = [size for task, size in calls if task == "organize"]
    assert len(organize_lengths) >= 3
    assert max(organize_lengths) <= graph.NOTE_LLM_CHUNK_CHARS
    assert any(task == "metadata" and size <= graph.NOTE_METADATA_CONTEXT_CHARS + 100
               for task, size in calls)
    assert result["entry_idx"] == 2
    assert record["artifacts"][url]["note"] == str(note_path)

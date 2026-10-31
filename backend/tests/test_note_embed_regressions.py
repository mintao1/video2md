"""Replay the real second lecture's bare and inline model image references."""
import pytest


@pytest.mark.parametrize('reference', ['![[frame.jpg]]','![[attachments/frame.jpg]]',
                                      '![[frame.jpg|640]]','![[attachments/frame.jpg|640]]'])
@pytest.mark.parametrize('inline', [False, True])
def test_known_frames_normalize_to_standalone_authorized_attachment(reference, inline):
    from video2md.clips import insert_clip_embeds
    from video2md.note import sanitize_embeds

    body = f'正文{reference}图注' if inline else f'正文\n{reference}\n*图注*'
    result = sanitize_embeds(body, {'frame.jpg'})
    assert '![[attachments/frame.jpg]]' in result.splitlines()
    assert '正文' in result and '图注' in result
    assert result == sanitize_embeds(result, {'frame.jpg'})
    final = insert_clip_embeds(result, {'frame.jpg':'clip.mp4'})
    assert '![[attachments/clip.mp4]]' in final.splitlines()


@pytest.mark.parametrize('reference', ['![[unknown.jpg]]','![[attachments/unknown.jpg]]',
    '![[../frame.jpg]]','![[attachments/../frame.jpg]]','![[https://evil.test/frame.jpg]]',
    '![[folder/frame.jpg]]'])
def test_unknown_or_traversing_image_cannot_become_authorized_reference(reference):
    from video2md.note import sanitize_embeds

    result = sanitize_embeds(f'正文\n{reference}\n*图注*', {'frame.jpg'})
    assert '![[' not in result
    assert '正文' in result


def test_empty_frame_allowlist_removes_bare_and_prefixed_images():
    from video2md.note import sanitize_embeds

    result = sanitize_embeds('text ![[frame.jpg]]\n![[attachments/frame.jpg]]', set())
    assert '![[' not in result


def test_ordinary_links_and_source_text_are_preserved():
    from video2md.note import sanitize_embeds

    body = '正文 [[transcripts/file.txt|完整转录]]\n[^1]: 正常注释'
    assert sanitize_embeds(body, {'frame.jpg'}) == body


def test_collecting_note_regressions_cannot_initialize_project_storage():
    import subprocess
    import sys
    from pathlib import Path

    code = 'import runpy,sys; runpy.run_path(sys.argv[1]); assert "video2md.config" not in sys.modules'
    result = subprocess.run([sys.executable,'-c',code,str(Path(__file__))],capture_output=True,text=True,timeout=10)
    assert result.returncode == 0, result.stderr

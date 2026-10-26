"""PDF 导出：Obsidian 风格笔记的转换口径与安全边界（正文按不可信输入处理）。"""
from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from video2pdf import pdf


@pytest.fixture
def attachments(tmp_path):
    root = tmp_path / "attachments"
    root.mkdir()
    buf = io.BytesIO()
    Image.new("RGB", (64, 36), (40, 90, 200)).save(buf, "JPEG")
    (root / "demo-f01-0105.jpg").write_bytes(buf.getvalue())
    (root / "demo-c01-0230.mp4").write_bytes(b"mp4")
    secret = tmp_path / "secret.jpg"
    secret.write_bytes(b"outside-secret")
    (root / "escape.jpg").symlink_to(secret)
    return root


NOTE = """---
title: 演示
---

> [!info] 视频信息
> - ▶️ 来源：B站
> - 📝 字幕文稿：[[transcripts/演示-transcript|完整转录]]

## 第一节（00:00）

正文 **重点**。

![[attachments/demo-f01-0105.jpg|560]]

![[attachments/demo-c01-0230.mp4]]

![[attachments/escape.jpg]]

![外链](https://example.com/track.png) <script>alert(1)</script>
"""


def test_note_conversion(attachments):
    html = pdf.note_html(NOTE, attachments)
    assert "title: 演示" not in html                        # frontmatter 去掉
    assert '<div class="callout info"><p class="callout-title">视频信息</p>' in html
    assert "完整转录" in html and "[[" not in html            # wikilink 只留显示文本
    assert '<img src="data:image/jpeg;base64,' in html       # 图片内嵌
    assert "视频片段 02:30" in html                            # 视频换时间点提示
    assert "📝" not in html and "▶️" not in html             # 装饰 emoji 去掉
    assert "<strong>重点</strong>" in html


def test_untrusted_content_cannot_reach_files_or_markup(attachments):
    html = pdf.note_html(NOTE, attachments)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert base64.b64encode(b"outside-secret").decode() not in html  # 符号链接逃逸不取
    assert "图片暂不可用：escape.jpg" in html


def test_book_layout_has_cover_toc_and_lessons(attachments):
    html = pdf.build_html("演示课程", [{"title": "第一讲", "md": NOTE}, {"title": "第二讲", "md": "正文"}],
                          attachments, source="https://example.test/v")
    assert "<h1>演示课程</h1>" in html and "共 2 讲" in html
    assert '<a href="#lesson-2">第二讲</a>' in html
    assert html.count("<article ") == 2


def test_render_pdf_produces_a_pdf_without_fetching_external_urls(attachments, monkeypatch):
    pytest.importorskip("weasyprint")
    import urllib.request

    opened = []
    original = urllib.request.OpenerDirector.open

    def spy(self, fullurl, *args, **kwargs):
        url = fullurl if isinstance(fullurl, str) else fullurl.full_url
        opened.append(url.split(":", 1)[0])
        return original(self, fullurl, *args, **kwargs)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", spy)
    data = pdf.render_pdf("演示课程", [{"title": "第一讲", "md": NOTE}], attachments)
    assert data.startswith(b"%PDF")
    assert opened and set(opened) == {"data"}  # 只打开过内嵌图片，外链从未发起请求

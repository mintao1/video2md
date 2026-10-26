"""文稿导出 PDF：把一个 run 的笔记（含 Obsidian 风格语法）排成一份可下载的 PDF。

输入是 note 阶段写出的 markdown：frontmatter、`> [!info]` callout、`![[attachments/x.jpg]]`
嵌入、`[[transcripts/x|完整转录]]` wikilink。处理口径与前端阅读器（frontend/web/src/reader.ts）一致：

- frontmatter 去掉；callout 渲染成带标题的提示框；wikilink 只留显示文本。
- 图片嵌入只认 attachments 目录里真实存在的文件，以 data URI 内嵌；视频片段在纸面上
  无法播放，换成「视频片段 mm:ss」提示。
- markdown 不放行原始 HTML（正文来自 LLM 对外部视频的改写，按不可信输入处理），
  渲染器只允许 data: 资源，正文里的外链图片、本地路径一律不取。
"""
from __future__ import annotations

import base64
import html
import re
from pathlib import Path

from markdown_it import MarkdownIt

_FRONTMATTER = re.compile(r"^---\r?\n[\s\S]*?\r?\n---(?:\r?\n|$)")
_EMBED = re.compile(r"!\[\[attachments/([^\]\n\\/|]+)(?:\|[^\]]*)?\]\]")
_WIKILINK = re.compile(r"\[\[([^\]\n|]+)(?:\|([^\]\n]+))?\]\]")
_CALLOUT_START = re.compile(r"^>\s*\[!(info|tip|warning|example)\](-)?(?:\s+(.*))?\s*$")
_CLIP_TIME = re.compile(r"-c\d+-(\d{2})(\d{2})\.mp4$", re.I)
# 彩色 emoji 在 CJK 字体里是缺字方框：纸面上去掉装饰性 emoji 与变体选择符
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0000FE0F]")
_TOKEN = "V2PDFBLOCK{}X"
_IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
_CALLOUT_LABELS = {"info": "信息", "tip": "提示", "warning": "注意", "example": "示例"}

_MD = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])

_CSS = """
@page { size: A4; margin: 22mm 20mm 24mm;
  @bottom-center { content: counter(page) " / " counter(pages); font-size: 9pt; color: #888; } }
@page :first { @bottom-center { content: none; } }
html { font-family: "Noto Serif CJK SC", "Noto Sans CJK SC", "WenQuanYi Zen Hei", serif;
  font-size: 10.5pt; line-height: 1.7; color: #1f1f1f; }
h1, h2, h3, h4 { font-family: "Noto Sans CJK SC", "WenQuanYi Zen Hei", sans-serif;
  line-height: 1.35; break-after: avoid; }
.cover { break-after: page; padding-top: 70mm; text-align: center; }
.cover h1 { font-size: 26pt; margin: 0 0 8mm; }
.cover p { color: #666; margin: 2mm 0; word-break: break-all; }
.toc { break-after: page; }
.toc h2 { font-size: 16pt; }
.toc ol { padding-left: 6mm; }
.toc a { color: inherit; text-decoration: none; }
.toc a::after { content: leader(".") target-counter(attr(href), page); }
article { break-before: page; }
article > h1.lesson { font-size: 18pt; border-bottom: 1px solid #ddd; padding-bottom: 2mm; }
img { display: block; max-width: 100%; max-height: 120mm; margin: 4mm auto; break-inside: avoid; }
.clip { color: #666; font-size: 9pt; text-align: center; margin: 2mm 0; }
.callout { border-left: 3px solid #4a7bd0; background: #f3f6fb; padding: 2mm 4mm; margin: 4mm 0;
  break-inside: avoid; }
.callout.tip { border-color: #2f9e6e; background: #f1f8f4; }
.callout.warning { border-color: #d08a2f; background: #fbf6ef; }
.callout-title { font-weight: bold; margin: 0 0 1mm; }
blockquote { border-left: 3px solid #ccc; margin: 3mm 0; padding-left: 4mm; color: #555; }
pre, code { font-family: "DejaVu Sans Mono", monospace; font-size: 9pt; }
pre { background: #f6f6f6; padding: 3mm; white-space: pre-wrap; }
table { border-collapse: collapse; margin: 3mm 0; }
th, td { border: 1px solid #ccc; padding: 1mm 2mm; }
a { color: #2a5db0; }
"""


def _image_data_uri(name: str, attachments: Path) -> str | None:
    suffix = Path(name).suffix.lower()
    path = attachments / name
    if suffix not in _IMAGE_TYPES or not path.resolve().is_relative_to(attachments.resolve()) \
            or not path.is_file():
        return None
    return f"data:{_IMAGE_TYPES[suffix]};base64,{base64.b64encode(path.read_bytes()).decode()}"


def _embed_html(name: str, attachments: Path) -> str:
    if name.lower().endswith(".mp4"):
        m = _CLIP_TIME.search(name)
        at = f" {m[1]}:{m[2]}" if m else ""
        return f'<p class="clip">视频片段{at}（在线阅读器中播放）</p>'
    uri = _image_data_uri(name, attachments)
    if uri is None:
        return f'<p class="clip">图片暂不可用：{html.escape(name)}</p>'
    return f'<img src="{uri}" alt="{html.escape(name)}">'


def _render(md: str, attachments: Path) -> str:
    """markdown（已去 frontmatter）→ HTML；嵌入与 callout 先换成占位符，渲染后再填回。"""
    blocks: list[str] = []

    def hold(fragment: str) -> str:
        blocks.append(fragment)
        return _TOKEN.format(len(blocks) - 1)

    lines, out = md.split("\n"), []
    i = 0
    while i < len(lines):
        m = _CALLOUT_START.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        body = []
        while i + 1 < len(lines) and re.match(r"^>\s?", lines[i + 1]):
            i += 1
            body.append(re.sub(r"^>\s?", "", lines[i]))
        i += 1
        kind = m[1]
        title = html.escape((m[3] or "").strip() or _CALLOUT_LABELS[kind])
        out += ["", hold(f'<div class="callout {kind}"><p class="callout-title">{title}</p>'
                         f'{_render(chr(10).join(body), attachments)}</div>'), ""]
    text = "\n".join(out)
    text = _EMBED.sub(lambda e: "\n\n" + hold(_embed_html(e[1], attachments)) + "\n\n", text)
    text = _WIKILINK.sub(lambda w: (w[2] or Path(w[1]).stem).strip(), text)
    rendered = _MD.render(_EMOJI.sub("", text))
    for n, fragment in enumerate(blocks):
        token = _TOKEN.format(n)
        rendered = rendered.replace(f"<p>{token}</p>", fragment).replace(token, fragment)
    return rendered


def note_html(md: str, attachments: Path) -> str:
    return _render(_FRONTMATTER.sub("", md.replace("\r\n", "\n"), count=1), attachments)


def build_html(title: str, notes: list[dict], attachments: Path, source: str = "") -> str:
    """notes: [{"title", "md"}]，按讲次顺序。封面 + 目录（带页码）+ 每讲一节。"""
    toc, articles = [], []
    for n, note in enumerate(notes, 1):
        lesson = html.escape(note.get("title") or f"第 {n} 讲")
        toc.append(f'<li><a href="#lesson-{n}">{lesson}</a></li>')
        articles.append(f'<article id="lesson-{n}"><h1 class="lesson">{lesson}</h1>'
                        f'{note_html(note.get("md") or "", attachments)}</article>')
    cover = [f"<h1>{html.escape(title)}</h1>", f"<p>共 {len(notes)} 讲</p>"]
    if source:
        cover.append(f"<p>{html.escape(source)}</p>")
    toc_html = (f'<nav class="toc"><h2>目录</h2><ol>{"".join(toc)}</ol></nav>'
                if len(notes) > 1 else "")
    return (f'<!doctype html><html lang="zh"><head><meta charset="utf-8">'
            f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body>"
            f'<section class="cover">{"".join(cover)}</section>{toc_html}{"".join(articles)}'
            f"</body></html>")


def render_pdf(title: str, notes: list[dict], attachments: Path, source: str = "") -> bytes:
    # 延迟导入：weasyprint 依赖系统 Pango，只有真正导出时才加载
    from weasyprint import HTML
    from weasyprint.urls import URLFetcher

    document = HTML(string=build_html(title, notes, attachments, source),
                    url_fetcher=URLFetcher(allowed_protocols=("data",)))
    return document.write_pdf()  # PDF 标题元数据取自 <title>

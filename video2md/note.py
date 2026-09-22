"""笔记组装写盘：render/write/transcript/索引/合集目录。"""
from __future__ import annotations

import re
from pathlib import Path
from datetime import date

from .config import ENV, INDEX_FILE, OUT_DIR, TRANSCRIPT_DIR
from .util import sanitize_filename



# ---------------------------------------------------------------- 质量门禁
# 实测事故：staged 路径没有 CLI 的「输出过短重试」门，Kimi 的一句拒答
# （「抱歉，我无法提供相关信息。…」）被当成正文写成了笔记并上传知识库。
_REFUSAL_RE = re.compile(r"抱歉[，,][^。！？\n]{0,16}(无法|不能)|无法提供相关信息|我无法(回答|提供)|"
                         r"对不起[，,][^。！？\n]{0,8}(无法|不能)|as an AI|I cannot (help|assist)|I'm sorry",
                         re.I)


MIN_NOTE_TRANSCRIPT_CHARS = int(ENV.get("V2MD_MIN_TRANSCRIPT_CHARS", "200"))  # 低于此视为片头/片尾噪音



def looks_like_refusal(text: str) -> bool:
    """拒答/空壳判定：命中拒答套话，或极短且无章节结构（= 根本没写出文章）。

    判正样本：「抱歉，我无法提供相关信息。有什么其他问题可以帮你吗？」（30 字、无 ##）
    不该误杀：16 秒片尾那类薄笔记 300+ 字且带 `## 片尾（00:00）` 章节头（它由转录本长度门管）。
    """
    t = (text or "").strip()
    if not t:
        return True
    if _REFUSAL_RE.search(t[:300]):
        return True
    return len(t) < 60 and not re.search(r"^#{2,3} ", t, re.M)



def sanitize_embeds(body: str, valid: set[str]) -> str:
    """剥离指向不存在帧的 ![[attachments/…]]（LLM 幻觉防线），孤立斜体图注随删。"""
    if not valid:
        return re.sub(r"!\[\[attachments/[^\]]+\]\]\n?", "", body)
    lines, out, i = body.split("\n"), [], 0
    while i < len(lines):
        m = re.fullmatch(r"!\[\[attachments/([^\]]+)\]\]", lines[i].strip())
        if m and m.group(1) not in valid:
            i += 1
            if i < len(lines) and re.fullmatch(r"\*[^*]+\*", lines[i].strip()):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)



def extract_toc(body: str) -> list[tuple[str, str]]:
    """从正文 `## 章节名（mm:ss）` 提取章节导航（正文保持干净，导航进折叠 callout）。"""
    toc = []
    for m in re.finditer(r"^## (.+?)(?:（(\d{1,2}:\d{2})）)?\s*$", body, re.M):
        if m.group(1).strip():
            toc.append((m.group(2) or "", m.group(1).strip()))
    return toc





def write_transcript(title: str, transcript: str) -> Path:
    """转录本 SSOT 落 transcripts/（覆盖同名：标题相同即同一篇笔记的重跑）。"""
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    p = TRANSCRIPT_DIR / f"{sanitize_filename(title)}-transcript.txt"
    p.write_text(transcript)
    return p



# ---------------------------------------------------------------- ④ Dify workflow








# ---------------------------------------------------------------- vault 笔记组装
def render_note(body: str, meta: dict, ctx: dict, toc: list[tuple[str, str]]) -> str:
    """组装 3B1B 式笔记：富 frontmatter + [!info] 视频信息 + 折叠章节导航 + 文稿正文。"""
    tags = list(dict.fromkeys(["视频笔记"] + meta["tags"]))
    is_img = bool(ctx.get("is_image_post"))
    fm: dict = {
        "title": meta["title"] or ctx["video"].stem,
        "lang": "zh",
        "date": date.today().isoformat(),
        "ocr_engine": ctx["ocr_engine"],
        "frames_kept": ctx["n_frames"],
        "frames_ocr": ctx["n_ocr"],
        "processor": "video2md",
        "reviewed": "false",
    }
    if is_img:
        fm["media_type"] = "图文"
        fm["images"] = ctx["n_frames"]
    else:
        fm["duration"] = ctx["dur_str"]
        fm["transcribe_model"] = ctx["model"]
        fm["transcribe_conf"] = round(ctx["conf"], 2)
    if ctx.get("playlist_title"):  # 合集/播放列表来源（分P 时能追溯第几集）
        fm["playlist"] = ctx["playlist_title"]
    if ctx.get("playlist_index"):
        fm["playlist_index"] = ctx["playlist_index"]
    if ctx.get("source_url"):
        fm["source_url"] = ctx["source_url"]
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines.append("tags:")
    lines += [f"  - {t}" for t in tags]
    lines.append("---")

    if is_img:
        info = [
            "> [!info] 图文信息",
            f"> - ▶️ 来源：{ctx['source_line']} · 图集 {ctx['n_frames']} 张",
            f"> - 📝 图集文字：[[transcripts/{ctx['transcript_name']}|完整文本]]（OCR {ctx['ocr_engine']}）",
        ]
    else:
        info = [
            "> [!info] 视频信息",
            f"> - ▶️ 来源：{ctx['source_line']} · 时长 {ctx['dur_str']}",
            f"> - 📝 字幕文稿：[[transcripts/{ctx['transcript_name']}|完整转录]]"
            f"（whisper {ctx['model']} · 置信度 {ctx['conf']:.2f}）",
            f"> - 🎞 插图：{ctx['n_frames']} 帧 · OCR 命中 {ctx['n_ocr']}（{ctx['ocr_engine']}）",
        ]
    out = ["\n".join(lines), ""]
    out += info
    out.append("")
    if toc:
        out.append("> [!tip]- 章节导航")
        for t, name in toc:
            out.append(f"> - {f'[{t}] ' if t else ''}{name}")
        out.append("")
    out.append("---")
    out.append("")
    out.append(body.strip())
    return "\n".join(out) + "\n"



_COLL_SPLIT_RE = re.compile(r"[：:｜|]")



def _collection_folder_name(playlist_title: str) -> str:
    """合集文件夹名。

    合集标题常带 SEO/营销尾巴（「主名：一长串卖点」「⚠️主名｜副标题」）——
    先按「：:｜|」取主名（主名够长时），再剥掉开头的 emoji/符号，最后截断到 40 字。
    完整合集名始终保留在每篇笔记的 frontmatter `playlist` 字段里（可追溯）。
    """
    if not (playlist_title or "").strip():
        return ""  # 单视频：不留目录（注意 sanitize_filename("") 会兜底成「未命名」）
    t = sanitize_filename(playlist_title.strip())
    head = _COLL_SPLIT_RE.split(t, maxsplit=1)[0].strip()
    name = head if 8 <= len(head) < len(t) else t
    name = re.sub(r"^[^\w\u4e00-\u9fff]+", "", name).strip()  # 去开头 emoji/符号
    return (name or t)[:40]


def collection_dir(playlist_title: str) -> Path | None:
    """合集/播放列表的笔记目录：`资料库/视频笔记/<合集名>/`。

    单视频（无 playlist_title）返回 None → 笔记仍放在视频笔记根目录。
    只挪笔记：transcripts/ 与 attachments/ 保持全库共享池——Obsidian 的
    `[[transcripts/x]]` / `![[attachments/y.jpg]]` 按文件名解析，笔记进子目录不影响链接。
    """
    name = _collection_folder_name(playlist_title)
    return (OUT_DIR / name) if name else None



def write_note(md: str, title: str, playlist_title: str = "") -> Path:
    folder = collection_dir(playlist_title) or OUT_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sanitize_filename(title)}.md"
    i = 2
    while path.exists():  # 重名只在同一目录内避让（不同合集可以有同名笔记）
        path = folder / f"{sanitize_filename(title)}-{i}.md"
        i += 1
    path.write_text(md)
    return path



def append_index(title: str, dur_str: str, summary: str, playlist_title: str = "") -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not INDEX_FILE.exists():
        INDEX_FILE.write_text(
            "---\ntags:\n  - 索引\n  - 视频笔记\n---\n\n# 视频笔记索引\n\n"
            "| 笔记 | 时长 | 摘要 | 日期 |\n| --- | --- | --- | --- |\n")
    parts = ([_collection_folder_name(playlist_title), sanitize_filename(title)]
             if playlist_title else [sanitize_filename(title)])
    link = "/".join(p for p in parts if p)  # 合集笔记带目录前缀，Obsidian 按路径解析
    row = f"| [[{link}]] | {dur_str} | {(summary or '—')[:60]} | {date.today().isoformat()} |\n"
    with INDEX_FILE.open("a") as f:
        f.write(row)

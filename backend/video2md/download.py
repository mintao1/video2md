"""条目下载（yt-dlp；抖音直链兜底；图文帖图片抓取）。"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import httpx

from .resolve import _DOUYIN_UA, _clean_ytdlp_err, _download_direct, _subprocess_env
from .util import redact, sanitize_filename


def download_entry(entry: dict, tmp: Path) -> tuple[Path, str]:
    """下载一个已解析的条目；输出名带 entry id/分P序号，避免多条目附件碰撞。"""
    ytdlp = shutil.which("yt-dlp") or next(
        (str(p) for p in (Path("/Library/Frameworks/Python.framework/Versions/3.14/bin/yt-dlp"),
                          Path.home() / "Library/Frameworks/Python.framework/Versions/3.14/bin/yt-dlp",
                          Path("/opt/homebrew/bin/yt-dlp")) if p.exists()), "")
    if not ytdlp:
        sys.exit("❌ 未找到 yt-dlp：pip install yt-dlp")
    eid = sanitize_filename(entry.get("id") or "video")[:40]
    idx = entry.get("playlist_index")
    prefix = f"p{idx:03d}-{eid}" if isinstance(idx, int) else eid
    if entry.get("direct_url"):
        # 抖音直链兜底：yt-dlp 必然被 Fresh cookies 挡，直接走 play 直链
        try:
            return _download_direct(entry["direct_url"], tmp / f"{prefix}.mp4"), entry.get("uploader", "")
        except Exception as exc:
            sys.exit(f"❌ 抖音直链下载失败: {redact(str(exc)[-200:])}")
    common = ["-f", "bv*[height<=720]+ba/b[height<=720]", "--merge-output-format", "mp4",
              "--concurrent-fragments", "8", "-o", str(tmp / f"{prefix}.%(ext)s"),
              "--no-playlist", "--no-simulate", "--print", "META|%(uploader)s|%(channel)s"]
    url = entry["url"]
    p = subprocess.run([ytdlp, *common, url], capture_output=True, text=True,
                       env=_subprocess_env(url))
    if p.returncode != 0:
        p = subprocess.run([ytdlp, "--cookies-from-browser", "chrome", *common, url],
                           capture_output=True, text=True, env=_subprocess_env(url))
    if p.returncode != 0:
        err = _clean_ytdlp_err(p.stderr)  # 剥 requests/urllib3 版本警告，保住真正的 ERROR 行
        if "Fresh cookies" in err:
            sys.exit("❌ 抖音需要浏览器新鲜 cookies（请先在 Chrome 登录后重试）")
        if "No video formats" in err:
            sys.exit("❌ 小红书无可解析视频（可能是图文笔记或触发反爬；请提供 Chrome cookies）")
        sys.exit(f"❌ 视频下载失败: {redact(err[-300:])}")
    meta = [ln.split("|") for ln in p.stdout.splitlines() if ln.startswith("META|")]
    uploader = (meta[-1][1].strip() if meta and len(meta[-1]) > 1 else "") or \
               (meta[-1][2].strip() if meta and len(meta[-1]) > 2 else "") or entry.get("uploader", "")
    files = sorted(tmp.glob(f"{prefix}.*"), key=lambda f: f.stat().st_size)
    if not files:
        sys.exit("❌ 下载完成但未找到视频文件")
    return files[-1], uploader



def download_images(entry: dict, tmp: Path) -> list[Path]:
    """图文帖：直下图集（douyinpic CDN，Referer 防盗链）；幂等按文件存在跳过。"""
    img_dir = tmp / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for i, iu in enumerate(entry.get("images") or [], 1):
        dst = img_dir / f"img_{i:03d}.jpg"
        if not dst.exists() or dst.stat().st_size < 1024:
            try:
                with httpx.stream("GET", iu, follow_redirects=True, timeout=30,
                                  headers={"User-Agent": _DOUYIN_UA,
                                           "Referer": "https://www.douyin.com/"}) as r:
                    r.raise_for_status()
                    with open(dst, "wb") as f:
                        for chunk in r.iter_bytes(1 << 18):
                            f.write(chunk)
            except Exception as exc:
                raise SystemExit(f"图文下载失败 img_{i}: {redact(str(exc)[-120:])}")
            if not dst.exists() or dst.stat().st_size < 1024:
                raise SystemExit(f"图文 img_{i} 下载内容过小，疑似风控页")
        out.append(dst)
    if not out:
        raise SystemExit("图文帖无图片地址（枚举响应未带 images）")
    return out

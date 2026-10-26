"""Public multi-part videos when the Bilibili webpage is unavailable.

Metadata and playback URLs come from Bilibili's public API. Only anonymously
available progressive MP4 is accepted; restricted/removed videos remain errors.
Requests use direct TLS because the production general proxy breaks this source.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import overload
from urllib.parse import parse_qs, urlsplit

import httpx

_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}


def accepts_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname in {"www.bilibili.com", "bilibili.com"}
            and re.fullmatch(r"/video/BV[0-9A-Za-z]{10}/?", parsed.path) is not None)


@overload
def _api(client: httpx.Client, path: str, params: dict, *, data_type: type[dict] = dict) -> dict: ...


@overload
def _api(client: httpx.Client, path: str, params: dict, *, data_type: type[list]) -> list: ...


def _api(client: httpx.Client, path: str, params: dict, *, data_type: type[dict] | type[list] = dict) -> dict | list:
    try:
        response = client.get(f"https://api.bilibili.com/x/{path}", params=params)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"B站公开接口请求失败（HTTP {exc.response.status_code}）") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(f"B站公开接口请求失败（{type(exc).__name__}）") from exc
    if not isinstance(payload, dict) or payload.get("code") != 0:
        code = payload.get("code") if isinstance(payload, dict) else "invalid"
        raise RuntimeError(f"B站公开接口返回错误（{code}），视频可能不可用或需要登录权限")
    data = payload.get("data")
    if not isinstance(data, data_type):
        raise RuntimeError("B站公开接口未返回视频信息")
    return data


def resolve_public_video(url: str) -> list[dict]:
    if not accepts_url(url):
        raise ValueError("不是支持的 B站公开视频链接")
    parsed = urlsplit(url)
    bvid = parsed.path.strip("/").split("/")[-1]
    with httpx.Client(trust_env=False, timeout=15, headers=_HEADERS) as client:
        try:
            data = _api(client, "web-interface/view", {"bvid": bvid})
        except RuntimeError as exc:
            cause = exc.__cause__
            if not isinstance(cause, httpx.HTTPStatusError) or cause.response.status_code != 412:
                raise
            # The public anthology endpoint remains available when the richer
            # view endpoint rejects the server. Do not invent uploader metadata.
            pages = _api(client, "player/pagelist", {"bvid": bvid}, data_type=list)
            data = {"pages": pages, "title": f"B站合集 {bvid}"}
    pages = data.get("pages") or []
    if not pages:
        raise RuntimeError("B站公开接口未返回分集信息")
    part = parse_qs(parsed.query, keep_blank_values=True).get("p")
    if part:
        if len(part) != 1 or not part[0].isdigit() or int(part[0]) < 1:
            raise RuntimeError("B站分P参数必须是正整数")
        pages = [p for p in pages if p.get("page") == int(part[0])]
        if not pages:
            raise RuntimeError("B站指定分P不存在")
    title = data.get("title") or bvid
    uploader = (data.get("owner") or {}).get("name") or ""
    anthology = len(data["pages"]) > 1
    entries = []
    for page in pages:
        index, cid = page.get("page"), page.get("cid")
        if not isinstance(index, int) or index < 1 or not isinstance(cid, int) or cid < 1:
            raise RuntimeError("B站公开接口返回无效分集信息")
        entries.append({
            "url": f"https://www.bilibili.com/video/{bvid}/" + (f"?p={index}" if anthology or part else ""),
            "id": f"{bvid}_p{index}" if anthology else bvid,
            "title": page.get("part") or title,
            "playlist_index": index if anthology or part else None,
            "playlist_title": title if anthology else "",
            "playlist_id": bvid if anthology else "",
            "uploader": uploader,
            "duration": page.get("duration"),
            "bilibili": {"bvid": bvid, "cid": cid},
        })
    return entries


def download_public_video(entry: dict, destination: Path) -> Path:
    """Fetch a fresh signed media URL at download time, without persisting it."""
    identity = entry["bilibili"]
    with httpx.Client(trust_env=False, timeout=httpx.Timeout(60, connect=10),
                      headers={**_HEADERS, "Referer": entry["url"]}) as client:
        data = _api(client, "player/playurl", {
            "bvid": identity["bvid"], "cid": identity["cid"], "qn": 64,
            "fnval": 1, "platform": "html5", "high_quality": 1,
        })
        streams = data.get("durl") or []
        if data.get("format") != "mp4" or len(streams) != 1:
            raise RuntimeError("B站未提供可匿名下载的完整 MP4，请使用可公开播放的视频")
        stream = streams[0]
        media_url = stream.get("url") or ""
        parsed = urlsplit(media_url)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443)
                or not any(host == d or host.endswith("." + d) for d in ("bilivideo.com", "bilivideo.cn"))):
            raise RuntimeError("B站返回了不受支持的媒体地址")
        partial = destination.with_suffix(".part")
        try:
            with client.stream("GET", media_url) as response:
                response.raise_for_status()
                with partial.open("wb") as output:
                    for chunk in response.iter_bytes(1 << 18):
                        output.write(chunk)
            expected = stream.get("size")
            if partial.stat().st_size < 1024 or (expected and partial.stat().st_size != expected):
                raise RuntimeError("B站视频下载不完整")
            with partial.open("rb") as output:
                if b"ftyp" not in output.read(32):
                    raise RuntimeError("B站视频响应不是有效 MP4")
            partial.replace(destination)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"B站媒体下载失败（{type(exc).__name__}）") from exc
        finally:
            partial.unlink(missing_ok=True)
    return destination

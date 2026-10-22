"""Replay the production webpage failure at the resolver/download boundaries."""
import subprocess

import httpx
import pytest

URL = "https://www.bilibili.com/video/BV1vV411k7GB/"
MEDIA = "https://upos-sz-mirrorcos.bilivideo.com/video.mp4"
VIEW = {"code": 0, "data": {
    "bvid": "BV1vV411k7GB", "title": "PPT 教程", "owner": {"name": "讲师"},
    "pages": [{"cid": 101, "page": 1, "part": "第一讲", "duration": 79},
              {"cid": 102, "page": 2, "part": "第二讲", "duration": 79}],
}}


@pytest.fixture(autouse=True)
def isolated_modules(monkeypatch, tmp_path):
    # Import after collection: the API contract suite configures storage before
    # importing the application. Eager imports here would capture real paths.
    for key, name in {"V2MD_RUNS_FILE": "runs.json", "V2MD_STATE_FILE": "state.json",
                      "V2MD_USERS_FILE": "users.json", "V2MD_PREFS_FILE": "prefs.json",
                      "V2MD_SECRETS_FILE": "missing.env", "V2MD_LOG_DIR": "logs",
                      "V2MD_UPLOADS_DIR": "uploads", "OBSIDIAN_VAULT": "vault"}.items():
        monkeypatch.setenv(key, str(tmp_path / name))
    monkeypatch.setenv("V2MD_AUTH", "off")
    monkeypatch.setenv("V2MD_FAKE_LLM", "1")
    global download, resolve
    from video2md import download, resolve


@pytest.fixture
def api_transport(monkeypatch):
    calls = []
    responses = {"view": VIEW, "playurl": {"code": 0, "data": {
        "format": "mp4", "quality": 16, "durl": [{"url": MEDIA, "size": 1036}],
    }}, "pagelist": {"code": 0, "data": VIEW["data"]["pages"]}}

    def handler(request):
        calls.append(request)
        if request.url.host == "api.bilibili.com":
            response = responses[request.url.path.rsplit("/", 1)[-1]]
            return httpx.Response(412) if response == "HTTP_412" else httpx.Response(200, json=response)
        return httpx.Response(200, content=b"\x00\x00\x00\x18ftypisom" + b"x" * 1024,
                              headers={"content-type": "video/mp4"})

    client = httpx.Client

    def factory(**kwargs):
        assert kwargs.get("trust_env") is False, "public API must not inherit the broken proxy"
        return client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    return calls, responses


def fail_webpage(monkeypatch, *, timeout=False):
    attempts = []

    def run(*args, **kwargs):
        attempts.append(args[0])
        if timeout:
            raise subprocess.TimeoutExpired(args[0], 60)
        return subprocess.CompletedProcess(args[0], 1, "", "ERROR: [BiliBili] SSL: UNEXPECTED_EOF_WHILE_READING")

    monkeypatch.setattr(resolve.subprocess, "run", run)
    monkeypatch.setattr(resolve.time, "sleep", lambda _: pytest.fail("do not retry the broken webpage"))
    return attempts


@pytest.mark.parametrize("count", [8, 100])
@pytest.mark.parametrize("view_412", [False, True])
def test_proxy_outage_preserves_every_part_in_both_collection_sizes(monkeypatch, api_transport, count, view_412):
    calls, responses = api_transport
    monkeypatch.setenv("V2MD_PROXY", "http://127.0.0.1:1")
    responses["view"] = {"code": 0, "data": {"title": "collection", "pages": [
        {"page": i, "cid": 1000 + i, "part": f"P{i}", "duration": 78}
        for i in range(1, count + 1)]}}
    if view_412:
        responses["pagelist"] = {"code": 0, "data": responses["view"]["data"]["pages"]}
        responses["view"] = "HTTP_412"
    fail_webpage(monkeypatch)
    entries = resolve.resolve_url(URL)
    assert len(entries) == count
    assert [e["playlist_index"] for e in entries] == list(range(1, count + 1))
    assert [e["bilibili"]["cid"] for e in entries] == list(range(1001, 1001 + count))
    assert all(e["url"].endswith(f"?p={i}") for i, e in enumerate(entries, 1))
    assert calls and all(c.url.host == "api.bilibili.com" for c in calls)


@pytest.mark.parametrize("timeout", [False, True])
def test_failed_webpage_expands_public_collection_without_retries(monkeypatch, api_transport, timeout):
    attempts = fail_webpage(monkeypatch, timeout=timeout)
    entries = resolve.resolve_url(URL)
    assert len(attempts) == 1
    assert [e["playlist_index"] for e in entries] == [1, 2]
    assert [e["bilibili"]["cid"] for e in entries] == [101, 102]
    assert entries[0]["url"] == URL + "?p=1"
    assert entries[0]["playlist_title"] == "PPT 教程"
    assert entries[0]["uploader"] == "讲师"
    assert "direct_url" not in entries[0], "expiring media URLs are resolved only when downloading"


def test_explicit_part_preserves_identity(monkeypatch, api_transport):
    fail_webpage(monkeypatch)
    entries = resolve.resolve_url(URL + "?p=2")
    assert len(entries) == 1
    assert entries[0]["bilibili"]["cid"] == 102
    assert entries[0]["playlist_index"] == 2


def test_single_video_keeps_unindexed_filter_contract(monkeypatch, api_transport):
    fail_webpage(monkeypatch)
    api_transport[1]["view"] = {"code": 0, "data": {**VIEW["data"], "pages": VIEW["data"]["pages"][:1]}}
    entries = resolve.resolve_url(URL)
    assert entries[0]["playlist_index"] is None
    assert entries[0]["url"] == URL


@pytest.mark.parametrize("part", ["0", "3", "invalid"])
def test_invalid_or_missing_part_does_not_download_another_episode(monkeypatch, api_transport, part):
    fail_webpage(monkeypatch)
    with pytest.raises(RuntimeError, match="分P"):
        resolve.resolve_url(URL + "?p=" + part)


def test_private_or_removed_video_is_not_treated_as_public(monkeypatch, api_transport):
    fail_webpage(monkeypatch)
    api_transport[1]["view"] = {"code": -404, "message": "视频不存在"}
    with pytest.raises(RuntimeError, match="B站.*-404"):
        resolve.resolve_url(URL)


def test_public_pagelist_handles_http_412_without_losing_parts(monkeypatch, api_transport):
    fail_webpage(monkeypatch)
    api_transport[1]["view"] = "HTTP_412"
    entries = resolve.resolve_url(URL)
    assert [e["bilibili"]["cid"] for e in entries] == [101, 102]
    assert entries[0]["uploader"] == ""
    assert entries[0]["playlist_title"] == "B站合集 BV1vV411k7GB"


def test_download_fetches_fresh_public_media_and_does_not_use_webpage(monkeypatch, api_transport, tmp_path):
    monkeypatch.setenv("HTTPS_PROXY", "http://broken-proxy.invalid:17898")
    monkeypatch.setattr(download.subprocess, "run", lambda *a, **kw: pytest.fail("no webpage or browser cookie access"))
    entry = {"url": URL + "?p=2", "id": "BV1vV411k7GB_p2", "playlist_index": 2,
             "uploader": "讲师", "bilibili": {"bvid": "BV1vV411k7GB", "cid": 102}}
    path, uploader = download.download_entry(entry, tmp_path)
    assert path.read_bytes().startswith(b"\x00\x00\x00\x18ftyp")
    assert uploader == "讲师"
    requests = api_transport[0]
    assert requests[0].url.params["cid"] == "102"
    assert requests[-1].headers["referer"] == entry["url"]


@pytest.mark.parametrize("timeout", [False, True])
def test_webpage_download_failure_uses_correct_part_without_browser_cookies(monkeypatch, api_transport, tmp_path, timeout):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] == 60
        if timeout:
            raise subprocess.TimeoutExpired(command, 60)
        return subprocess.CompletedProcess(command, 1, "", "ERROR: playback unavailable from webpage")

    monkeypatch.setattr(download.subprocess, "run", run)
    entry = {"url": URL + "?p=2", "id": "part2", "playlist_index": 2}
    path, _ = download.download_entry(entry, tmp_path)
    assert path.is_file()
    assert len(calls) == 1
    assert "--cookies-from-browser" not in calls[0]
    play = next(r for r in api_transport[0] if r.url.path.endswith('/playurl'))
    assert play.url.params["cid"] == "102"


@pytest.mark.parametrize("media_url", ["http://127.0.0.1/private", "https://bilivideo.com.evil.example/a.mp4"])
def test_playback_response_cannot_redirect_download_to_arbitrary_host(api_transport, tmp_path, media_url):
    api_transport[1]["playurl"]["data"]["durl"][0]["url"] = media_url
    entry = {"url": URL, "id": "video", "bilibili": {"bvid": "BV1vV411k7GB", "cid": 101}}
    with pytest.raises(RuntimeError, match="媒体地址"):
        download.download_entry(entry, tmp_path)
    assert not list(tmp_path.iterdir())


def test_missing_media_fails_instead_of_creating_fake_output(api_transport, tmp_path):
    api_transport[1]["playurl"] = {"code": -403, "message": "无权限"}
    entry = {"url": URL, "id": "video", "bilibili": {"bvid": "BV1vV411k7GB", "cid": 101}}
    with pytest.raises(RuntimeError, match="B站.*-403"):
        download.download_entry(entry, tmp_path)
    assert not list(tmp_path.iterdir())


def test_truncated_media_is_removed(api_transport, tmp_path):
    api_transport[1]["playurl"]["data"]["durl"][0]["size"] = 9000
    entry = {"url": URL, "id": "video", "bilibili": {"bvid": "BV1vV411k7GB", "cid": 101}}
    with pytest.raises(RuntimeError, match="不完整"):
        download.download_entry(entry, tmp_path)
    assert not list(tmp_path.iterdir())

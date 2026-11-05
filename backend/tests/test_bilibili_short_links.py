"""Regression coverage for B站 b23.tv share links."""
from __future__ import annotations

import json
import subprocess

from video2md import resolve


def test_b23_short_link_is_expanded_before_ytdlp(monkeypatch):
    seen: list[str] = []

    class Response:
        url = "https://www.bilibili.com/video/BV13JnG6rE3n/?p=1&vd_source=share"

    monkeypatch.setattr(resolve.httpx, "get", lambda *args, **kwargs: Response())

    def run(command, **kwargs):
        seen.append(command[-1])
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({
                "id": "BV13JnG6rE3n_p1",
                "webpage_url": Response.url,
                "title": "颠覆口播创作流程",
                "playlist_index": 1,
            }),
            "",
        )

    monkeypatch.setattr(resolve.subprocess, "run", run)
    entries = resolve.resolve_url("https://b23.tv/pZaxaHP")

    assert seen == [Response.url]
    assert entries[0]["id"] == "BV13JnG6rE3n_p1"


def test_b23_expansion_failure_preserves_original_url(monkeypatch):
    monkeypatch.setattr(resolve.httpx, "get", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))
    original = "https://b23.tv/pZaxaHP"
    assert resolve.expand_short_link(original) == original


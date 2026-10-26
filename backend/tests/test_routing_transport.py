"""真实 llm.chat 适配器边界的无网络路由评测。"""
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _sse(text="ok"):
    return ("data: " + json.dumps({"choices": [{"delta": {"content": text}}]})
            + "\n\ndata: [DONE]\n").encode()


def test_chat_routes_model_and_task_params(monkeypatch):
    from video2pdf import llm

    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse("ok"))

    monkeypatch.setattr(llm, "_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(llm, "LLM_BASE", "https://mock.local/v1")
    monkeypatch.setattr(llm, "LLM_KEY", "test-key")
    out = llm.chat("system", "user", task="metadata", temperature=.1, max_tokens=2000,
                   model="route-model", attempts=1)
    assert out == "ok"
    assert seen[0]["model"] == "route-model"
    assert seen[0]["temperature"] == .1 and seen[0]["max_tokens"] == 2000
    assert seen[0]["stream"] is True


def test_chat_retries_empty_stream_then_succeeds(monkeypatch):
    from video2pdf import llm

    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=b"data: [DONE]\n")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse("retry-ok"))

    monkeypatch.setattr(llm, "_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(llm, "LLM_BASE", "https://mock.local/v1")
    monkeypatch.setattr(llm, "LLM_KEY", "test-key")
    monkeypatch.setattr(llm, "_sleep", lambda _: None)
    assert llm.chat("s", "u", task="organize", temperature=.4, max_tokens=16000,
                    model="route-model", attempts=2) == "retry-ok"
    assert len(calls) == 2


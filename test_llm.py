#!/usr/bin/env python3
"""test_llm.py — llm.py 离线单测：模板渲染 / chat 重试纪律 / 选帧解析 / 写盘组装。

运行：python3 test_llm.py（无网络：httpx MockTransport 拦截；重试 sleep 打桩）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import httpx

from video2md import llm

llm._sleep = lambda _s: None  # 重试间隔不真等


# ---- render：{{var}} 替换纪律 ------------------------------------------------
assert llm.render("a {{x}} b", x="1") == "a 1 b"
assert llm.render("{{ x }}", x="1") == "1"                       # 容忍空格
assert llm.render("{{missing}} 保留", x="1") == "{{missing}} 保留"  # 未知占位符原样保留
meta_sys = llm._load("metadata", "system")
assert '{"title"' in meta_sys                                    # JSON 示例的字面花括号不被当占位符
assert llm.render(meta_sys) == meta_sys                          # 无占位符模板渲染后不变


# ---- chat：请求形状 + 重试纪律 ------------------------------------------------
def run_chat(handler, **kw):
    llm._TRANSPORT = httpx.MockTransport(handler)
    llm.LLM_BASE, llm.LLM_KEY = "https://llm.test/v1", "sk-test"
    try:
        return llm.chat("sys", "usr", task="metadata", temperature=0.1, max_tokens=100, **kw)
    finally:
        llm._TRANSPORT = None

calls = []

def ok_handler(req: httpx.Request) -> httpx.Response:
    calls.append(req)
    return httpx.Response(200, json={"choices": [{"message": {"content": "  答案  "}}]})

out = run_chat(ok_handler)
assert out == "  答案  ", out
req = calls[0]
assert req.url == "https://llm.test/v1/chat/completions"
assert req.headers["Authorization"] == "Bearer sk-test"
body = json.loads(req.content)
assert body["model"] == llm.LLM_MODEL and body["temperature"] == 0.1 and body["max_tokens"] == 100
assert [m["role"] for m in body["messages"]] == ["system", "user"]

# 500 → 重试成功（共 2 次调用）
seq = iter([httpx.Response(500, text="boom"),
            httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})])
calls.clear()
llm._TRANSPORT = httpx.MockTransport(lambda _r: next(seq))
llm.LLM_BASE, llm.LLM_KEY = "https://llm.test/v1", "sk-test"
assert llm.chat("s", "u", task="t", temperature=0, max_tokens=1) == "ok"
llm._TRANSPORT = None

# 429 三次全灭 → RuntimeError
calls.clear()
llm._TRANSPORT = httpx.MockTransport(lambda _r: httpx.Response(429, text="rate"))
llm.LLM_BASE, llm.LLM_KEY = "https://llm.test/v1", "sk-test"
try:
    llm.chat("s", "u", task="t", temperature=0, max_tokens=1)
    raise AssertionError("应当抛 RuntimeError")
except RuntimeError as e:
    assert "连续 3 次" in str(e)
llm._TRANSPORT = None

# 401 不重试（鉴权错重试无意义）
n = [0]
def auth_handler(_r):
    n[0] += 1
    return httpx.Response(401, text="bad key")
try:
    run_chat(auth_handler)
    raise AssertionError("应当抛 RuntimeError")
except RuntimeError as e:
    assert "401" in str(e)
assert n[0] == 1, f"4xx 不该重试，实际调了 {n[0]} 次"


# ---- SSE 流式收集（524 规避的主路径）-----------------------------------------
def sse_handler(_r):
    calls.append(_r)
    body = ('data: {"choices":[{"delta":{"content":"第一段"}}]}\n\n'
            'data: {"choices":[{"delta":{}}]}\n\n'          # 空 delta 心跳
            'data: {"choices":[{"delta":{"content":"第二段"}}]}\n\n'
            'data: [DONE]\n\n')
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

calls.clear()
out = run_chat(sse_handler)
assert out == "第一段第二段", out
assert json.loads(calls[0].content)["stream"] is True  # 必须强制流式（长生成防中转 524）

# 中转不支持 stream 时回普通 JSON → 兼容解析
def json_handler(_r):
    return httpx.Response(200, json={"choices": [{"message": {"content": "非流式"}}]})
assert run_chat(json_handler) == "非流式"


# ---- 选帧解析（移植自 DSL code 节点）-----------------------------------------
manifest = ("===== 条目 1/2：https://a =====\nf1.jpg [00:01] OCR: 标题\nf2.jpg [00:02] OCR: —\n\n"
            "===== 条目 2/2：https://b =====\ng1.jpg [00:03] OCR: 图\n")
allowed = llm.allowed_frames_of(manifest)
assert allowed == {1: ["f1.jpg", "f2.jpg"], 2: ["g1.jpg"]}, allowed
assert llm.allowed_frames_of("x.jpg [00:01] OCR: t") == {1: ["x.jpg"]}

blocks, mode = llm.parse_select_output('```json\n{"frames": [{"name": "f1.jpg", "caption": "图"}]}\n```')
assert mode == "frames" and blocks[0]["frames"] == [{"name": "f1.jpg", "caption": "图"}]
blocks, mode = llm.parse_select_output('{"selections": [{"entry": 2, "frames": [{"name": "g1.jpg"}]}]}')
assert mode == "selections" and blocks[0]["entry"] == 2
assert llm.parse_select_output("不是 JSON")[1] == "frames"  # 垃圾输入不炸，空 frames 兜底

# 滤非法名 + 去重；多条目 selections 按块过滤
payload, n, note = llm.filter_select(
    [{"entry": 1, "frames": [{"name": "f1.jpg"}, {"name": "编造.jpg"}, {"name": "f1.jpg"}]}],
    "frames", {1: ["f1.jpg", "f2.jpg"]}, 1)
assert n == 1 and payload == {"frames": [{"name": "f1.jpg", "caption": ""}]}, payload
payload, n, note = llm.filter_select(
    [{"entry": 1, "frames": [{"name": "f1.jpg"}]}, {"entry": 2, "frames": [{"name": "g1.jpg"}]}],
    "selections", {1: ["f1.jpg"], 2: ["g1.jpg"]}, 2)
assert n == 2 and payload["selections"][1]["entry"] == 2
payload, n, note = llm.filter_select([{"entry": 0, "frames": [{"name": "编.jpg"}]}],
                                     "frames", {1: ["f1.jpg"]}, 1)
assert n == 0 and "兜底" in note and payload == {"frames": []}


# ---- select_frames 端到端（MockTransport）------------------------------------
def sel_handler(_r):
    return httpx.Response(200, json={"choices": [{"message": {"content":
        '{"frames": [{"name": "f1.jpg", "caption": "封面"}, {"name": "bad.jpg"}]}'}}]})
llm._TRANSPORT = httpx.MockTransport(sel_handler)
llm.LLM_BASE, llm.LLM_KEY = "https://llm.test/v1", "sk-test"
payload, n, _note = llm.select_frames("转录本", "f1.jpg [00:01] OCR: 封面", 1)
assert n == 1 and payload["frames"][0]["caption"] == "封面", payload
llm._TRANSPORT = None


# ---- assemble_entry_note（CODE_ENTRY_NOTE 移植）-------------------------------
r = llm.assemble_entry_note("原稿" * 30, "校对稿" * 20, '{"title": "题", "tags": ["a", "b"], "summary": "摘"}')
assert r["body"].startswith("校对稿") and r["meta"]["title"] == "题"
r = llm.assemble_entry_note("原稿" * 30, "太短", "```json\n{\"title\": \"题\"}\n```")
assert r["body"].startswith("原稿") and r["meta"]["title"] == "题"  # 围栏容忍 + 校对稿 <50 字回原稿
r = llm.assemble_entry_note("原稿" * 30, "", "垃圾")
assert r["meta"] == {"title": "", "tags": [], "summary": ""} or r["meta"] == {}
r = llm.assemble_entry_note("", "", "{}")
assert r["body"] == ""

print("✅ test_llm.py 全部通过")

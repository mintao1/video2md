#!/usr/bin/env python3
"""test_llm.py — llm.py 离线单测：模板渲染 / chat 重试纪律 / 选帧解析 / 写盘组装。

运行：pytest tests/test_llm.py（无网络：httpx MockTransport 拦截；重试 sleep 打桩）。
"""
import json

import httpx


def test_llm(monkeypatch):
    # Import after collection so the API suite can install isolated paths and
    # credentials first. The SDK is a declared dependency; a global fake breaks
    # API startup when this file is collected before the contract suite.
    from video2md import llm

    for name in ('_TRANSPORT', 'LLM_BASE', 'LLM_KEY', '_FAKE_LLM'):
        monkeypatch.setattr(llm, name, getattr(llm, name))
    monkeypatch.setattr(llm, '_sleep', lambda _: None)
    # Other contract tests intentionally set V2MD_FAKE_LLM=1 for safety. This
    # test exercises the real HTTP/mock transport path, so isolate that module
    # switch from process-wide test ordering.
    llm._FAKE_LLM = False
    
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
    
    # model 参数覆盖：judge 单独换模型（V2MD_JUDGE_MODEL 由 evals 传入），缺省仍 LLM_MODEL
    calls.clear()
    run_chat(ok_handler, model="Fast-Judge")
    assert json.loads(calls[0].content)["model"] == "Fast-Judge"
    calls.clear()
    run_chat(ok_handler)
    assert json.loads(calls[0].content)["model"] == llm.LLM_MODEL
    
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
    
    
    # ---- usage 捕获（LLM 成本可见性的源头）---------------------------------------
    def sse_usage_handler(_r):
        calls.append(_r)
        body = ('data: {"choices":[{"delta":{"content":"甲"}}]}\n\n'
                # include_usage 尾 chunk：choices 为空、只有 usage——不许炸解析也不许丢
                'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":7,'
                '"total_tokens":18}}\n\n'
                'data: [DONE]\n\n')
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
    
    calls.clear()
    sp = {}
    out = run_chat(sse_usage_handler, span_rec=sp)
    assert out == "甲"
    assert sp["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}, sp
    assert sp["model"] == llm.LLM_MODEL and sp["attempts"] == 1
    assert sp["prompt_chars"] == len("sys") + len("usr") and sp["completion_chars"] == 1
    assert sp["ttftMs"] >= 0
    assert json.loads(calls[-1].content)["stream_options"] == {"include_usage": True}
    
    # 非流式回包带 usage → 也要捕获
    def json_usage_handler(_r):
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}],
                                         "usage": {"prompt_tokens": 3, "completion_tokens": 1,
                                                   "total_tokens": 4}})
    sp2 = {}
    assert run_chat(json_usage_handler, span_rec=sp2) == "x"
    assert sp2["usage"]["total_tokens"] == 4, sp2
    
    # provider 不回 usage：只有字符数，不编造 usage
    calls.clear()
    sp3 = {}
    assert run_chat(sse_handler, span_rec=sp3) == "第一段第二段"
    assert "usage" not in sp3 and sp3["completion_chars"] == len("第一段第二段")
    
    # 中转 400 拒识 stream_options → 摘掉重建 payload 重试（不占 30s 退避）
    seq2 = iter([httpx.Response(400, text="Bad request: unknown field stream_options"),
                 httpx.Response(200, json={"choices": [{"message": {"content": "去选项 ok"}}]})])
    n2 = [0]
    def picky_handler(_r):
        n2[0] += 1
        return next(seq2)
    llm._TRANSPORT = httpx.MockTransport(picky_handler)
    llm.LLM_BASE, llm.LLM_KEY = "https://llm.test/v1", "sk-test"
    sp4 = {}
    assert llm.chat("s", "u", task="t", temperature=0, max_tokens=1, span_rec=sp4) == "去选项 ok"
    assert n2[0] == 2 and sp4["attempts"] == 2
    llm._TRANSPORT = None

    # mimo 式 5xx 甩锅：流式带 stream_options 回 500 "Upstream Response Error"
    # （2026-10-13 实测，同请求摘掉即 200）→ 5xx 也摘一次重试
    seq3 = iter([httpx.Response(500, text="Upstream Response Error"),
                 httpx.Response(200, json={"choices": [{"message": {"content": "mimo ok"}}]})])
    n3, bodies3 = [0], []
    def mimo_handler(r):
        n3[0] += 1
        bodies3.append(json.loads(r.content))
        return next(seq3)
    llm._TRANSPORT = httpx.MockTransport(mimo_handler)
    llm.LLM_BASE, llm.LLM_KEY = "https://llm.test/v1", "sk-test"
    sp5 = {}
    assert llm.chat("s", "u", task="t", temperature=0, max_tokens=1, span_rec=sp5) == "mimo ok"
    assert n3[0] == 2 and sp5["attempts"] == 2
    assert "stream_options" in bodies3[0] and "stream_options" not in bodies3[1]
    llm._TRANSPORT = None

    
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
    
    
    # ---- 空回包/畸形回包 = 可重试错误（中转把上游异常包成 200，实测 2026-09-22）------
    _empty_sse = 'data: {"choices": []}\n\ndata: [DONE]\n\n'  # 200 但零内容的流
    _ok_sse = 'data: {"choices":[{"delta":{"content":"活过来了"}}]}\n\ndata: [DONE]\n\n'
    seq = iter([httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_empty_sse),
                httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_empty_sse),
                httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_ok_sse)])
    llm._TRANSPORT = httpx.MockTransport(lambda _r: next(seq))
    llm.LLM_BASE, llm.LLM_KEY = "https://llm.test/v1", "sk-test"
    assert llm.chat("s", "u", task="t", temperature=0, max_tokens=1) == "活过来了"
    llm._TRANSPORT = None
    
    # 三次全空 → RuntimeError（不静默返回空串）
    llm._TRANSPORT = httpx.MockTransport(
        lambda _r: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=_empty_sse))
    llm.LLM_BASE, llm.LLM_KEY = "https://llm.test/v1", "sk-test"
    try:
        llm.chat("s", "u", task="t", temperature=0, max_tokens=1)
        raise AssertionError("应当抛 RuntimeError")
    except RuntimeError as e:
        assert "空回包" in str(e), e
    llm._TRANSPORT = None
    
    # 非流式兼容路径的畸形 JSON 也可重试：先畸形后正常
    seq2 = iter([httpx.Response(200, json={"unexpected": "shape"}),
                 httpx.Response(200, json={"choices": [{"message": {"content": "好了"}}]})])
    llm._TRANSPORT = httpx.MockTransport(lambda _r: next(seq2))
    llm.LLM_BASE, llm.LLM_KEY = "https://llm.test/v1", "sk-test"
    assert llm.chat("s", "u", task="t", temperature=0, max_tokens=1) == "好了"
    llm._TRANSPORT = None
    
    pass

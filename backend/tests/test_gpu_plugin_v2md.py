"""v2md 插件端到端单测（在 video2md 仓跑）：插件加载 / 配置聚合 / server 端点契约 /
provider 能力装载（http provider MockTransport）。不联网、不起真 provider。

跑法：python3 test_gpu_plugin_v2md.py
"""
from __future__ import annotations

import io
import json
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import httpx

V2MD_ROOT = Path(__file__).resolve().parent
GPU_ROUTER_ROOT = (V2MD_ROOT.parent / "gpu-router").resolve()
sys.path.insert(0, str(GPU_ROUTER_ROOT))

from fastapi.testclient import TestClient
from gpu_router.config import load_config
from gpu_router.router import Router
from gpu_router.server import create_app
from gpu_router.state import StateStore

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


_TMP = Path(tempfile.mkdtemp(prefix="v2md-plugin-test-"))


def test_gpu_plugin_v2md():
    print("== v2md 插件加载 ==")
    from gpu_router.plugins import load_plugins
    
    plugs = load_plugins(["v2md"], [V2MD_ROOT / "gpu_plugins"])
    check("v2md 插件加载 + 三能力契约",
          len(plugs) == 1 and plugs[0].capabilities ==
          {"asr": "transcribe", "clips": "cut_clips", "ocr": "ocr"})
    check("PROVIDER_IMPL 声明四 kind（http 能力方法归本插件，内核零业务）",
          set(plugs[0].module.PROVIDER_IMPL) == {"http", "colab-cli", "kaggle", "zerogpu"})
    
    # ================================================================ 配置聚合
    print("== 配置聚合 ==")
    cfg_file = _TMP / "gpu_router.json"
    cfg_file.write_text(json.dumps({
        "listen": "127.0.0.1:9701",
        "plugins": ["v2md"],
        "plugin_paths": [str(V2MD_ROOT / "gpu_plugins")],
        "order": {"asr": ["modal"]},
        "providers": [{"name": "modal", "kind": "http", "url": "https://x.modal.run/"}]}))
    cfg = load_config(cfg_file)
    check("load_config 装载插件", [pl.name for pl in cfg.loaded_plugins] == ["v2md"])
    check("kind 默认能力：http 三件",
          set(cfg.providers[0].capabilities) == {"asr", "clips", "ocr"})
    check("order 缺口补全（clips/ocr）",
          cfg.order["clips"] == ["modal"] and cfg.order["ocr"] == ["modal"])
    
    # ================================================================ provider 能力装载
    print("== provider 能力装载（http MockTransport）==")
    r = Router.from_config(cfg, StateStore(_TMP / "state.json"))
    hp = r.providers["modal"]
    check("http provider 装载了三能力方法",
          all(callable(getattr(hp, m, None)) for m in ("transcribe", "cut_clips", "ocr")))
    
    
    def _tar_bytes():
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            data = b"fake-mp4-bytes"
            info = tarfile.TarInfo("a-c01-0000.mp4")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
            payload = json.dumps({"a-c01-0000.mp4": True}).encode()
            info = tarfile.TarInfo("_result.json")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        return buf.getvalue()
    
    
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/ping":
            return httpx.Response(200, json={"ok": True, "model": "turbo"})
        if path == "/transcribe":
            return httpx.Response(200, json={"segments": [], "model": "turbo"})
        if path == "/clips":
            return httpx.Response(200, content=_tar_bytes())
        if path == "/ocr":
            return httpx.Response(200, json={"results": [{"name": "g_001.jpg",
                                                          "text": "你好", "conf": 0.9}]})
        return httpx.Response(404, text="nope")
    
    
    hp._client = httpx.Client(transport=httpx.MockTransport(handler),
                              base_url="https://x.modal.run")
    (_TMP / "x.m4a").write_bytes(b"audio")
    (_TMP / "g_001.jpg").write_bytes(b"jpg")
    res, who = r.call("asr", "transcribe", _TMP / "x.m4a", "turbo", "zh", _TMP)
    check("router.call(asr) 经插件契约走通", who == "modal" and res["model"] == "turbo")
    res, who = r.call("ocr", "ocr", [_TMP / "g_001.jpg"], _TMP)
    check("router.call(ocr) 走通", who == "modal" and res[0]["text"] == "你好")
    
    # clips 契约（原 gpu_router 内核 http 契约用例迁入）：tar 解包 + _result.json 回填
    specs = [{"out": "a-c01-0000.mp4", "s0": 0.0, "s1": 2.0}]
    results, out_dir = hp.cut_clips(None, "https://cdn/x.mp4", specs, _TMP)
    check("http clips：tar 解包 + _result.json",
          results.get("a-c01-0000.mp4") is True
          and (out_dir / "a-c01-0000.mp4").read_bytes() == b"fake-mp4-bytes")
    specs2 = [{"out": "miss-c01-0000.mp4", "s0": 0.0, "s1": 1.0}]
    results2, _ = hp.cut_clips(None, "https://cdn/x.mp4", specs2, _TMP)
    check("http clips：缺片回填 False", results2.get("miss-c01-0000.mp4") is False)
    bad = httpx.MockTransport(lambda req: httpx.Response(500, text="boom"))
    hp._client = httpx.Client(transport=bad, base_url="https://x.modal.run")
    try:
        hp.transcribe(_TMP / "x.m4a", "turbo", "zh", _TMP)
        check("http 转录 500 → RuntimeError（task 失败换下家）", False)
    except RuntimeError:
        check("http 转录 500 → RuntimeError（task 失败换下家）", True)
    finally:
        hp._client = httpx.Client(transport=httpx.MockTransport(handler),
                                  base_url="https://x.modal.run")
    
    # ================================================================ server 端点契约
    print("== server 端点（插件注册）==")
    client = TestClient(create_app(router=r))
    
    resp = client.post("/transcribe", files={"audio": ("in.m4a", b"audio-bytes", "audio/mp4")},
                       data={"model": "turbo", "language": "zh"})
    check("POST /transcribe 带 _provider",
          resp.status_code == 200 and resp.json().get("_provider") == "modal")
    
    resp = client.post("/clips", data={
        "spec": json.dumps({"clips": [{"out": "a-c01-0000.mp4", "s0": 0, "s1": 2}]}),
        "direct_url": "https://cdn.example.com/x.mp4"})
    ok_tar = False
    if resp.status_code == 200:
        with tarfile.open(fileobj=io.BytesIO(resp.content)) as tf:
            names = tf.getnames()
            ok_tar = "a-c01-0000.mp4" in names and "_result.json" in names
    check("POST /clips 返回 tar（mp4 + _result.json）", ok_tar, f"HTTP {resp.status_code}")
    
    resp = client.post("/clips", data={
        "spec": json.dumps({"clips": [{"out": "../evil.mp4", "s0": 0, "s1": 2}]}),
        "direct_url": "https://cdn.example.com/x.mp4"})
    check("POST /clips 拒绝路径注入名", resp.status_code == 400)
    
    resp = client.post("/ocr", files=[("images", ("g_001.jpg", b"jpg", "image/jpeg"))])
    check("POST /ocr", resp.status_code == 200
          and resp.json()["results"][0]["text"] == "你好"
          and resp.json()["provider"] == "modal")
    
    idx = client.get("/").json()
    check("GET / 服务名片带 v2md 插件与能力",
          idx.get("plugins") == ["v2md"] and set(idx.get("capabilities", {})) == {"asr", "clips", "ocr"})
    
    # ================================================================ 异步端点
    print("== 异步端点（submit → /jobs 轮询 → inline/检索）==")
    check("名片带 async_capabilities", idx.get("async_capabilities") == ["asr", "clips", "ocr"])
    
    resp = client.post("/transcribe/async",
                       files={"audio": ("in.m4a", b"audio-bytes", "audio/mp4")},
                       data={"model": "turbo", "language": "zh"})
    acc = resp.json()
    check("POST /transcribe/async → 202 带 job_id/inline",
          resp.status_code == 202 and acc["result"] == "inline"
          and acc["status_url"] == f"/jobs/{acc['job_id']}")
    snap = r.jobs.wait(acc["job_id"], timeout=5)
    check("inline 结果经 /jobs/{id} 直读（业务原样 + provider 独立字段）",
          snap["status"] == "done" and snap["result"] == {"segments": [], "model": "turbo"}
          and snap["provider"] == "modal")
    
    resp = client.post("/ocr/async", files=[("images", ("g_001.jpg", b"jpg", "image/jpeg"))])
    acc = resp.json()
    snap = r.jobs.wait(acc["job_id"], timeout=5)
    check("POST /ocr/async → done（inline 为业务原样，未经端点名还原）",
          resp.status_code == 202 and snap["status"] == "done"
          and snap["result"][0]["text"] == "你好")
    
    resp = client.post("/clips/async", data={
        "spec": json.dumps({"clips": [{"out": "a-c01-0000.mp4", "s0": 0, "s1": 2}]}),
        "direct_url": "https://cdn.example.com/x.mp4"})
    acc = resp.json()
    check("POST /clips/async → 202 带 result_url",
          resp.status_code == 202 and acc["result"] == "retrieval"
          and acc["result_url"] == f"/jobs/{acc['job_id']}/result")
    r.jobs.wait(acc["job_id"], timeout=5)
    resp = client.get(f"/jobs/{acc['job_id']}/result")
    ok_tar = False
    if resp.status_code == 200:
        with tarfile.open(fileobj=io.BytesIO(resp.content)) as tf:
            names = tf.getnames()
            result_meta = json.loads(tf.extractfile("_result.json").read())
            ok_tar = ("a-c01-0000.mp4" in names and "_result.json" in names
                      and result_meta.get("_provider") == "modal")
    check("GET /jobs/{id}/result 重打包 tar（mp4 + _result.json 带 _provider）", ok_tar)
    
    resp = client.post("/clips/async", data={
        "spec": json.dumps({"clips": [{"out": "../evil.mp4", "s0": 0, "s1": 2}]}),
        "direct_url": "https://cdn.example.com/x.mp4"})
    check("异步 /clips/async 校验对齐同步版（路径注入 400）", resp.status_code == 400)
    resp = client.post("/clips/async", data={"spec": json.dumps({"clips": []})})
    check("异步校验：空 clips 400", resp.status_code == 400)
    check("GET /jobs/{未知}/result → 404",
          client.get("/jobs/ghost/result").status_code == 404)
    
    # 未终态 → 409：把唯一 provider 换成慢实现占住 worker
    _orig_cut = hp.cut_clips
    
    
    def _slow_cut(*a, **k):
        time.sleep(0.3)
        return {"a-c01-0000.mp4": True}, _TMP
    
    
    try:
        hp.cut_clips = _slow_cut
        resp = client.post("/clips/async", data={
            "spec": json.dumps({"clips": [{"out": "a-c01-0000.mp4", "s0": 0, "s1": 2}]}),
            "direct_url": "https://cdn.example.com/x.mp4"})
        acc = resp.json()
        early = client.get(f"/jobs/{acc['job_id']}/result")
        check("未终态取结果 → 409 带 status",
              early.status_code == 409 and "status" in early.json()["detail"])
        r.jobs.wait(acc["job_id"], timeout=5)
        check("终态后再取 → 200", client.get(f"/jobs/{acc['job_id']}/result").status_code == 200)
    finally:
        hp.cut_clips = _orig_cut
    
    # ================================================================ kaggle_impl 迁 tasking 契约
    print("== kaggle_impl（机械上收内核 tasking，插件只剩模板+解析）==")
    import importlib
    
    from gpu_router.config import _provider_from_dict
    from gpu_router.providers import build_provider
    from gpu_router.providers.kaggle import KaggleProvider
    
    kimpl = importlib.import_module("v2md.providers.kaggle_impl")
    kp = build_provider(_provider_from_dict({"name": "kg", "kind": "kaggle"}),
                        {"kaggle": ["v2md.providers.kaggle_impl"]})
    check("kaggle provider 装载插件三能力方法",
          all(callable(getattr(kp, m, None)) for m in ("transcribe", "cut_clips", "ocr")))
    
    kb = KaggleProvider(capabilities=("asr",))
    kb._user = "u1"
    kimpl._bind(kb)
    check("_bind 钉回 v2md 命名（模板 IN=/kaggle/input/v2md-io 联动）",
          kb.dataset == "u1/v2md-io" and kb.kernel == "u1/v2md-job")
    kb_cfg = KaggleProvider(extra={"dataset": "x/y"})
    kb_cfg._user = "u1"
    kimpl._bind(kb_cfg)
    check("_bind 不覆盖显式配置", kb_cfg.dataset == "x/y" and kb_cfg.kernel == "u1/v2md-job")
    
    
    def _stub_run(arts):
        def _rt(files, spec, want, **kw):
            return "t-1", {k: v for k, v in arts.items() if k in want}
        return _rt
    
    
    kp.run_task = _stub_run({
        "out.json": json.dumps({"model": "turbo", "segments": [1]}).encode(),
        "status.json": b'{"state":"done"}'})
    res = kp.transcribe(_TMP / "x.m4a", "turbo", "zh", _TMP)
    check("transcribe：run_task 产物解析", res["model"] == "turbo")
    
    kp.run_task = _stub_run({"status.json": b'{"state":"error","error":"CUDA OOM"}'})
    try:
        kp.transcribe(_TMP / "x.m4a", "turbo", "zh", _TMP)
        check("内核 status error → 业务报错", False)
    except RuntimeError as exc:
        check("内核 status error → 业务报错", "CUDA OOM" in str(exc))
    
    kp.run_task = _stub_run({
        "out.json": b'{"results":[{"name":"g.jpg","text":"hi","conf":0.5}]}',
        "status.json": b'{"state":"done"}'})
    res = kp.ocr([_TMP / "g_001.jpg"], _TMP)
    check("ocr：results 提取", res[0]["text"] == "hi")
    
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        data = b"mp4"
        info = tarfile.TarInfo("a-c01-0000.mp4")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    kp.run_task = _stub_run({
        "clips.tar": buf.getvalue(),
        "clips_result.json": b'{"a-c01-0000.mp4": true, "b-c02.mp4": false}',
        "status.json": b'{"state":"done"}'})
    results, od = kp.cut_clips(None, "https://cdn/x.mp4",
                               [{"out": "a-c01-0000.mp4", "s0": 0, "s1": 2},
                                {"out": "b-c02.mp4", "s0": 0, "s1": 1}], _TMP)
    check("cut_clips：解包 + 缺片回填 False",
          results["a-c01-0000.mp4"] is True and results["b-c02.mp4"] is False
          and (od / "a-c01-0000.mp4").read_bytes() == b"mp4")
    
    seen = {}
    
    
    def _capture_run(files, spec, want, **kw):
        seen.update(files=files, spec=spec, want=want)
        return "t", {"status.json": b'{"state":"done"}',
                     "clips_result.json": b"{}"}
    
    
    kp.run_task = _capture_run
    kp.cut_clips(_TMP / "x.m4a", "", [{"out": "a.mp4", "s0": 0, "s1": 1}], _TMP)
    check("视频以 Path 进 files（流式不进内存）+ spec 带内核脚本",
          isinstance(seen["files"]["clip-src.mp4"], Path) and "script" in seen["spec"]
          and "clip_job.json" in seen["files"])
    
    # ================================================================ 全灭 502
    print("== 全灭 502 ==")
    # 有插件但唯一 provider 健康探测失败 → 端点在、转发全灭 → 502
    cfg_dead_file = _TMP / "gpu_router_dead.json"
    cfg_dead_file.write_text(json.dumps({
        "listen": "127.0.0.1:9701",
        "plugins": ["v2md"],
        "plugin_paths": [str(V2MD_ROOT / "gpu_plugins")],
        "providers": [{"name": "dead", "kind": "http", "url": "http://127.0.0.1:1",
                       "read_timeout": 2}]}))
    cfg_dead = load_config(cfg_dead_file)
    r2 = Router.from_config(cfg_dead, StateStore(_TMP / "state2.json"))
    c2 = TestClient(create_app(router=r2), raise_server_exceptions=False)
    resp = c2.post("/transcribe", files={"audio": ("in.m4a", b"x", "audio/mp4")},
                   data={"model": "turbo", "language": "zh"})
    check("全灭 → 502 带 errors", resp.status_code == 502
          and "errors" in resp.json()["detail"])
    # 无插件配置：内核零能力，业务端点不存在（404）——插件化架构的正确行为
    cfg_bare = load_config(_TMP / "nonexistent.json")
    r3 = Router.from_config(cfg_bare, StateStore(_TMP / "state3.json"))
    c3 = TestClient(create_app(router=r3), raise_server_exceptions=False)
    check("无插件配置 → /transcribe 不存在（404）",
          c3.post("/transcribe", files={"audio": ("x", b"x", "audio/mp4")}).status_code == 404)
    
    print()
    assert not FAILED, f'{len(FAILED)} 项失败：' + '; '.join(FAILED)

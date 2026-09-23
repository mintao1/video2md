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
GPU_ROUTER_ROOT = Path("/Users/linmintao/Desktop/开发项目/gpu_router")
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

# ================================================================ 插件加载与聚合
print("== v2md 插件加载 ==")
from gpu_router.plugins import load_plugins

plugs = load_plugins(["v2md"], [V2MD_ROOT / "gpu_plugins"])
check("v2md 插件加载 + 三能力契约",
      len(plugs) == 1 and plugs[0].capabilities ==
      {"asr": "transcribe", "clips": "cut_clips", "ocr": "ocr"})
check("PROVIDER_IMPL 声明三 kind",
      set(plugs[0].module.PROVIDER_IMPL) == {"colab-cli", "kaggle", "zerogpu"})

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
if FAILED:
    print(f"❌ {len(FAILED)} 项失败：" + "; ".join(FAILED))
    sys.exit(1)
print("✅ 全部通过")

"""远端算力薄客户端单测：remote_cut_clips / ocr_remote（GPU 路由或参考服务端的 HTTP 契约）。

GPU 路由本体已独立成 ~/Desktop/开发项目/gpu_router（那边 tests/ 管路由逻辑）；
这里只保 video2md 侧的客户端行为。不联网、MockTransport 桩。

跑法：python3 test_remote_clients.py
"""
from __future__ import annotations

import io
import json
import sys
import tarfile
import tempfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from video2md import clips as clips_mod
from video2md import frames as frames_mod

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


_TMP = Path(tempfile.mkdtemp(prefix="v2md-remote-clients-"))


def _tar_bytes() -> bytes:
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


# ---- clips 客户端
# Phase 2 后 attach 落点走 artsink.attach_dir()（动态读 config），重定向打在 config 缝上
from video2md import config as _cfg
_cfg.ATTACH_DIR = _TMP / "attachments"  # 重定向：别碰真 vault
_cfg.ATTACH_DIR.mkdir(exist_ok=True)
clips_mod.ENV = {**clips_mod.ENV, "V2MD_CLIPS_SOURCE": ""}  # 钉死上传路径（与 .env 无关）
(_TMP / "fake.mp4").touch()


def clips_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/clips":
        return httpx.Response(200, content=_tar_bytes())
    return httpx.Response(404)


mc = httpx.Client(transport=httpx.MockTransport(clips_handler), base_url="http://r")
published = [("a-f01-0000.jpg", "00:00")]
res = clips_mod.remote_cut_clips(published, _TMP / "fake.mp4", 120.0, [], _TMP,
                                 "http://r", direct_url="https://cdn/x.mp4", client=mc)
check("remote_cut_clips：spec 映射 + mp4 落 attachments",
      res.get("a-f01-0000.jpg") == "a-c01-0000.mp4"
      and (_cfg.ATTACH_DIR / "a-c01-0000.mp4").exists())

err_client = httpx.Client(transport=httpx.MockTransport(
    lambda req: httpx.Response(502, text="全灭")), base_url="http://r")
_thrown = ""
try:
    # 换一帧名：上一用例的产物已入库，同名会因幂等直接命中、根本不发 HTTP
    clips_mod.remote_cut_clips([("b-f01-0001.jpg", "00:01")], _TMP / "fake.mp4",
                               120.0, [], _TMP, "http://r", client=err_client)
except RuntimeError as exc:
    _thrown = str(exc)
check("remote_cut_clips：非 200 抛错不回退", "HTTP 502" in _thrown)

# ---- ocr 客户端
def ocr_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/ocr":
        return httpx.Response(200, json={"results": [
            {"name": "g_001.jpg", "text": "要点一", "conf": 0.92},
            {"name": "g_002.jpg", "text": "", "conf": 0.0}]})
    return httpx.Response(404)


(_TMP / "g_001.jpg").write_bytes(b"jpg1")
(_TMP / "g_002.jpg").write_bytes(b"jpg2")
mc2 = httpx.Client(transport=httpx.MockTransport(ocr_handler), base_url="http://r")
raw = frames_mod.ocr_remote([(1.0, _TMP / "g_001.jpg"), (2.0, _TMP / "g_002.jpg")],
                            "http://r", client=mc2)
check("ocr_remote：按名字对齐时间戳",
      raw[0][0] == 1.0 and raw[0][1] == "要点一" and raw[1][1] == "")
items, _raw2 = frames_mod.ocr_candidates([(1.0, _TMP / "g_001.jpg")], "remote",
                                         base="http://r", client=mc2)
check("ocr_candidates remote 分支 + LOW_CONF 过滤", items == [(1.0, "要点一", 0.92)])

print()
if FAILED:
    print(f"❌ {len(FAILED)} 项失败：" + "; ".join(FAILED))
    sys.exit(1)
print("✅ 全部通过")

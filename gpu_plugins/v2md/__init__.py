"""v2md 能力插件：asr（whisper 转录）/ clips（视频节段剪辑）/ ocr（帧 OCR）。

归属 dify-video2md 仓（负载语义、参考服务端、Colab 会话机械都是 video2md 的业务），
gpu_router 内核经 plugins.py 协议加载本包——gpu_router.json：
    "plugins": ["v2md"],
    "plugin_paths": ["../dify-video2md/gpu_plugins"]

能力 → provider 方法契约：
- asr    → transcribe(audio, model, language, tmp) -> whisper 同形 JSON
- clips  → cut_clips(video, direct_url, clips, tmp) -> (results, out_dir)
- ocr    → ocr(images, tmp) -> [{"name", "text", "conf", "err"}]

provider 侧实现：
- http kind（Modal/隧道/国内云）：部署 reference/app.py（同目录，原 gpu_router/reference 迁入）
- colab-cli kind：providers/colab_machinery.py（原 gpu_router/providers/colab_machinery 迁入，
  lease/queue 看门狗协同是 video2md 专属，随插件走）
- kaggle / zerogpu kind：providers/kaggle_impl.py、providers/zerogpu_impl.py
  （v2md 内核模板与 Space，原实现迁入；gpu_router 侧 provider 类已泛化）

WATCHDOG：Colab 会话空闲自动关停（原 __main__ 硬编码逻辑迁入，内核只起线程）。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import File, UploadFile

# 端点签名用的 Annotated 别名必须在模块级（FastAPI 解析字符串注解走模块全局命名空间，
# 放在 register_server 函数作用域内会解析失败退化为 query 参数）
UploadFileAnnotated = Annotated[UploadFile, File(...)]
OcrFiles = Annotated[list[UploadFile], File(...)]

CAPABILITIES = {
    "asr": "transcribe",
    "clips": "cut_clips",
    "ocr": "ocr",
}

# 各 provider kind 的默认能力集（provider 条目可用 "capabilities" 覆盖）
KIND_CAPABILITIES = {
    "http": ("asr", "clips", "ocr"),        # 参考服务端三端点（Modal/隧道/国内云通用）
    "colab-cli": ("asr", "clips", "ocr"),   # CLI 会话：asr/clips 现成，ocr 走 rapidocr worker
    "kaggle": ("asr", "clips", "ocr"),      # 批内核三模板
    "zerogpu": ("asr",),                    # 免费日配额以分钟计，只配给 ASR 兜底
}

# 各 kind 的能力实现模块（gpu_router 侧 build_provider 据此装载业务方法）
PROVIDER_IMPL = {
    "colab-cli": "v2md.providers.colab_impl",
    "kaggle": "v2md.providers.kaggle_impl",
    "zerogpu": "v2md.providers.zerogpu_impl",
}


def WATCHDOG(is_inflight):
    """Colab 会话空闲看门狗（原 gpu_router/__main__.py 的 colab 分支迁入）：
    路由在途=0 且下游队列见底且 lease 不新鲜 → 自动 stop，配额只在有活干时消耗。"""
    from .providers import colab_machinery as M
    return M.autostop_watchdog(is_inflight)


def register_server(app, router) -> None:
    """挂 /transcribe /clips /ocr 三端点（原 gpu_router/server.py 业务面原样迁入，
    契约不变——video2md 侧 HTTP 客户端零改动）。"""
    import io
    import json
    import re
    import shutil
    import tarfile
    import tempfile
    from pathlib import Path

    from fastapi import Form, HTTPException
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    from gpu_router.errors import AllProvidersFailed

    def _fail(cap: str, exc: AllProvidersFailed) -> HTTPException:
        return HTTPException(status_code=502, detail={
            "error": str(exc), "capability": cap,
            "errors": exc.errors, "skipped": exc.skipped})

    def _save_upload(up: UploadFile, dest: Path) -> Path:
        with dest.open("wb") as fh:
            shutil.copyfileobj(up.file, fh, length=1024 * 1024)
        return dest

    # 剪辑产物名：与 video2md 本地清单同源（xx-c01-0105.mp4），白名单校验杜绝路径注入
    _OUT_NAME_RE = re.compile(r"^[\w.\-]+\.mp4$")

    @app.post("/transcribe")
    def transcribe(audio: UploadFileAnnotated, model: str = Form("turbo"),
                   language: str = Form("zh")):
        tmp = Path(tempfile.mkdtemp(prefix="gpu-router-asr-"))
        try:
            ap = _save_upload(audio, tmp / (audio.filename or "in.m4a"))
            try:
                data, who = router.call("asr", "transcribe", ap, model, language, tmp)
            except AllProvidersFailed as exc:
                raise _fail("asr", exc)
            data["_provider"] = who  # 观测：上游日志能直接看到这条转录落在哪家
            return data
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @app.post("/clips")
    def clips(spec: str = Form(...), direct_url: str = Form(""),
              video: UploadFileAnnotated | None = None):
        try:
            specs = json.loads(spec).get("clips", [])
        except (json.JSONDecodeError, AttributeError):
            raise HTTPException(400, "spec 不是合法 JSON（期望 {\"clips\": [{out,s0,s1}]}）")
        if not specs:
            raise HTTPException(400, "spec.clips 为空")
        bad = [c.get("out", "") for c in specs if not _OUT_NAME_RE.match(str(c.get("out", "")))]
        if bad:
            raise HTTPException(400, f"非法产物名（只允许平铺 mp4 文件名）: {bad[:3]}")
        if video is None and not direct_url:
            raise HTTPException(400, "video 与 direct_url 至少给一样")
        tmp = Path(tempfile.mkdtemp(prefix="gpu-router-clips-"))
        try:
            vp = _save_upload(video, tmp / "clip-src.mp4") if video is not None else None
            try:
                (results, out_dir), who = router.call(
                    "clips", "cut_clips", vp, direct_url, specs, tmp)
            except AllProvidersFailed as exc:
                raise _fail("clips", exc)
            tar_path = tmp / "clips.tar"
            with tarfile.open(tar_path, "w") as tf:
                for name, ok in results.items():
                    p = out_dir / name
                    if ok and p.exists():
                        tf.add(p, arcname=name)
                payload = json.dumps({**results, "_provider": who}).encode()
                info = tarfile.TarInfo("_result.json")
                info.size = len(payload)
                tf.addfile(info, io.BytesIO(payload))
            return FileResponse(tar_path, media_type="application/x-tar",
                                filename="clips.tar",
                                background=BackgroundTask(shutil.rmtree, tmp, True))
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

    @app.post("/ocr")
    def ocr(images: OcrFiles):
        if not images:
            raise HTTPException(400, "images 为空")
        tmp = Path(tempfile.mkdtemp(prefix="gpu-router-ocr-"))
        try:
            paths = [_save_upload(up, tmp / f"{i:03d}-{Path(up.filename or 'f.jpg').name}")
                     for i, up in enumerate(images)]
            try:
                results, who = router.call("ocr", "ocr", paths, tmp)
            except AllProvidersFailed as exc:
                raise _fail("ocr", exc)
            # 名字还原：去掉防重名前缀（参考服务端只认纯文件名）
            for r in results:
                r["name"] = re.sub(r"^\d{3}-", "", str(r.get("name", "")))
            return {"results": results, "provider": who}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

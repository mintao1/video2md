"""Modal 部署入口：把参考服务端（app.py）跑成 T4 GPU 的 Web Endpoint。

一次性准备（需要 Modal 账号，$30/月免费额度 ≈ 50h T4）：
    pip install modal
    modal token new
    modal deploy gpu_router/reference/modal_app.py
部署输出里的 https://xxx.modal.run 填进 gpu_router.json：
    {"name": "modal", "kind": "http", "url": "https://xxx.modal.run"}

要点：
- 模型（whisper turbo ~1.6GB + rapidocr ~15MB）缓存在 Volume，冷启动只发生在第一次/
  长时间闲置后（scaledown_window 内复用热容器）。
- timeout 2700s 与 video2md 的 V2MD_COLAB_MAX_WAIT 对齐：长视频 clips 不会被中途掐死。
- 并发上限 1：与路由的串行语义一致（video2md serve 本来就一次一个阶段作业）。
"""
from __future__ import annotations

import modal

app = modal.App("v2md-gpu")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install(
        "fastapi", "uvicorn[standard]", "python-multipart",
        "faster-whisper>=1.1", "rapidocr",
    )
    # 参考服务端源码随镜像带上（保持单文件自包含，Modal 端不依赖本仓其余代码）
    .add_local_file(str(__import__("pathlib").Path(__file__).parent / "app.py"),
                    "/root/ref_app.py", copy=True)
)

model_cache = modal.Volume.from_name("v2md-models", create_if_missing=True)



@app.function(image=image, gpu="T4", timeout=2700, scaledown_window=300,
              max_containers=1,
              volumes={"/root/.cache": model_cache})  # HF/RapidOCR 模型默认都落在 ~/.cache
@modal.concurrent(max_inputs=1)
@modal.asgi_app()
def serve():
    import sys
    sys.path.insert(0, "/root")
    import importlib.util
    spec = importlib.util.spec_from_file_location("ref_app", "/root/ref_app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.app

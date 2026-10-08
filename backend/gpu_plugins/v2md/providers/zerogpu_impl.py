"""zerogpu provider 的 v2md 能力实现：transcribe（HF ZeroGPU Gradio Space）。

原 gpu_router/providers/zerogpu.py 的方法体迁入（Space 部署体在同目录
reference/zerogpu_space/，业务归 video2md）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def transcribe(provider, audio: Path, model: str, language: str, tmp: Path) -> dict:
    from gradio_client import Client, handle_file

    with provider.lock:
        client = Client(provider.space, hf_token=os.environ.get("HF_TOKEN") or None)
        result = client.predict(handle_file(str(audio)), model, language,
                                api_name="/transcribe")
    # gr.JSON 输出 → dict；个别版本落到文件路径 → 读回来
    if isinstance(result, (str, os.PathLike)) and Path(result).exists():
        result = json.loads(Path(result).read_text())
    if not isinstance(result, dict) or "segments" not in result:
        raise RuntimeError(f"ZeroGPU 返回形状异常: {str(result)[:200]}")
    return result

"""mockpool：GPU 算力池契约的假实现（compose 冒烟专用，零 GPU 零 token）。

契约与 gpu_router / reference/app.py 一致：GET /ping、POST /transcribe、POST /ocr、POST /clips。
只验证管线接线与数据结构（图能不能跑通、字段对不对），不验证真实识别质量——
真实质量冒烟由部署后的 B 站短视频负责（README「部署」章）。
"""
from __future__ import annotations

import io
import json
import tarfile

from fastapi import FastAPI, Form, UploadFile
from fastapi.responses import Response

app = FastAPI(docs_url=None, redoc_url=None)

# 转录本总长度必须过 note 门禁（MIN_NOTE_TRANSCRIPT_CHARS=200，低了按片头/片尾噪音拒写盘）
_SEGS = [
    {"start": 0.0, "end": 5.0,
     "text": "量子纠缠是粒子之间的奇妙关联，测量其中一个粒子的状态会瞬间确定另一个的状态。" * 2,
     "no_speech_prob": 0.1, "compression_ratio": 1.0, "avg_logprob": -0.2},
    {"start": 5.0, "end": 10.0,
     "text": "光合作用将光能转化为化学能，叶绿体中的色素分子吸收光子并驱动电子传递链。" * 2,
     "no_speech_prob": 0.1, "compression_ratio": 1.0, "avg_logprob": -0.2},
    {"start": 10.0, "end": 15.0,
     "text": "深度学习的本质是多层特征变换，每一层都在学习数据的不同抽象表示。" * 2,
     "no_speech_prob": 0.1, "compression_ratio": 1.0, "avg_logprob": -0.2},
]


@app.get("/ping")
def ping() -> dict:
    return {"ok": True, "model": "mock", "ocr": "mock"}


@app.post("/transcribe")
async def transcribe(audio: UploadFile, model: str = Form("turbo"),
                     language: str = Form("zh")) -> dict:
    return {"model": model, "language": language, "duration": 12.0,
            "segments": _SEGS, "_provider": "mockpool"}


@app.post("/ocr")
async def ocr(images: list[UploadFile]) -> dict:
    return {"results": [{"name": img.filename, "text": "光合作用 幻灯片", "conf": 0.95}
                        for img in images],
            "provider": "mockpool"}


@app.post("/clips")
async def clips(spec: str = Form(...), video: UploadFile | None = None,
                direct_url: str = Form("")) -> Response:
    reqs = json.loads(spec).get("clips", [])
    buf = io.BytesIO()
    result: dict[str, object] = {}
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for c in reqs:
            data = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 256  # 假 mp4：名字到位即可，内容无人校验
            ti = tarfile.TarInfo(c["out"])
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
            result[c["out"]] = True
        payload = json.dumps({**result, "_provider": "mockpool"}).encode()
        ti = tarfile.TarInfo("_result.json")
        ti.size = len(payload)
        tf.addfile(ti, io.BytesIO(payload))
    return Response(buf.getvalue(), media_type="application/x-tar")

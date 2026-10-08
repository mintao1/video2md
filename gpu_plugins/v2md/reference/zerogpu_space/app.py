"""HF ZeroGPU Space：faster-whisper ASR（A100 按秒计费，免费档每天几分钟）。

部署：把整个 zerogpu_space/ 目录推到一个新 Space（SDK=Gradio）；
hardware 选 ZeroGPU（免费档自动有配额）。路由侧用 gradio_client 调 /transcribe。

模型在 @spaces.GPU 函数内懒加载并按名缓存：只有调用才占 GPU 秒数；
turbo 在 A100 上加载 ~20–30s，之后热调用 16min 音频 ≈ 30–40s。

⚠️ 配额按 spaces.GPU 的**预留 duration** 计而不是实际运行时长（官方论坛确认）——
写死大 duration 会白白烧配额，所以这里用动态 duration：按音频体积估时长。
"""
from __future__ import annotations

import os

import gradio as gr
import spaces

_MODELS: dict = {}



def _duration_for(audio_path: str, model_name: str, language: str) -> int:
    """动态预留时长：64kbps m4a ≈ 0.48MB/min；A100 批处理 ≈10–15× 实时；
    加 30s 冷载缓冲，clamp 在 [60, 240]s（免费日配额只有 300s，兜底场景本就只接短音频）。"""
    try:
        minutes = os.path.getsize(audio_path) / 1e6 / 0.48
    except OSError:
        minutes = 1.0
    return max(60, min(240, int(30 + minutes * 6)))



@spaces.GPU(duration=_duration_for)
def transcribe(audio_path: str, model_name: str, language: str) -> dict:
    from faster_whisper import WhisperModel

    name = (model_name or "turbo").strip() or "turbo"
    if name not in _MODELS:
        _MODELS[name] = WhisperModel(name, device="cuda", compute_type="float16")
    m = _MODELS[name]
    kw = dict(language=None if language == "auto" else language,
              condition_on_previous_text=False, vad_filter=True,
              initial_prompt="以下是普通话的简体中文内容。"
                  if (language or "").startswith("zh") else None)
    try:
        from faster_whisper import BatchedInferencePipeline
        segs_iter, info = BatchedInferencePipeline(model=m).transcribe(
            audio_path, batch_size=16, **kw)
    except Exception:
        segs_iter, info = m.transcribe(audio_path, **kw)
    segments = [{"start": s.start, "end": s.end, "text": s.text.strip(),
                 "no_speech_prob": s.no_speech_prob,
                 "compression_ratio": s.compression_ratio,
                 "avg_logprob": s.avg_logprob} for s in segs_iter]
    return {"model": name, "language": info.language,
            "duration": info.duration, "segments": segments}



demo = gr.Interface(
    fn=transcribe,
    inputs=[gr.Audio(type="filepath", label="音频"),
            gr.Textbox(value="turbo", label="模型"),
            gr.Textbox(value="zh", label="语言")],
    outputs=gr.JSON(label="转录结果"),
    api_name="transcribe",
    title="video2md ASR（ZeroGPU）",
    allow_flagging="never",
)

demo.queue(max_size=8).launch()

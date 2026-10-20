#!/usr/bin/env python3
"""Explicit real-Colab smoke: ASR + OCR + clips, no LLM, isolated local files.

Run after `colab sessions` authorization:
  backend/.venv/bin/python backend/scripts/smoke_colab_cli.py --session v2md-smoke
On Linux supply --video /path/to/a/short/spoken.mp4 (macOS can synthesize speech).
The named session is released on success or failure; do not use another job's session.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path)
    parser.add_argument("--session", default="v2md-smoke")
    parser.add_argument("--model", default="tiny")
    args = parser.parse_args()
    if not args.video and not shutil.which("say"):
        parser.error("当前平台请提供 --video（短语音视频）")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(prefix="v2md-colab-smoke-") as directory:
        root = Path(directory)
        os.environ.update({
            "V2MD_REMOTE": "colab-cli", "V2MD_OCR_REMOTE": "colab-cli",
            "V2MD_CLIPS_REMOTE": "colab-cli", "V2MD_CLIPS_SOURCE": "local",
            "V2MD_COLAB_SESSION": args.session, "V2MD_ARTIFACT_MODE": "vault",
            "V2MD_RUNS_STORE": "json", "V2MD_STATE_STORE": "json",
            "V2MD_RUNS_FILE": str(root / "runs.json"), "V2MD_STATE_FILE": str(root / "state.json"),
            "V2MD_COLAB_LOCK": str(root / "colab.lock"),
            "V2MD_LOG_DIR": str(root / "logs"), "OBSIDIAN_VAULT": str(root / "vault"),
            "V2MD_SECRETS_FILE": str(root / "no-secrets.env"),
            "LANGFUSE_PK": "", "LANGFUSE_SK": "", "OTEL_EXPORTER_OTLP_ENDPOINT": "",
        })
        from PIL import Image, ImageDraw, ImageFont

        from video2md import artsink, colab
        from video2md.clips import remote_cut_clips
        from video2md.frames import ocr_candidates
        from video2md.transcribe import transcribe

        frame = root / "validation.jpg"
        image = Image.new("RGB", (640, 360), "white")
        ImageDraw.Draw(image).text((35, 130), "VIDEO2MD TEST", fill="black", font=ImageFont.load_default(size=48))
        image.save(frame)
        video = args.video
        if video is None:
            speech = root / "speech.aiff"
            subprocess.run(["say", "-v", "Samantha", "-o", str(speech),
                            ("This is the video integration test. We are testing speech recognition, "
                             "image text recognition, and video clips on a cloud graphics processor.")], check=True)
            video = root / "speech.mp4"
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-loop", "1",
                            "-i", str(frame), "-i", str(speech), "-c:v", "libx264", "-tune", "stillimage",
                            "-c:a", "aac", "-pix_fmt", "yuv420p", "-shortest", str(video)], check=True)
        artsink.attach_dir().mkdir(parents=True, exist_ok=True)
        with colab.session_scope():
            segs, confidence, _dropped = transcribe(video, args.model, "en", root, "colab-cli")
            assert segs and any(s["text"].strip() for s in segs), "ASR 未输出语音段"
            print(f"ASR PASS: {len(segs)} segments, confidence={confidence:.3f}", flush=True)
            items, _raw = ocr_candidates([(0.0, frame)], "remote", "colab-cli")
            assert any("VIDEO" in text.upper() for _t, text, _conf in items), "OCR 未识别画面文字"
            print(f"OCR PASS: {len(items)} text frames", flush=True)
            clips = remote_cut_clips([(frame.name, "00:01")], video, 5.0, [], root, "colab-cli")
            assert clips and all(clips.values()), "剪辑产物缺失"
            for name in clips.values():
                path = artsink.attach_dir() / name
                subprocess.run(["ffprobe", "-v", "error", str(path)], check=True)
            print(f"CLIPS PASS: {len(clips)} playable clips", flush=True)
        print("COLAB CLI SMOKE PASS (session released)", flush=True)


if __name__ == "__main__":
    main()

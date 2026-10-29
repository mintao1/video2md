"""抽样对比 本地 macOS Vision 与远端 GPU OCR（路由/参考服务端）的识别差异。

跑法：
  python3 backend/scripts/ocr_compare.py <帧目录> [base_url] [--limit N]
  base_url 默认 http://127.0.0.1:9700（gpu_router），也可直指参考服务端。

输出逐帧两边文本与归一化一致率（_NORM 同款规整），用于验收「OCR 外移不掉质量」。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from video2pdf.frames import _NORM, ocr_remote, ocr_vision


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    d = Path(args[0])
    base = args[1] if len(args) > 1 else "http://127.0.0.1:9700"
    limit = 10
    for a in sys.argv[1:]:
        if a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
    frames = sorted([*d.glob("*.jpg"), *d.glob("*.png")])[:limit]
    if not frames:
        print(f"❌ {d} 下没有 jpg/png")
        return 1

    raw = ocr_remote([(float(i), p) for i, p in enumerate(frames)], base)
    remote = {p.name: {"text": row[1], "conf": row[2]}
              for p, row in zip(frames, raw)}
    same = both_hit = 0
    for i, p in enumerate(frames, 1):
        v_txt, v_conf = ocr_vision(p)
        r = remote.get(p.name, {})
        r_txt, r_conf = r.get("text", ""), r.get("conf", 0.0)
        agree = _NORM(v_txt) == _NORM(r_txt) and bool(v_txt)
        same += agree
        both_hit += bool(v_txt and r_txt)
        print(f"[{i:02d}] {p.name}  一致={'✅' if agree else '❌'}")
        print(f"     vision({v_conf:.2f}): {v_txt[:80]!r}")
        print(f"     remote({r_conf:.2f}): {r_txt[:80]!r}")
    print(f"\n一致 {same}/{len(frames)} · 双端都识别到文字 {both_hit}/{len(frames)}")
    return 0



if __name__ == "__main__":
    sys.exit(main())

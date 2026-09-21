#!/usr/bin/env python3
"""一次性冒烟：抖音博主主页走完整工作流。用法: douyin_smoke.py [part_items]（空=全量）。"""
import json
import sys
import time

import requests

sys.path.insert(0, "/Users/linmintao/Desktop/开发项目/dify_kit")
from dify_kit.config import load_config  # noqa: E402

cfg = load_config()
DIFY = cfg["base_url"].rstrip("/")
KEY = cfg.get("app_key")
URL = "https://www.douyin.com/user/MS4wLjABAAAAEdoinwc8bC1HTF95XlLojlOl-8FbK0jMnf7zsUyRDQIOhk6xSQilNHfupC3YWiNR"
PART_ITEMS = sys.argv[1] if len(sys.argv) > 1 else ""

print(f"[{time.strftime('%H:%M:%S')}] 发起: part_items={PART_ITEMS or '(全量)'}", flush=True)
t0 = time.time()
done = False
with requests.post(f"{DIFY}/workflows/run", headers={"Authorization": f"Bearer {KEY}"},
                   json={"inputs": {"url": URL, "part_items": PART_ITEMS}, "response_mode": "streaming",
                         "user": "zcode-douyin-smoke"}, stream=True, timeout=(30, 7200)) as r:
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        try:
            obj = json.loads(line[5:].strip())
        except Exception:
            continue
        ev = obj.get("event")
        if ev == "workflow_finished":
            d = obj.get("data") or {}
            print(f"[{time.strftime('%H:%M:%S')}] 完成 status={d.get('status')} 用时={time.time()-t0:.0f}s",
                  flush=True)
            print("outputs:", json.dumps(d.get("outputs") or {}, ensure_ascii=False)[:500], flush=True)
            done = True
            break
        if ev == "error":
            print("error:", str(obj)[:300], flush=True)
            done = True
            break
if not done:
    print("流结束未见 workflow_finished", flush=True)

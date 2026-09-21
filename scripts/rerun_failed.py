#!/usr/bin/env python3
"""重跑修复前失败的 URL（串行，避免并发触发 B 站 412 风控）。用法: rerun_failed.py"""
import json
import sys
import time

import requests

sys.path.insert(0, "/Users/linmintao/Desktop/开发项目/dify_kit")
from dify_kit.config import load_config  # noqa: E402

cfg = load_config()
DIFY = cfg["base_url"].rstrip("/")
KEY = cfg.get("app_key")

URLS = [
    "https://www.bilibili.com/video/BV1ADge6YEvE",
    "https://www.bilibili.com/video/BV17gMi6DEFW",
]

def run_one(url: str) -> bool:
    print(f"[{time.strftime('%H:%M:%S')}] ▶ 发起 {url}", flush=True)
    t0 = time.time()
    ok = False
    try:
        with requests.post(f"{DIFY}/workflows/run", headers={"Authorization": f"Bearer {KEY}"},
                           json={"inputs": {"url": url}, "response_mode": "streaming",
                                 "user": "zcode-rerun-failed"}, stream=True, timeout=(30, 7200)) as r:
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
                    st = d.get("status")
                    err = (d.get("error") or "").strip().splitlines()
                    print(f"[{time.strftime('%H:%M:%S')}] 完成 status={st} 用时={time.time()-t0:.0f}s", flush=True)
                    if err:
                        print("  error 末行:", err[-1][:220], flush=True)
                    out = d.get("outputs") or {}
                    print("  outputs:", json.dumps(out, ensure_ascii=False)[:300], flush=True)
                    ok = st == "succeeded"
                    break
                if ev == "error":
                    print("  error:", str(obj)[:300], flush=True)
                    break
    except Exception as e:
        print(f"  ⚠ 调用异常: {str(e)[:200]}", flush=True)
    return ok

results = {}
for u in URLS:
    results[u] = run_one(u)
print("\n==== 汇总 ====", flush=True)
for u, ok in results.items():
    print(("✅" if ok else "❌"), u, flush=True)
sys.exit(0 if all(results.values()) else 1)

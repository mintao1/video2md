#!/usr/bin/env python3
"""重跑修复前失败的 URL（LangGraph API 版，串行避免并发触发风控）。用法: rerun_failed.py [url...]"""
import os
import sys
import time

import httpx

API = (os.environ.get("V2PDF_API") or "http://127.0.0.1:8790").rstrip("/")

URLS_DEFAULT = [
    "https://www.bilibili.com/video/BV1ADge6YEvE",
    "https://www.bilibili.com/video/BV17gMi6DEFW",
]



def run_one(url: str) -> bool:
    print(f"[{time.strftime('%H:%M:%S')}] ▶ 发起 {url}", flush=True)
    t0 = time.time()
    try:
        r = httpx.post(f"{API}/runs", json={"url": url, "force": True},
                       timeout=httpx.Timeout(30.0, read=300.0))
        d = r.json()
        if r.status_code != 200 or not d.get("run_id"):
            print(f"  ❌ 建 run 失败: {str(d)[:220]}", flush=True)
            return False
    except Exception as e:
        print(f"  ⚠ 调用异常: {str(e)[:200]}", flush=True)
        return False
    run_id = d["run_id"]
    print(f"  run_id={run_id} total={d.get('total')}", flush=True)
    deadline = time.time() + 4 * 3600
    while time.time() < deadline:
        time.sleep(30)
        try:
            d = httpx.get(f"{API}/runs/{run_id}", timeout=30).json()
        except Exception:
            continue  # 单次轮询抖动不判死
        st = d.get("status") or (d.get("task") or {}).get("status") or ""
        if st in ("done", "partial", "error"):
            missing = [e.get("index") for e in d.get("entries", [])
                       if not e.get("skipped") and not (e.get("done") or {}).get("note")]
            print(f"[{time.strftime('%H:%M:%S')}] 完成 status={st} 用时={time.time()-t0:.0f}s"
                  + (f" 未写盘条目: {missing}" if missing else ""), flush=True)
            return st == "done" and not missing
    print("  ❌ 轮询超时", flush=True)
    return False



urls = sys.argv[1:] or URLS_DEFAULT
results = {}
for u in urls:
    results[u] = run_one(u)
print("\n==== 汇总 ====", flush=True)
for u, ok in results.items():
    print(("✅" if ok else "❌"), u, flush=True)
sys.exit(0 if all(results.values()) else 1)

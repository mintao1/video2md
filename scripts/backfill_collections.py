#!/usr/bin/env python3
"""分区间补跑驱动：把三个合集按 part_items 分段顺序补跑，每段跑完记产物数。

为什么分区间：工作流把一次 run 的**所有条目**转录本塞进**一次**「整理正文」LLM 调用
（max_tokens 16000）。907e1c53 是 4.4h 视频 → 一次写 17 篇必然被压缩/截断（这就是
「合集笔记都很薄」的结构性原因）。分段后每次只写 3-5 篇，质量和预算都可控。

顺序驱动而非并发：本地 serve 的 worker 是单线程串行，12 个 workflow 并发只会互相挤，
逐个跑时序可控、日志可读。

用法: python3 scripts/backfill_collections.py
日志: logs/backfill-YYYY-MM-DD.log
"""
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import requests

PROJ = Path("/Users/linmintao/ZCodeProject/dify-video2md")
sys.path.insert(0, "/Users/linmintao/Desktop/开发项目/dify_kit")
from dify_kit.config import load_config  # noqa: E402

cfg = load_config()
DIFY = cfg["base_url"].rstrip("/")
APP_KEY = os.environ.get("DIFY_APP_API_KEY") or cfg.get("app_key")
SERVE = "http://127.0.0.1:8790"
LOG = PROJ / "logs" / f"backfill-{date.today().isoformat()}.log"
RUN_TIMEOUT_S = int(os.environ.get("BACKFILL_RUN_TIMEOUT", "2700"))  # 单段上限 45min

# 分段表：按「累计时长 ≤ ~40min」切，长视频（61/42/43min）独占一段
PLAN = [
    ("907e1c53 17P BV1v4YY6zEqJ", "https://www.bilibili.com/video/BV1v4YY6zEqJ", "2-5"),   # 45m
    ("907e1c53 17P BV1v4YY6zEqJ", "https://www.bilibili.com/video/BV1v4YY6zEqJ", "6-10"),  # 23m
    ("907e1c53 17P BV1v4YY6zEqJ", "https://www.bilibili.com/video/BV1v4YY6zEqJ", "11"),    # 61m
    ("907e1c53 17P BV1v4YY6zEqJ", "https://www.bilibili.com/video/BV1v4YY6zEqJ", "12-15"), # 38m
    ("907e1c53 17P BV1v4YY6zEqJ", "https://www.bilibili.com/video/BV1v4YY6zEqJ", "16"),    # 42m
    ("907e1c53 17P BV1v4YY6zEqJ", "https://www.bilibili.com/video/BV1v4YY6zEqJ", "17"),    # 43m
    ("75b9b865 14P BV1Dae26iEsd", "https://www.bilibili.com/video/BV1Dae26iEsd", "1-5"),   # 35m
    ("75b9b865 14P BV1Dae26iEsd", "https://www.bilibili.com/video/BV1Dae26iEsd", "6-10"),  # 35m
    ("75b9b865 14P BV1Dae26iEsd", "https://www.bilibili.com/video/BV1Dae26iEsd", "11-14"), # 27m
    ("b2333284 11P BV19AUkBcEWD", "https://www.bilibili.com/video/BV19AUkBcEWD", "1-4"),   # 17m
    ("b2333284 11P BV19AUkBcEWD", "https://www.bilibili.com/video/BV19AUkBcEWD", "5"),     # 21m
    ("b2333284 11P BV19AUkBcEWD", "https://www.bilibili.com/video/BV19AUkBcEWD", "6-11"),  # 21m（含 0-1min 段，会被门禁跳过）
]


# 补跑（tail）计划：修复/迁移后需要重做的分段
#  907e1c53 6  = 老路径分篇少写一篇（5 条目只出 4 篇，条目1 落空）
#  907e1c53 11 = 交错驱动导致无图，且叠加「每条目子目录」迁移前的旧相对路径
#  907e1c53 17 = 同上无图
#  75b9b865 *  = 双写/热改 jobs.json 期间被打断的那几段
#  b2333284 *  = 竞品合集的薄笔记清理后重做
PLAN_TAIL = [
    ("907e1c53 17P BV1v4YY6zEqJ", "https://www.bilibili.com/video/BV1v4YY6zEqJ", "6"),
    ("907e1c53 17P BV1v4YY6zEqJ", "https://www.bilibili.com/video/BV1v4YY6zEqJ", "11"),
    ("907e1c53 17P BV1v4YY6zEqJ", "https://www.bilibili.com/video/BV1v4YY6zEqJ", "17"),
    ("75b9b865 14P BV1Dae26iEsd", "https://www.bilibili.com/video/BV1Dae26iEsd", "1-5"),
    ("75b9b865 14P BV1Dae26iEsd", "https://www.bilibili.com/video/BV1Dae26iEsd", "6-10"),
    ("75b9b865 14P BV1Dae26iEsd", "https://www.bilibili.com/video/BV1Dae26iEsd", "11-14"),
    ("b2333284 11P BV19AUkBcEWD", "https://www.bilibili.com/video/BV19AUkBcEWD", "1-4"),
    ("b2333284 11P BV19AUkBcEWD", "https://www.bilibili.com/video/BV19AUkBcEWD", "5"),
    ("b2333284 11P BV19AUkBcEWD", "https://www.bilibili.com/video/BV19AUkBcEWD", "6-11"),
]


# 补写计划：907e1c53 的分段 run 产物完好（转录本/选中帧/节段都在），
# 只是笔记文件在状态重置时被删、产物标记还写着已写盘 → 清标记后重写即可（无需重下载/转录）
PLAN_REWRITE = [
    ("907e1c53 17P BV1v4YY6zEqJ", "https://www.bilibili.com/video/BV1v4YY6zEqJ", "2-5"),
    ("907e1c53 17P BV1v4YY6zEqJ", "https://www.bilibili.com/video/BV1v4YY6zEqJ", "6-10"),
    ("907e1c53 17P BV1v4YY6zEqJ", "https://www.bilibili.com/video/BV1v4YY6zEqJ", "12-15"),
    ("907e1c53 17P BV1v4YY6zEqJ", "https://www.bilibili.com/video/BV1v4YY6zEqJ", "16"),
]


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def precreate(url: str, part_items: str) -> str:
    """先在本地建 run（拿到 run_id），工作流的建 run 节点会 dedup 到它。"""
    r = requests.post(f"{SERVE}/runs", json={"url": url, "part_items": part_items}, timeout=300)
    r.raise_for_status()
    d = r.json()
    return d["run_id"], d.get("total", 0), d.get("dedup", False)


def run_state(run_id: str) -> dict:
    try:
        return requests.get(f"{SERVE}/runs/{run_id}", timeout=30).json()
    except Exception:
        return {}


def count_notes(run_id: str) -> tuple[int, int]:
    """返回（写盘条目数, 条目总数），从 runs.json 取。"""
    try:
        runs = json.loads((PROJ / "runs.json").read_text())
        r = runs.get(run_id) or {}
        return sum(1 for a in r.get("artifacts", {}).values() if a.get("note")), r.get("total", 0)
    except Exception:
        return 0, 0


def fire_stream(url: str, part_items: str) -> tuple[str, dict]:
    """streaming：连接保持到工作流跑完（断连会中断运行，比 blocking+超时稳）。

    返回 (status, outputs)。status ∈ succeeded/failed/None（流断但没见终止帧）。
    """
    try:
        with requests.post(f"{DIFY}/workflows/run", headers={"Authorization": f"Bearer {APP_KEY}"},
                           json={"inputs": {"url": url, "part_items": part_items},
                                 "response_mode": "streaming", "user": "zcode-backfill"},
                           stream=True, timeout=(30, RUN_TIMEOUT_S)) as r:
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
                    return d.get("status") or "succeeded", d.get("outputs") or {}
                if ev == "error":
                    return "error", {"message": str(obj.get("message"))[:200]}
    except Exception as exc:
        return None, {"error": str(exc)[:200]}
    return None, {}  # 流结束但没收到 workflow_finished


def main() -> None:
    # 用法：backfill_collections.py [N M]        → 跑 PLAN 的第 N..M 段
    #       backfill_collections.py tail [N M]  → 跑 PLAN_TAIL（补跑计划）
    args = sys.argv[1:]
    plan_src = PLAN
    if args and args[0] == "tail":
        plan_src, args = PLAN_TAIL, args[1:]
    elif args and args[0] == "rewrite":
        plan_src, args = PLAN_REWRITE, args[1:]
    a = [int(x) for x in args if x.isdigit()]
    lo, hi = (a[0], a[1]) if len(a) >= 2 else ((a[0], a[0]) if a else (1, len(plan_src)))
    plan = plan_src[lo - 1:hi]
    log(f"=== 补跑开始：{len(plan)} 段（计划 {lo}..{hi}，来源 {'PLAN_TAIL' if plan_src is PLAN_TAIL else 'PLAN'}）===")
    ok = skipped = 0
    for i, (label, url, parts) in enumerate(plan, lo):
        log(f"--- [{i}/{len(plan_src)}] {label} · part_items={parts}")
        try:
            run_id, total, dedup = precreate(url, parts)
        except Exception as exc:
            log(f"    预建 run 失败：{str(exc)[:150]}；跳过")
            skipped += 1
            continue
        log(f"    run={run_id} 条目={total}{'（复用进行中 run）' if dedup else ''}")
        t0 = time.time()
        status, outputs = fire_stream(url, parts)
        n, tot = count_notes(run_id)
        used = int(time.time() - t0)
        detail = json.dumps({k: (str(v)[:60]) for k, v in outputs.items()}, ensure_ascii=False)[:300]
        if status == "succeeded":
            log(f"    ✅ 段完成：写盘 {n}/{tot} 篇（用时 {used}s）outputs={detail}")
            ok += 1
        else:
            log(f"    ⚠️ 段异常（workflow status={status}）：写盘 {n}/{tot} 篇，用时 {used}s，outputs={detail}")
            skipped += 1
    log(f"=== 补跑结束：完成 {ok} 段，未完成 {skipped} 段 ===")


if __name__ == "__main__":
    main()

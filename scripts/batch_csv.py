#!/usr/bin/env python3
"""按 CSV 模板批量跑「视频转文档」工作流（串行，避免并发触发风控）。

用法: batch_csv.py [csv路径] [--limit N] [--force]
- 从 CSV 第一列提取 URL（兼容抖音口令里嵌短链），归一化去重
- 串行逐条 POST workflows/run（streaming 等 workflow_finished），单条失败不阻断
- 进度落 logs/batch_csv_progress.json：崩了重跑自动续（ok 的跳过；fail 的重试）
- state 幂等仍在 serve 侧兜底：已处理条目进 run 即 skipped，秒过不重复烧
"""
import csv
import json
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, "/Users/linmintao/Desktop/开发项目/dify_kit")
from dify_kit.config import load_config  # noqa: E402

cfg = load_config()
DIFY = cfg["base_url"].rstrip("/")
KEY = cfg.get("app_key")

PROJ = Path(__file__).resolve().parent.parent
PROGRESS = PROJ / "logs" / "batch_csv_progress.json"
CSV_DEFAULT = "/Users/linmintao/Desktop/template32.csv"


def extract_urls(csv_path: str) -> list[str]:
    urls: list[str] = []
    with open(csv_path, encoding="utf-8") as f:
        r = csv.reader(f)
        next(r, None)  # 表头
        for row in r:
            if not row or not row[0].strip():
                continue
            cell = row[0].strip()
            m = re.search(r"https?://[^\s，,]+", cell)  # 抖音口令：抽出内嵌短链
            u = m.group(0) if m else cell
            if not u.startswith("http"):
                u = "https://" + u
            urls.append(u)
    seen, uniq = set(), []
    for u in urls:
        k = u.rstrip("/")
        if k not in seen:
            seen.add(k)
            uniq.append(u)
    return uniq


SERVE = "http://127.0.0.1:8790"


def verify_written(run_id: str) -> str:
    """回查 serve：每个非 skipped 条目都要有 note 产物。
    防「workflow succeeded 但单条目阶段失败没写盘」的假阳性（PARTIAL-FAIL 不打 failed）。"""
    if not run_id:
        return "无 run_id，无法回查"
    try:
        r = requests.get(f"{SERVE}/runs/{run_id}", timeout=15)
        d = r.json()
    except Exception as e:
        return f"回查异常: {str(e)[:120]}"
    bad = [f"条目{e.get('index')}(缺{','.join(s for s in ('download', 'transcribe', 'select', 'note')
                                                 if not (e.get('done') or {}).get(s))})"
           for e in d.get("entries", []) if not e.get("skipped")]
    bad = [b for b in bad if "缺" in b]
    return "" if not bad else "未写盘: " + "、".join(bad[:5])


def run_one(url: str, force: bool, part_items: str = "") -> dict:
    t0 = time.time()
    inputs = {"url": url}
    if part_items:
        inputs["part_items"] = part_items
    if force:
        inputs["force"] = "on"
    try:
        with requests.post(f"{DIFY}/workflows/run",
                           headers={"Authorization": f"Bearer {KEY}"},
                           json={"inputs": inputs, "response_mode": "streaming",
                                 "user": "zcode-batch-csv"},
                           stream=True, timeout=(30, 7200)) as r:
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
                    err = ((d.get("error") or "").strip().splitlines() or [""])[-1][:220]
                    if st == "succeeded":
                        v = verify_written(str((d.get("outputs") or {}).get("run_id") or ""))
                        if v:
                            return {"status": "fail", "elapsed": round(time.time() - t0),
                                    "error": v}
                    return {"status": "ok" if st == "succeeded" else "fail",
                            "elapsed": round(time.time() - t0), "error": err}
                if ev == "error":
                    return {"status": "fail", "elapsed": round(time.time() - t0),
                            "error": str(obj)[:220]}
    except Exception as e:
        return {"status": "fail", "elapsed": round(time.time() - t0),
                "error": f"调用异常: {str(e)[:200]}"}
    return {"status": "fail", "elapsed": round(time.time() - t0), "error": "流提前结束"}


WINDOW = 60  # 博主合集分窗：Dify loop 硬限 100 轮，note 循环每条目 1 轮 → 窗口必须 <100；
             # 60 同时压住 transcribe 轮询轮数（60 条×~75s / 60s 间隔 ≈ 75 轮 < 100）


def enumerate_total(url: str) -> int:
    """预飞：POST serve /runs 拿条目总数（枚举结果有 6h 磁盘缓存，重复调用不碰抖音页面）。

    注意会留下一个 part_items 空的全量 run 记录（status=created，永不驱动，24h 闲置自清）——
    换「不再本地解析、单一事实源在 serve」。
    """
    try:
        r = requests.post(f"{SERVE}/runs", json={"url": url}, timeout=(10, 280))
        d = r.json()
        return int(d.get("total") or 0)
    except Exception:
        return 0


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    csv_path = args[0] if args else CSV_DEFAULT
    limit = 0
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    force = "--force" in sys.argv

    urls = extract_urls(csv_path)
    if limit:
        urls = urls[:limit]
    prog = json.loads(PROGRESS.read_text()) if PROGRESS.exists() else {}
    todo = [u for u in urls if prog.get(u.rstrip("/"), {}).get("status") != "ok"]
    print(f"共 {len(urls)} 条（去重后），已完成 {len(urls) - len(todo)}，本轮待跑 {len(todo)}", flush=True)

    t_all = time.time()
    for i, u in enumerate(todo, 1):
        # 大合集分窗：note 循环每条目占 1 轮 loop（Dify 硬限 100），>100 条不分窗必败在第 100 条
        total = enumerate_total(u)
        windows = [""]
        if total > WINDOW:
            windows = [f"{a}-{min(a + WINDOW - 1, total)}" for a in range(1, total + 1, WINDOW)]
        wkey = lambda w: f"{u.rstrip('/')}#{w or 'all'}"
        todo_w = [w for w in windows
                  if prog.get(wkey(w), {}).get("status") != "ok"]
        if not todo_w:
            prog[u.rstrip("/")] = {"status": "ok", "elapsed": 0,
                                   "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "windows": "all"}
            PROGRESS.write_text(json.dumps(prog, ensure_ascii=False, indent=1))
            continue
        print(f"[{time.strftime('%H:%M:%S')}] ▶ [{i}/{len(todo)}] {u}"
              + (f"（{total} 条 → {len(windows)} 窗，待跑 {len(todo_w)} 窗）" if total > WINDOW else ""),
              flush=True)
        u_ok = True
        for w in todo_w:
            tag = f"窗 {w}" if w else "单条"
            print(f"[{time.strftime('%H:%M:%S')}]   ▶ {tag}", flush=True)
            res = run_one(u, force, part_items=w)
            res["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            prog[wkey(w)] = res
            PROGRESS.write_text(json.dumps(prog, ensure_ascii=False, indent=1))
            mark = "✅" if res["status"] == "ok" else "❌"
            print(f"[{time.strftime('%H:%M:%S')}]   {mark} {tag} 用时 {res['elapsed']}s"
                  + (f" · {res['error']}" if res.get("error") else ""), flush=True)
            if res["status"] != "ok":
                u_ok = False
                break  # 同一条目的后续窗口不再连烧；留待下次重跑（断点续跑）
        prog[u.rstrip("/")] = {"status": "ok" if u_ok else "fail",
                               "elapsed": sum(prog.get(wkey(w), {}).get("elapsed", 0) for w in windows),
                               "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "windows": f"{sum(1 for w in windows if prog.get(wkey(w), {}).get('status') == 'ok')}/{len(windows)}"}
        PROGRESS.write_text(json.dumps(prog, ensure_ascii=False, indent=1))

    n_ok = sum(1 for u in urls if prog.get(u.rstrip("/"), {}).get("status") == "ok")
    fails = [(u, prog[u.rstrip("/")].get("error", "")) for u in urls
             if prog.get(u.rstrip("/"), {}).get("status") != "ok"]
    print(f"\n==== 汇总（总用时 {(time.time() - t_all) / 60:.0f}min）====", flush=True)
    print(f"✅ {n_ok}/{len(urls)}", flush=True)
    for u, e in fails:
        print(f"❌ {u} · {e[:120]}", flush=True)


if __name__ == "__main__":
    main()

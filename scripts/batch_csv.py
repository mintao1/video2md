#!/usr/bin/env python3
"""按 CSV 模板批量跑视频转文档（LangGraph API 版，串行避免并发触发风控）。

用法: batch_csv.py [csv路径] [--limit N] [--force] [--api http://127.0.0.1:8790]
- 从 CSV 第一列提取 URL（兼容抖音口令里嵌短链），归一化去重
- 逐条 POST /runs（同步解析即知 total）→ 轮询 GET /runs/{id} 到终态；单条失败不阻断
- 进度落 logs/batch_csv_progress.json：崩了重跑自动续（ok 的跳过；fail 的重试）
- state 幂等在服务端兜底：已处理条目建 run 即 skipped，秒过不重复烧
- 大合集分窗（part_items）保留：不再是 Dify loop 100 硬限（LangGraph 无此限制），
  窗口只为控制单 run 爆炸半径与进度粒度
"""
import csv
import json
import re
import sys
import time
from pathlib import Path

import httpx

PROJ = Path(__file__).resolve().parent.parent
PROGRESS = PROJ / "logs" / "batch_csv_progress.json"
CSV_DEFAULT = "/Users/linmintao/Desktop/template32.csv"



def _api_base() -> str:
    if "--api" in sys.argv:
        return sys.argv[sys.argv.index("--api") + 1].rstrip("/")
    import os
    return (os.environ.get("V2MD_API") or "http://127.0.0.1:8790").rstrip("/")


API = _api_base()

POLL_INTERVAL = 30       # 终态轮询间隔（秒）
POLL_DEADLINE = 4 * 3600  # 单 run 轮询上限（合集+校对可以很久）



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



def verify_written(d: dict) -> str:
    """每个非 skipped 条目都要有 note 产物——防「run done 但单条目没写盘」的假阳性
    （PARTIAL-FAIL 不打 error）。数据就在 GET /runs/{id} 响应里，不用再回查第二次。
    只以 note 为准：clips 是可选阶段（不开切片时 done.clips 恒 False），download 等
    中间态在部分失败场景本就缺失，逐段报只会制造噪音误判。"""
    bad = [f"条目{e.get('index')}" for e in d.get("entries", [])
           if not e.get("skipped") and not (e.get("done") or {}).get("note")]
    return "" if not bad else "未写盘: " + "、".join(bad[:5])



def run_one(url: str, force: bool, part_items: str = "") -> dict:
    t0 = time.time()
    payload: dict = {"url": url, "force": force}
    if part_items:
        payload["part_items"] = part_items
    try:
        r = httpx.post(f"{API}/runs", json=payload, timeout=httpx.Timeout(30.0, read=300.0))
        d = r.json()
        if r.status_code != 200:
            return {"status": "fail", "elapsed": round(time.time() - t0),
                    "error": f"建 run HTTP {r.status_code}: {str(d)[:200]}"}
    except Exception as e:
        return {"status": "fail", "elapsed": round(time.time() - t0),
                "error": f"调用异常: {str(e)[:200]}"}
    run_id = d.get("run_id", "")
    if d.get("all_skipped"):
        return {"status": "ok", "elapsed": round(time.time() - t0), "error": ""}
    deadline = time.time() + POLL_DEADLINE
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            d = httpx.get(f"{API}/runs/{run_id}", timeout=30).json()
        except Exception:
            continue  # 单次轮询抖动不判死
        st = d.get("status") or (d.get("task") or {}).get("status") or ""
        if st in ("done", "partial", "error"):
            v = verify_written(d)
            err = v or "；".join((d.get("task") or {}).get("errors") or [])[:220]
            ok = st == "done" and not v
            return {"status": "ok" if ok else "fail",
                    "elapsed": round(time.time() - t0), "error": err}
    return {"status": "fail", "elapsed": round(time.time() - t0), "error": "轮询超时"}



WINDOW = 100  # 合集分窗：仅控爆炸半径/进度粒度（Dify loop 硬限已随编排抽离消除）



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
        # 预飞拿 total 决定分窗：POST /runs 同步解析即回（不再留「永不驱动」的空 run——
        # 建出的 run 就是本次要跑的 run，part_items 空窗即全量）
        probe = run_one_probe(u)
        total = probe or 0
        windows = [""]
        if total > WINDOW:
            windows = [f"{a}-{min(a + WINDOW - 1, total)}" for a in range(1, total + 1, WINDOW)]
        def wkey(w: str) -> str:
            return f"{u.rstrip('/')}#{w or 'all'}"
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



def run_one_probe(url: str) -> int:
    """预飞：POST /runs 建 run 拿 total。注意这个 run 就是待会要跑的 run
    （dedup 语义保证同 URL+part_items 复用，不会建出两份）。"""
    try:
        d = httpx.post(f"{API}/runs", json={"url": url, "resolve_only": True},
                       timeout=httpx.Timeout(30.0, read=300.0)).json()
        return int(d.get("total") or 0)
    except Exception:
        return 0



if __name__ == "__main__":
    main()

"""入口：serve / graph-run / __stage / runs 四命令。"""
from __future__ import annotations

import argparse
import json
import sys

from .config import ENV
from .runs import _load_runs, cmd_stage
from .serve import cmd_serve



def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        ap = argparse.ArgumentParser(prog="video2md serve")
        ap.add_argument("--port", type=int, default=int(ENV.get("V2MD_PORT", "8790")))
        a = ap.parse_args(sys.argv[2:])
        cmd_serve(a.port)
        return
    if len(sys.argv) > 1 and sys.argv[1] == "graph-run":
        # LangGraph 单次直跑（调试/对拍用；生产入口是 api 服务的 POST /runs）
        from .graph import make_checkpointer, run_graph
        ap = argparse.ArgumentParser(prog="video2md graph-run")
        ap.add_argument("url")
        ap.add_argument("--part-items", default="")
        ap.add_argument("--force", action="store_true")
        ap.add_argument("--review", action="store_true")
        ap.add_argument("--max-frames", type=int, default=60)
        a = ap.parse_args(sys.argv[2:])
        saver = make_checkpointer()
        final = run_graph(a.url, part_items=a.part_items, force=a.force,
                          review=a.review, max_frames=a.max_frames, checkpointer=saver)
        run = final.get("run") or {}
        notes = [a2.get("note") for a2 in (run.get("artifacts") or {}).values() if a2.get("note")]
        print(json.dumps({"run_id": final.get("run_id"), "status": final.get("status"),
                          "notes": notes, "errors": (final.get("errors") or [])[-5:]},
                         ensure_ascii=False, indent=1))
        sys.exit(0 if final.get("status") in ("done", "partial") else 1)
    if len(sys.argv) > 1 and sys.argv[1] == "__stage":
        # 内部入口：serve 的阶段 job 子进程（worker 以 `__stage <run> <stage> <args-json>` 拉起）
        ap = argparse.ArgumentParser(prog="video2md __stage")
        ap.add_argument("run_id")
        ap.add_argument("stage")
        ap.add_argument("args_json", default="{}", nargs="?")
        a = ap.parse_args(sys.argv[2:])
        cmd_stage(a.run_id, a.stage, a.args_json)
        return
    if len(sys.argv) > 1 and sys.argv[1] == "runs":
        # 排障工具：runs [run_id] 查看 run 状态
        ap = argparse.ArgumentParser(prog="video2md runs")
        ap.add_argument("run_id", default="", nargs="?")
        a = ap.parse_args(sys.argv[2:])
        runs = _load_runs()
        if a.run_id:
            r = runs.get(a.run_id)
            if not r:
                sys.exit(f"❌ run {a.run_id} 不存在")
            print(json.dumps(r, ensure_ascii=False, indent=1)[:4000])
        else:
            for rid, r in sorted(runs.items(), key=lambda kv: kv[1].get("created", ""), reverse=True)[:10]:
                print(f"{rid}  {r.get('created', '')}  {r.get('status', '')}  "
                      f"条目 {r.get('total', 0)}  url={r.get('url', '')[:60]}")
        return
    sys.exit("用法：video2md serve [--port 8790] | graph-run <url> [--force] [--review] | runs [run_id]")



if __name__ == "__main__":
    main()

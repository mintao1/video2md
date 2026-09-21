"""入口：serve / __stage / runs 三命令。"""
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
    sys.exit("用法：video2md serve [--port 8790] | runs [run_id]"
             "（管线由 Dify workflow「视频转文档」驱动，已删除本地 CLI 直跑路径）")



if __name__ == "__main__":
    main()

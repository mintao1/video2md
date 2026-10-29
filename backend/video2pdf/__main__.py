"""入口：api / graph-run / __stage / runs 四命令（serve 已随 Dify 抽离归档 legacy/）。"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from .config import ENV
from .runs import cmd_stage


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "api":
        # LangGraph 内嵌服务（FastAPI）——Dify 抽离后的生产入口
        from .api import cmd_api
        ap = argparse.ArgumentParser(prog="video2pdf api")
        ap.add_argument("--port", type=int, default=int(ENV.get("V2PDF_PORT", "8790")))
        a = ap.parse_args(sys.argv[2:])
        cmd_api(a.port)
        return
    if len(sys.argv) > 1 and sys.argv[1] == "migrate":
        # schema 迁移（P0-5）：应用待迁移版本后校验，幂等可重跑；api 建池即校验，
        # 落后拒起等本命令。json 档（无 DSN）exit 0——deploy.sh 对 json 部署照常走。
        from . import pg
        from .pgmigrate import SCHEMA_LATEST, apply_migrations, validate_schema
        if not pg.pg_enabled():
            print("· V2PDF_PG_DSN 未配置（json 档）：无 schema 可迁移")
            return
        # Migrations are the sole component allowed to use the DDL-capable
        # role.  The API DSN is intentionally restricted to tenant-scoped DML.
        pool = pg.make_pool(pg.pg_migrate_dsn())
        try:
            applied = apply_migrations(pool)
            print(f"✅ 迁移就绪：库版本 {SCHEMA_LATEST}"
                  + (f"（本次新应用：{applied}）" if applied else "（无待应用，幂等重跑）"))
            validate_schema(pool)
        finally:
            pool.close()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "graph-run":
        # LangGraph 单次直跑（调试/对拍用；生产入口是 api 服务的 POST /runs）
        from .graph import make_checkpointer_from_env, run_graph
        ap = argparse.ArgumentParser(prog="video2pdf graph-run")
        ap.add_argument("url")
        ap.add_argument("--part-items", default="")
        ap.add_argument("--force", action="store_true")
        ap.add_argument("--review", action="store_true")
        ap.add_argument("--max-frames", type=int, default=60)
        a = ap.parse_args(sys.argv[2:])
        saver, closer = make_checkpointer_from_env()  # 默认 sqlite；V2PDF_CHECKPOINTER=postgres 切库
        try:
            final = run_graph(a.url, part_items=a.part_items, force=a.force,
                              review=a.review, max_frames=a.max_frames, checkpointer=saver)
        finally:
            closer()
        run = final.get("run") or {}
        notes = [a2.get("note") for a2 in (run.get("artifacts") or {}).values() if a2.get("note")]
        print(json.dumps({"run_id": final.get("run_id"), "status": final.get("status"),
                          "notes": notes, "errors": (final.get("errors") or [])[-5:]},
                         ensure_ascii=False, indent=1))
        sys.exit(0 if final.get("status") in ("done", "partial") else 1)
    if len(sys.argv) > 1 and sys.argv[1] == "__stage":
        # 单阶段手动排障入口：`__stage <run_id> <stage> '<args JSON>'` 重放一个阶段（详见 runs.cmd_stage）
        ap = argparse.ArgumentParser(prog="video2pdf __stage")
        ap.add_argument("run_id")
        ap.add_argument("stage")
        ap.add_argument("args_json", default="{}", nargs="?")
        a = ap.parse_args(sys.argv[2:])
        cmd_stage(a.run_id, a.stage, a.args_json)
        return
    if len(sys.argv) > 1 and sys.argv[1] == "cost":
        # 成本报表导出（P2-2 验收：按 uid 导出）：`video2pdf cost [--uid X] [--month YYYY-MM]`
        # 逐 run 一行 JSON（run_id/created/status/cost_usd/usage）+ 末行汇总；uid 缺省=全用户。
        from .runs import _load_runs
        ap = argparse.ArgumentParser(prog="video2pdf cost")
        ap.add_argument("--uid", default="")
        ap.add_argument("--month", default=datetime.now().strftime("%Y-%m"))
        a = ap.parse_args(sys.argv[2:])
        since = f"{a.month}-01T00:00:00"
        rows = [r for r in _load_runs().values()
                if (not a.uid or r.get("owner") == a.uid)
                and (r.get("created") or "") >= since]
        rows.sort(key=lambda r: r.get("created") or "")
        agg = {"llm_in_tok": 0, "llm_out_tok": 0, "gpu_s": 0.0, "spend_usd": 0.0, "runs_n": 0}
        for r in rows:
            u = r.get("usage") or {}
            agg["llm_in_tok"] += int(u.get("llm_in_tok") or 0)
            agg["llm_out_tok"] += int(u.get("llm_out_tok") or 0)
            agg["gpu_s"] += float(u.get("gpu_s") or 0)
            agg["spend_usd"] += float(r.get("cost_usd") or 0)
            agg["runs_n"] += 1
            print(json.dumps({"run_id": r.get("run_id"), "created": r.get("created"),
                              "status": r.get("status"), "cost_usd": r.get("cost_usd"),
                              "usage": u}, ensure_ascii=False))
        agg["spend_usd"] = round(agg["spend_usd"], 6)
        print(json.dumps({"summary": True, "uid": a.uid or "(all)", "month": a.month, **agg},
                         ensure_ascii=False))
        return
    if len(sys.argv) > 1 and sys.argv[1] == "runs":
        # 排障工具：runs [run_id] 查看 run 状态
        ap = argparse.ArgumentParser(prog="video2pdf runs")
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
    sys.exit("用法：video2pdf api [--port 8790] | migrate | graph-run <url> [--force] [--review] | runs [run_id]"
             "（__stage 为单阶段手动排障入口；Dify 时代的 serve 已归档 legacy/serve.py）")



if __name__ == "__main__":
    main()

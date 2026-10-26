#!/usr/bin/env python3
"""json 文件档 → PG 档一次性迁移（幂等 upsert，可重跑）。

前提：V2PDF_PG_DSN 已配（secrets/video2pdf.env 或环境变量）；源文件按
V2PDF_RUNS_FILE / V2PDF_STATE_FILE / V2PDF_USERS_FILE 解析（缺省项目根三件套）。

用法（容器内/项目根均可）：
  python backend/scripts/migrate_json_to_pg.py                     # 全量 runs+state+users
  python backend/scripts/migrate_json_to_pg.py --only runs,state
  python backend/scripts/migrate_json_to_pg.py --dry-run

迁移完成后：secrets 里把 V2PDF_RUNS_STORE/V2PDF_STATE_STORE/V2PDF_USERS_STORE 切
postgres（+ 可选 V2PDF_EVENTS=pg / V2PDF_CHECKPOINTER=postgres），重启服务即切换。
反向（PG → json）不提供——PG 档是升级方向。
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目外跑（镜像 /app）兜底

from video2pdf import pg  # noqa: E402
from video2pdf.auth.pg_store import bulk_load  # noqa: E402
from video2pdf.config import ENV  # noqa: E402
from video2pdf.pgstore import PostgresRunsStore, PostgresStateStore  # noqa: E402


def _read_json(env_key: str, default: str) -> dict:
    p = Path(ENV.get(env_key, default))
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        sys.exit(f"✗ {p} 解析失败：{exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description="json 文件档 → PG 档迁移（幂等）")
    ap.add_argument("--only", default="runs,state,users",
                    help="逗号分隔子集：runs,state,users（缺省全量）")
    ap.add_argument("--dry-run", action="store_true", help="只计数不写库")
    a = ap.parse_args()
    only = {s.strip() for s in a.only.split(",") if s.strip()}
    unknown = only - {"runs", "state", "users"}
    if unknown:
        sys.exit(f"✗ --only 未知项：{sorted(unknown)}（支持 runs,state,users）")
    if not pg.pg_enabled():
        sys.exit("✗ V2PDF_PG_DSN 未配置（secrets/video2pdf.env 或环境变量）")

    pool = pg.make_pool(pg.pg_dsn())  # 独立池：先应用迁移基线（P0-5，store 构造不再隐式建表）
    try:
        from video2pdf.pgmigrate import apply_migrations
        apply_migrations(pool)
        if "runs" in only:
            src = _read_json("V2PDF_RUNS_FILE", "runs.json")
            print(f"runs : {len(src)} 条" + ("（dry-run 不写）" if a.dry_run else ""))
            if src and not a.dry_run:
                st = PostgresRunsStore(pool)
                for run in src.values():
                    st.merge(run)
        if "state" in only:
            src = _read_json("V2PDF_STATE_FILE", "state.json")
            print(f"state: {len(src)} 条幂等记录" + ("（dry-run 不写）" if a.dry_run else ""))
            if src and not a.dry_run:
                PostgresStateStore(pool).save(src)
        if "users" in only:
            src = _read_json("V2PDF_USERS_FILE", "users.json")
            print(f"users: {len(src)} 个账号" + ("（dry-run 不写）" if a.dry_run else ""))
            if src and not a.dry_run:
                n_users, n_idents = bulk_load(pool, src)
                print(f"      identities 展开 {n_idents} 条（总账号 {n_users}）")
        print("✅ 迁移完成" if not a.dry_run else "· dry-run 完成")
        return 0
    finally:
        pool.close()


if __name__ == "__main__":
    sys.exit(main())

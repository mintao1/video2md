#!/usr/bin/env python3
"""会员/配额展示档种子（幂等 upsert，可重跑）——**仅 dev/test 环境手动执行，勿在生产跑**。

前提：V2MD_PG_DSN 已配且迁移已应用（`python -m video2md migrate`）。
数据对齐 web 前端 mock 契约（frontend/web/src/api/mock/data.ts）：
  林思齐 (Siyi Lin) · pro · AI 38/50 小时 · 存储 4.2/20 GB · 统计 47/182/94.8

用法（backend 目录或项目根均可）：
  V2MD_PG_DSN=postgresql://localhost:5432/v2pdf uv run --project backend python backend/scripts/seed_membership.py
  python backend/scripts/seed_membership.py --uid u_xxx          # 指定别的 uid

默认 uid 是本地 dev 档（V2MD_AUTH=off + dev provider）的账号 u_9db31e487173——
种子后 web 个人页即显示该测试人员资料。种子不进迁移：0002 会应用到生产，
测试数据独立脚本按环境手动落。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目外跑（镜像 /app）兜底

from video2md import pg  # noqa: E402
from video2md.pgstore import PostgresMembershipStore  # noqa: E402

# 测试人员（对齐前端 mock：frontend/web/src/api/mock/data.ts mockUser + mockQuota）
SEED = {
    "plan": "pro",
    "display_name": "林思齐 (Siyi Lin)",
    "avatar_hue": 258,
    "ai_hours_used": 38,
    "ai_hours_total": 50,
    "storage_gb_used": 4.2,
    "storage_gb_total": 20,
    "stats_books": 47,
    "stats_knowledge_points": 182,
    "stats_accuracy": 94.8,
    "record": {"seeded_by": "backend/scripts/seed_membership.py", "note": "dev/test 测试人员"},
}


def main() -> int:
    ap = argparse.ArgumentParser(description="membership 展示档种子（幂等，仅 dev/test）")
    ap.add_argument("--uid", default="u_9db31e487173",
                    help="目标 uid（缺省=本地 dev 档账号，users.json 里 dev provider 的映射）")
    a = ap.parse_args()
    if not pg.pg_enabled():
        sys.exit("✗ V2MD_PG_DSN 未配置（secrets/video2md.env 或环境变量）")

    pool = pg.make_pool(pg.pg_dsn())  # 独立池：先应用迁移基线（P0-5，store 构造不再隐式建表）
    try:
        from video2md.pgmigrate import apply_migrations
        apply_migrations(pool)
        st = PostgresMembershipStore(pool)
        st.upsert({"uid": a.uid, **SEED})
        row = st.get(a.uid)
        print(f"✓ membership[{a.uid}] = {row['plan']} · {row['display_name']} · "
              f"AI {row['ai_hours_used']}/{row['ai_hours_total']}h · "
              f"存储 {row['storage_gb_used']}/{row['storage_gb_total']}GB · "
              f"统计 {row['stats_books']}/{row['stats_knowledge_points']}/{row['stats_accuracy']}")
    finally:
        pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

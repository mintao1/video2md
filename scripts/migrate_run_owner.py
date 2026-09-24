#!/usr/bin/env python3
"""一次性运维：把 runs.json 里指定 run 集合的 owner 改到目标 uid（带备份）。

场景：AUTH=off/vault 时代的全量批次都挂在 dev uid 名下；切 deliver+google_device 后
插件只列/领自己 uid 名下的 run——存量产物要收编给某个真实身份才拿得到。

用法（容器内跑，指到 data 卷的 runs.json）:
  python scripts/migrate_run_owner.py --to <uid> [--from <uid>] [--runs r1,r2 | --status done] [--dry-run]
- --to 必填：目标 owner uid（users.json 里先有身份可 `--show-users` 核对）
- --from 缺省 = 所有非目标 uid 的来源；给了则只迁该 uid 的
- --runs 显式 run_id 清单，或 --status 按状态筛（两者都不给 = 全量）
- 写前自动备份 runs.json → runs.json.bak-migrate-<ts>；锁内读-改-写（与 API 进程共锁文件）
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from video2md import runs  # noqa: E402

if (runs.ENV.get("V2MD_RUNS_STORE") or "").strip().lower() == "postgres":
    sys.exit("本工具面向 JSON 文件档（runs.json）；V2MD_RUNS_STORE=postgres 下数据在 PG——"
             "owner 收编用 SQL（UPDATE v2md_runs SET record = jsonb_set(...)）处理")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--to", required=True, help="目标 owner uid")
    ap.add_argument("--from", dest="frm", default="", help="只迁该来源 uid（缺省不限）")
    ap.add_argument("--runs", default="", help="逗号分隔 run_id 清单（优先于 --status）")
    ap.add_argument("--status", default="", help="按状态筛：done|error|partial|...")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show-users", action="store_true", help="打印 users.json 身份表后退出")
    a = ap.parse_args()

    if a.show_users:
        uf = Path(runs.ENV.get("V2MD_USERS_FILE", "users.json"))
        print(json.dumps(json.loads(uf.read_text()) if uf.exists() else {},
                         ensure_ascii=False, indent=1))
        return 0

    want = {r.strip() for r in a.runs.split(",") if r.strip()}
    with runs._runs_file_lock():
        db = runs._load_runs()
        hit = []
        for rid, rec in db.items():
            if want and rid not in want:
                continue
            if a.status and rec.get("status") != a.status:
                continue
            if a.frm and (rec.get("owner", "") or "") != a.frm:
                continue
            if (rec.get("owner", "") or "") == a.to:
                continue
            hit.append((rid, rec.get("owner", ""), rec.get("status"), str(rec.get("url", ""))[:60]))
        print(f"命中 {len(hit)} 条 → owner 改为 {a.to}")
        for h in hit[:50]:
            print("  ", *h)
        if not hit or a.dry_run:
            return 0
        bak = runs.RUNS_FILE.with_name(f"runs.json.bak-migrate-{time.strftime('%Y%m%d%H%M%S')}")
        shutil.copy2(runs.RUNS_FILE, bak)
        print(f"已备份 → {bak}")
        for rid, *_ in hit:
            db[rid]["owner"] = a.to
        runs._save_runs(db)
        print("✅ 迁移完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())

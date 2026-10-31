"""runs / state / tasks 的 Postgres 适配器（P3/P4b 存储分派 PG 档的实现体）。

设计口径：与 JSON 文件档**函数语义一一对应**——
- PostgresRunsStore.load() 返回 {run_id: run_dict}（dedup / 清理器的全表扫描语义保留）；
  merge(run) = 行级 upsert（多实例并发安全，替代文件档「fcntl 锁内读改写」）。
  无全表替换（文件档 _save_runs 的 save_all 语义在多实例下会覆盖他进程写入，PG 档禁用）。
- PostgresStateStore.save() 把「全量替换」降为逐 fp upsert——state 键集只增不删
  （重跑覆盖同 fp），语义等价。

表名带 v2pdf_ 前缀（与 langgraph checkpointer 的 checkpoints* 表同库不撞）；
owner/status/created 提取列供 PostgresTaskStore 的队列查询（P4b）与运维 SQL 用，
真相仍是 record jsonb——merge() 整行覆盖 record+status，两态天然一致。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from psycopg.types.json import Json

from .pg import tenant_connection

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

# watchdog 巡检时钟域纪律（P1-4 踩坑实证）：record 里的阶段 ts 是应用写的**无时区本地
# 时间串**，PG（容器会话常为 UTC）cast 成 timestamptz 会偏到未来数小时——SQL 侧任何
# GREATEST(阶段ts, ...) 判超龄对带阶段戳的行恒假（running 残档检测整体静默失效）。
# 故 DB 侧只按 **DB 自己写的 updated** 过滤；阶段 ts 回应用侧解析（_stage_ts_epoch）。
from datetime import datetime as _dt


def _stage_ts_epoch(record: dict) -> float:
    """record->'stages' 各 .ts（应用侧 naive 本地串）的最大 epoch；无阶段返 0。"""
    last = 0.0
    for s in (record.get("stages") or {}).values():
        ts = (s or {}).get("ts")
        if not ts:
            continue
        try:
            last = max(last, _dt.fromisoformat(str(ts)).timestamp())
        except (ValueError, TypeError, OverflowError):
            continue
    return last


class PostgresRunsStore:
    """runs 表：run_id PK + 提取列 + record jsonb（整条 run dict）。"""

    def __init__(self, pool: ConnectionPool):
        self.pool = pool  # 建表已上收 migrations/0001（P0-5），构造只接线

    def load(self) -> dict:
        with tenant_connection(self.pool) as conn:
            rows = conn.execute("SELECT record FROM v2pdf_runs").fetchall()
        out: dict = {}
        for (rec,) in rows:
            out[rec["run_id"]] = rec
        return out

    def delete_terminal(self, run_id: str, owner: str) -> bool:
        with tenant_connection(self.pool) as conn:
            row = conn.execute("""
                UPDATE v2pdf_runs SET record=record || '{"deleted":true}'::jsonb, updated=now()
                WHERE run_id=%s AND owner=%s AND record->>'deleted' IS DISTINCT FROM 'true'
                AND status IN ('done', 'partial', 'error') RETURNING run_id
                """, (run_id, owner)).fetchone()
        return row is not None

    def patch_owned(self, run_id: str, owner: str, statuses: tuple[str, ...], patch: dict) -> dict | None:
        with tenant_connection(self.pool) as conn:
            row = conn.execute("""
                UPDATE v2pdf_runs SET record = record || %s::jsonb, status = COALESCE(%s, status), updated = now()
                WHERE run_id=%s AND owner=%s AND record->>'deleted' IS DISTINCT FROM 'true'
                AND status = ANY(%s) RETURNING record
                """, (Json(patch), patch.get("status"), run_id, owner, list(statuses))).fetchone()
        return row[0] if row else None

    def merge(self, run: dict) -> None:
        """行级 upsert（原子）。调用方无需持锁——文件档的锁纪律在此内化为行锁。"""
        with tenant_connection(self.pool) as conn:
            conn.execute("""
                INSERT INTO v2pdf_runs (run_id, owner, status, created, record)
                VALUES (%(run_id)s, %(owner)s, %(status)s, %(created)s, %(rec)s)
                ON CONFLICT (run_id) DO UPDATE SET
                    owner = EXCLUDED.owner,
                    status = EXCLUDED.status,
                    created = EXCLUDED.created,
                    record = CASE WHEN v2pdf_runs.record->>'deleted'='true'
                        THEN EXCLUDED.record || '{"deleted":true}'::jsonb ELSE EXCLUDED.record END,
                    updated = now()""",
                {"run_id": run.get("run_id", ""),
                 "owner": run.get("owner", "") or "",
                 "status": run.get("status", "") or "",
                 "created": run.get("created", "") or "",
                 "rec": Json(run)})


class PostgresStateStore:
    """state 表：(owner, fp) PK + record jsonb（租户级幂等记录）。

    文件档的 fp 是外层 dict key（不进记录体），故表单存 fp 列。"""

    def __init__(self, pool: ConnectionPool):
        self.pool = pool  # 建表已上收 migrations/0001（P0-5），构造只接线

    def load(self) -> dict:
        from .pg import current_tenant, tenant_connection
        with tenant_connection(self.pool) as conn:
            # A concrete tenant is set by graph.run_graph through pg.tenant_scope.
            # Keep the unscoped form for CLI/eval callers and migration tooling.
            owner = current_tenant()
            if owner is None:
                rows = conn.execute("SELECT fp, record FROM v2pdf_state").fetchall()
            else:
                rows = conn.execute("SELECT fp, record FROM v2pdf_state WHERE owner=%s",
                                    (owner,)).fetchall()
        return {fp: rec for fp, rec in rows}

    def save(self, s: dict) -> None:
        """逐 fp upsert（键只增不删 → 与文件档全量替换语义等价）。"""
        from .pg import current_tenant, tenant_connection
        with tenant_connection(self.pool) as conn:
            tenant = current_tenant()
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO v2pdf_state (owner, fp, record) VALUES (%s, %s, %s)
                    ON CONFLICT (owner, fp) DO UPDATE SET record = EXCLUDED.record, updated = now()
                    """, [((tenant if tenant is not None else (rec.get("owner") or "")),
                           fp, Json(rec)) for fp, rec in s.items()])


class PostgresTaskStore:
    """任务表 PG 真相源（P4b·多副本档）——只管「读侧 + 认领」，不碰状态写入。

    口径：record jsonb 是状态唯一真相，v2pdf_runs.status 提取列是其可查镜像
    （merge 整行覆盖两态天然一致）。本类方法全是纯 SQL 查询/单语句原子迁移：
    - counts/active_for/total_active/is_alive → /queue、配额闸、dedup 判活
    - recover_candidates → 启动恢复认领：FOR UPDATE SKIP LOCKED，claim=行锁本身，
      双起竞争败者跳过、胜者重投后由 _drive 的 merge 自然刷 claimed_by
    - stale_running/steal_if_unclaimed/heartbeat → watchdog DB 巡检与崩溃残档接管
    - submit_window_allowed → 跨进程滑窗限流（v2pdf_submit_log 单语句清旧+计数+判定）
    """

    def __init__(self, pool: ConnectionPool):
        self.pool = pool  # v2pdf_runs·claimed_by·v2pdf_submit_log 建表已上收 migrations/0001（P0-5）

    # ---------------------------------------------------------------- 队列视图

    def counts(self) -> dict:
        """{queued, running} 全盘计数（status 索引扫描，仅两态）。"""
        with tenant_connection(self.pool) as conn:
            rows = conn.execute(
                "SELECT status, count(*) FROM v2pdf_runs "
                "WHERE status IN ('queued','running') GROUP BY status").fetchall()
        c = {"queued": 0, "running": 0}
        for st, n in rows:
            c[st] = int(n)
        return c

    def total_active(self) -> int:
        with tenant_connection(self.pool) as conn:
            row = conn.execute(
                "SELECT count(*) FROM v2pdf_runs WHERE status IN ('queued','running')").fetchone()
        return int(row[0]) if row else 0

    def active_for(self, uid: str) -> int:
        with tenant_connection(self.pool) as conn:
            row = conn.execute(
                "SELECT count(*) FROM v2pdf_runs "
                "WHERE status IN ('queued','running') AND owner=%s", (uid,)).fetchone()
        return int(row[0]) if row else 0

    def is_alive(self, run_id: str) -> bool:
        """dedup 判活：DB 视角该 run 是否仍 queued/running（他进程驱动也算活）。"""
        with tenant_connection(self.pool) as conn:
            row = conn.execute(
                "SELECT 1 FROM v2pdf_runs WHERE run_id=%s "
                "AND status IN ('queued','running')", (run_id,)).fetchone()
        return row is not None

    def get_status_url(self, run_id: str) -> tuple[str | None, str | None]:
        """SSE 收尾判定的 DB 兜底：两行内存皆 miss 时现读 (status, url)。"""
        with tenant_connection(self.pool) as conn:
            row = conn.execute(
                "SELECT status, record->>'url' FROM v2pdf_runs WHERE run_id=%s",
                (run_id,)).fetchone()
        return (row[0], row[1]) if row else (None, None)

    # ---------------------------------------------------------------- 心跳 / 活性

    def heartbeat(self, run_id: str) -> bool:
        """阶段循环头打点：刷新 updated 作跨进程活性信号。终态不续（返回 False）。

        P1-1：queued 态也续——排队等待信号量期间由 api._queue_hb_loop 周期打点，
        「排队久」（健康）与「无主停滞」（属主死）靠 updated 新鲜度区分。
        """
        with tenant_connection(self.pool) as conn:
            row = conn.execute(
                "UPDATE v2pdf_runs SET updated=now() WHERE run_id=%s "
                "AND status IN ('queued','running') RETURNING 1", (run_id,)).fetchone()
        return row is not None

    def mark_queued(self, run_id: str, proc: str) -> bool:
        """入队即落库+钉名（P1-1）：status created→queued、claimed_by 空则钉（已有主不动）。

        直接 UPDATE 不走 merge——merge 整行覆盖会把 claimed_by 刷回默认 ''。
        返回是否命中（行不存在/已 running/终态 → False，调用方忽略）。
        """
        with tenant_connection(self.pool) as conn:
            row = conn.execute("""
                UPDATE v2pdf_runs
                   SET status='queued', updated=now(),
                       claimed_by = CASE WHEN claimed_by='' THEN %(proc)s ELSE claimed_by END
                 WHERE run_id=%(rid)s AND status IN ('created','queued')
                RETURNING 1""", {"proc": proc, "rid": run_id}).fetchone()
        return row is not None

    def still_mine(self, run_id: str, proc: str) -> bool:
        """持有权复核（P1-1「不重」半边）：_drive 拿到信号量后先问——租约被大盘
        watchdog 划给别人（或已终态）则 False，调用方静默让位不驱动。"""
        with tenant_connection(self.pool) as conn:
            row = conn.execute(
                "SELECT 1 FROM v2pdf_runs WHERE run_id=%s AND claimed_by=%s "
                "AND status IN ('queued','running')", (run_id, proc)).fetchone()
        return row is not None

    def note_claimed_by(self, run_id: str, proc: str) -> None:
        """best-effort 登记属主（空值才钉，防竞态覆盖真属主）；失败静默。"""
        try:
            with tenant_connection(self.pool) as conn:
                conn.execute(
                    "UPDATE v2pdf_runs SET claimed_by=%s WHERE run_id=%s AND claimed_by=''",
                    (proc, run_id))
        except Exception:
            pass

    def release_claim_for_retry(self, run_id: str) -> bool:
        """Clear a terminal run's dead process lease before it is enqueued again."""
        with tenant_connection(self.pool) as conn:
            row = conn.execute(
                "UPDATE v2pdf_runs SET claimed_by='', updated=now() "
                "WHERE run_id=%s AND status='created' RETURNING 1", (run_id,)
            ).fetchone()
        return row is not None

    def steal_if_unclaimed(self, run_id: str, lease_s: float, proc: str) -> dict | None:
        """崩溃残档接管入口：无主或租约过期才钉名，抢到即持行锁窗口内复核。

        条件 UPDATE 自带互斥：别的副本正在接管（claimed_by=它且租约未过期）→ 抢不到返 None。
        P1-1：扩到 queued——排队项无在途工作，接管无损（调用方重投而非标 error）。
        """
        with tenant_connection(self.pool) as conn:
            row = conn.execute("""
                UPDATE v2pdf_runs
                   SET claimed_by=%(proc)s, updated=now()
                 WHERE run_id=%(rid)s AND status IN ('queued','running')
                   AND (claimed_by='' OR EXTRACT(EPOCH FROM (now()-updated)) > %(lease)s)
                RETURNING record""",
                {"proc": proc, "rid": run_id, "lease": lease_s}).fetchone()
        return row[0] if row else None

    # ---------------------------------------------------------------- 启动恢复认领

    def recovery_scan(self) -> list[dict]:
        """启动恢复候选（P1-4 重设计）：全盘 running 无锁扫描——属主甄别交调用方
        （进程存活表是进程内状态，SQL 无从 join），认领走 claim_dead 条件 UPDATE
        （原子互斥，替代旧 FOR UPDATE SKIP LOCKED 全量抢占——批判补遗②：那会偷走
        稳态健康副本的活 run）。tmp 缺失翻 error 用 mark_stuck_error（条件 running）。
        """
        with tenant_connection(self.pool) as conn:
            rows = conn.execute(
                "SELECT run_id, record, claimed_by FROM v2pdf_runs "
                "WHERE status='running'").fetchall()
        cols = ("run_id", "record", "claimed_by")
        return [dict(zip(cols, r)) for r in rows]

    def claim_dead(self, run_id: str, expect_proc: str, new_proc: str) -> dict | None:
        """原子认领：行仍被 expect_proc 持有（''=无主）才翻 new_proc，抢到返回 record。

        条件 UPDATE 自带互斥：并发恢复/接管的两副本只有一个把 claimed_by 从旧值翻走，
        输家返回 None 安全让位。区别于 steal_if_unclaimed（不要求 updated 租约过期——
        调用方已通过进程存活甄别确认属主死，鲜死属主的 updated 可能还很新鲜）。
        """
        with tenant_connection(self.pool) as conn:
            row = conn.execute("""
                UPDATE v2pdf_runs
                   SET claimed_by=%(new)s, updated=now()
                 WHERE run_id=%(rid)s AND status='running' AND claimed_by=%(old)s
                RETURNING record""",
                {"new": new_proc, "rid": run_id, "old": expect_proc}).fetchone()
        return row[0] if row else None

    # ---------------------------------------------------------------- watchdog 巡检

    def stale_running(self, timeout_s: float) -> list[dict]:
        """全盘 running 中「DB 侧 updated 超龄」的候选行（watchdog DB 段用）。

        候选过滤只看 updated（DB 自己写、时钟域干净）；阶段 ts 在应用侧解析成
        last_active_epoch 再叠加复核（_stage_ts_epoch——见模块头时钟域纪律）。
        SQL 只按 updated 粗筛——updated 被驱动进程心跳持续刷新，天然活性信号。
        """
        with tenant_connection(self.pool) as conn:
            rows = conn.execute("""
                SELECT run_id, record, claimed_by,
                       record->>'started' AS started,
                       EXTRACT(EPOCH FROM updated) AS updated_epoch
                  FROM v2pdf_runs
                 WHERE status='running'
                   AND EXTRACT(EPOCH FROM (now() - updated)) > %(cut)s""",
                {"cut": timeout_s}).fetchall()
        out: list[dict] = []
        for rid, rec, claimed, started, upd in rows:
            lae = max(_stage_ts_epoch(rec or {}), float(upd or 0.0))
            out.append({"run_id": rid, "record": rec, "claimed_by": claimed,
                        "started": started, "last_active_epoch": lae})
        return out

    def stale_queued(self, lease_s: float) -> list[dict]:
        """排队项无主停滞候选（P1-1 大盘巡检 queued 段）。

        排队项无阶段记录，活性只看 updated——由 _queue_hb_loop 每 WATCHDOG_PERIOD_S
        刷新，故用短租约（3×period）即足以区分「排队久（健康，心跳在续）」与
        「属主死（updated 停滞）」。本进程自己持有的排队项由调用方再甄别。
        """
        with tenant_connection(self.pool) as conn:
            rows = conn.execute("""
                SELECT run_id, record, claimed_by
                  FROM v2pdf_runs
                 WHERE status='queued'
                   AND EXTRACT(EPOCH FROM (now()-updated)) > %(lease)s""",
                {"lease": lease_s}).fetchall()
        cols = ("run_id", "record", "claimed_by")
        return [dict(zip(cols, r)) for r in rows]

    def recheck_age(self, run_id: str) -> float:
        """steal 持锁后复核：最近阶段落盘 epoch（应用侧解析，无行/无阶段返 0）。

        只看 stages[*].ts，刻意不含 updated——steal_if_unclaimed 自身会刷 updated，
        若复核含 updated 则永远「新鲜」、接管永不生效（P4b 潜 bug 修复）。ts 解析走
        应用时钟域（_stage_ts_epoch）：SQL cast naive 串会跨时区偏移（见模块头纪律）。
        """
        with tenant_connection(self.pool) as conn:
            row = conn.execute(
                "SELECT record FROM v2pdf_runs WHERE run_id=%s", (run_id,)).fetchone()
        return _stage_ts_epoch(row[0] or {}) if row else 0.0

    def mark_stuck_error(self, run_id: str, msg: str) -> bool:
        """大盘 watchdog 翻 error：record+status 同事务迁移（仍 running 才动，防竞态）。"""
        with tenant_connection(self.pool) as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT record FROM v2pdf_runs WHERE run_id=%s AND status='running' "
                    "FOR UPDATE", (run_id,)).fetchone()
                if not row:
                    return False
                rec = dict(row[0])
                rec["status"] = "error"
                rec["errors"] = [*(rec.get("errors") or []), msg][-20:]
                conn.execute("""
                    UPDATE v2pdf_runs SET record=%(rec)s, status='error',
                                          claimed_by='', updated=now()
                     WHERE run_id=%(rid)s""", {"rec": Json(rec), "rid": run_id})
        return True

    # ---------------------------------------------------------------- P2-2 成本归因

    def month_spend(self, uid: str, since_iso: str) -> float:
        """uid 自 since_iso（含）起的已结算成本（预算闸读数；created 为 ISO 文本，
        同格式字典序=时间序）。"""
        with tenant_connection(self.pool) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM((record->>'cost_usd')::numeric), 0) FROM v2pdf_runs "
                "WHERE owner=%s AND created >= %s AND record ? 'cost_usd'",
                (uid, since_iso)).fetchone()
        return float(row[0]) if row else 0.0

    def month_rollup(self, uid: str, since_iso: str) -> dict:
        """uid 月度汇总（/cost 与 CLI 导出同形：json 档 runs.month_rollup）。"""
        with tenant_connection(self.pool) as conn:
            row = conn.execute("""
                SELECT COALESCE(SUM((record->>'cost_usd')::numeric), 0),
                       COALESCE(SUM((record->'usage'->>'llm_in_tok')::bigint), 0),
                       COALESCE(SUM((record->'usage'->>'llm_out_tok')::bigint), 0),
                       COALESCE(SUM((record->'usage'->>'gpu_s')::numeric), 0),
                       count(*)
                  FROM v2pdf_runs
                 WHERE owner=%s AND created >= %s""", (uid, since_iso)).fetchone()
        row = row or (0, 0, 0, 0, 0)
        return {"spend_usd": round(float(row[0]), 6), "llm_in_tok": int(row[1]),
                "llm_out_tok": int(row[2]), "gpu_s": float(row[3]),
                "runs_n": int(row[4])}

    # ---------------------------------------------------------------- 滑窗限流

    def submit_window_count(self, uid: str) -> int:
        """跨进程滑窗计数并登记本次：单语句 upsert 返回 60s 窗口内已计次数。

        同 uid 并发提交在 UPDATE 行锁上串行，窗口不丢不重；返回值即「本次之前」的
        在窗计数——调用方判 >= per_min 即限流（本次仍登记时间戳，与 json 档滑窗一致）。
        """
        with tenant_connection(self.pool) as conn:
            row = conn.execute("""
                INSERT INTO v2pdf_submit_log (uid, ts, count) VALUES (%(uid)s, now(), 1)
                ON CONFLICT (uid) DO UPDATE SET
                    ts    = now(),
                    count = CASE WHEN EXTRACT(EPOCH FROM (now() - v2pdf_submit_log.ts)) > 60
                                 THEN 1 ELSE v2pdf_submit_log.count + 1 END
                RETURNING count""", {"uid": uid}).fetchone()
        return int(row[0]) if row else 0


class PostgresMembershipStore:
    """membership 表：uid PK 的会员/配额展示档（web 个人页 /me、/quota 的数据源）。

    只读展示路径：get() 供端点读；upsert() 供种子/运维写（测试同款）。
    不参与提交强制（_admit_submission 不读它——后续工作）。"""

    def __init__(self, pool: ConnectionPool):
        self.pool = pool  # v2pdf_membership 建表已上收 migrations/0002（P0-5），构造只接线

    def get(self, uid: str) -> dict | None:
        """单行读；无行回 None（端点据此给 free/全 0 默认视图）。"""
        with tenant_connection(self.pool) as conn:
            row = conn.execute("""
                SELECT uid, plan, display_name, avatar_hue,
                       ai_hours_used, ai_hours_total,
                       storage_gb_used, storage_gb_total,
                       stats_books, stats_knowledge_points, stats_accuracy
                  FROM v2pdf_membership WHERE uid=%s""", (uid,)).fetchone()
        if not row:
            return None
        cols = ("uid", "plan", "display_name", "avatar_hue",
                "ai_hours_used", "ai_hours_total", "storage_gb_used", "storage_gb_total",
                "stats_books", "stats_knowledge_points", "stats_accuracy")
        return dict(zip(cols, row))

    def upsert(self, data: dict) -> None:
        """全列 upsert（幂等可重跑）；record 预留扩展位（签名/备注等不进列的字段）。"""
        with tenant_connection(self.pool) as conn:
            conn.execute("""
                INSERT INTO v2pdf_membership
                    (uid, plan, display_name, avatar_hue,
                     ai_hours_used, ai_hours_total, storage_gb_used, storage_gb_total,
                     stats_books, stats_knowledge_points, stats_accuracy, record)
                VALUES (%(uid)s, %(plan)s, %(display_name)s, %(avatar_hue)s,
                        %(ai_hours_used)s, %(ai_hours_total)s,
                        %(storage_gb_used)s, %(storage_gb_total)s,
                        %(stats_books)s, %(stats_knowledge_points)s, %(stats_accuracy)s,
                        %(rec)s)
                ON CONFLICT (uid) DO UPDATE SET
                    plan = EXCLUDED.plan, display_name = EXCLUDED.display_name,
                    avatar_hue = EXCLUDED.avatar_hue,
                    ai_hours_used = EXCLUDED.ai_hours_used,
                    ai_hours_total = EXCLUDED.ai_hours_total,
                    storage_gb_used = EXCLUDED.storage_gb_used,
                    storage_gb_total = EXCLUDED.storage_gb_total,
                    stats_books = EXCLUDED.stats_books,
                    stats_knowledge_points = EXCLUDED.stats_knowledge_points,
                    stats_accuracy = EXCLUDED.stats_accuracy,
                    record = EXCLUDED.record, updated = now()""",
                {"uid": data["uid"], "plan": data.get("plan", "free"),
                 "display_name": data.get("display_name", ""),
                 "avatar_hue": int(data.get("avatar_hue", 258)),
                 "ai_hours_used": float(data.get("ai_hours_used", 0)),
                 "ai_hours_total": float(data.get("ai_hours_total", 0)),
                 "storage_gb_used": float(data.get("storage_gb_used", 0)),
                 "storage_gb_total": float(data.get("storage_gb_total", 0)),
                 "stats_books": int(data.get("stats_books", 0)),
                 "stats_knowledge_points": int(data.get("stats_knowledge_points", 0)),
                 "stats_accuracy": float(data.get("stats_accuracy", 0)),
                 "rec": Json(data.get("record") or {})})

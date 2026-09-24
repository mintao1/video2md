"""runs / state / tasks 的 Postgres 适配器（P3/P4b 存储分派 PG 档的实现体）。

设计口径：与 JSON 文件档**函数语义一一对应**——
- PostgresRunsStore.load() 返回 {run_id: run_dict}（dedup / 清理器的全表扫描语义保留）；
  merge(run) = 行级 upsert（多实例并发安全，替代文件档「fcntl 锁内读改写」）。
  无全表替换（文件档 _save_runs 的 save_all 语义在多实例下会覆盖他进程写入，PG 档禁用）。
- PostgresStateStore.save() 把「全量替换」降为逐 fp upsert——state 键集只增不删
  （重跑覆盖同 fp），语义等价。

表名带 v2md_ 前缀（与 langgraph checkpointer 的 checkpoints* 表同库不撞）；
owner/status/created 提取列供 PostgresTaskStore 的队列查询（P4b）与运维 SQL 用，
真相仍是 record jsonb——merge() 整行覆盖 record+status，两态天然一致。
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable

from psycopg.types.json import Json

if TYPE_CHECKING:
    from psycopg_pool import ConnectionPool

# watchdog 巡检 SQL：每行现算「最近活动 epoch」= GREATEST(阶段 ts 最大值, updated)。
# 阶段 ts 是 record->'stages' 各对象 .ts 的 ISO→epoch（缺键按 0）；updated 被驱动进程
# 每次 merge / 心跳 UPDATE 刷新，天然活性信号。
_AGE_SQL = """
GREATEST(
    COALESCE((SELECT max(EXTRACT(EPOCH FROM ((s.value)->>'ts')::timestamptz))
              FROM jsonb_each(record->'stages') AS s), 0.0),
    EXTRACT(EPOCH FROM updated)) AS last_active_epoch
"""


class PostgresRunsStore:
    """runs 表：run_id PK + 提取列 + record jsonb（整条 run dict）。"""

    def __init__(self, pool: ConnectionPool):
        self.pool = pool  # 建表已上收 migrations/0001（P0-5），构造只接线

    def load(self) -> dict:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT record FROM v2md_runs").fetchall()
        out: dict = {}
        for (rec,) in rows:
            out[rec["run_id"]] = rec
        return out

    def merge(self, run: dict) -> None:
        """行级 upsert（原子）。调用方无需持锁——文件档的锁纪律在此内化为行锁。"""
        with self.pool.connection() as conn:
            conn.execute("""
                INSERT INTO v2md_runs (run_id, owner, status, created, record)
                VALUES (%(run_id)s, %(owner)s, %(status)s, %(created)s, %(rec)s)
                ON CONFLICT (run_id) DO UPDATE SET
                    owner = EXCLUDED.owner,
                    status = EXCLUDED.status,
                    created = EXCLUDED.created,
                    record = EXCLUDED.record,
                    updated = now()""",
                {"run_id": run.get("run_id", ""),
                 "owner": run.get("owner", "") or "",
                 "status": run.get("status", "") or "",
                 "created": run.get("created", "") or "",
                 "rec": Json(run)})


class PostgresStateStore:
    """state 表：fp PK + record jsonb（幂等记录：note/transcript/title/url/owner…）。

    文件档的 fp 是外层 dict key（不进记录体），故表单存 fp 列。"""

    def __init__(self, pool: ConnectionPool):
        self.pool = pool  # 建表已上收 migrations/0001（P0-5），构造只接线

    def load(self) -> dict:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT fp, record FROM v2md_state").fetchall()
        return {fp: rec for fp, rec in rows}

    def save(self, s: dict) -> None:
        """逐 fp upsert（键只增不删 → 与文件档全量替换语义等价）。"""
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO v2md_state (fp, record) VALUES (%s, %s)
                    ON CONFLICT (fp) DO UPDATE SET record = EXCLUDED.record, updated = now()
                    """, [(fp, Json(rec)) for fp, rec in s.items()])


class PostgresTaskStore:
    """任务表 PG 真相源（P4b·多副本档）——只管「读侧 + 认领」，不碰状态写入。

    口径：record jsonb 是状态唯一真相，v2md_runs.status 提取列是其可查镜像
    （merge 整行覆盖两态天然一致）。本类方法全是纯 SQL 查询/单语句原子迁移：
    - counts/active_for/total_active/is_alive → /queue、配额闸、dedup 判活
    - recover_candidates → 启动恢复认领：FOR UPDATE SKIP LOCKED，claim=行锁本身，
      双起竞争败者跳过、胜者重投后由 _drive 的 merge 自然刷 claimed_by
    - stale_running/steal_if_unclaimed/heartbeat → watchdog DB 巡检与崩溃残档接管
    - submit_window_allowed → 跨进程滑窗限流（v2md_submit_log 单语句清旧+计数+判定）
    """

    def __init__(self, pool: ConnectionPool):
        self.pool = pool  # v2md_runs·claimed_by·v2md_submit_log 建表已上收 migrations/0001（P0-5）

    # ---------------------------------------------------------------- 队列视图

    def counts(self) -> dict:
        """{queued, running} 全盘计数（status 索引扫描，仅两态）。"""
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT status, count(*) FROM v2md_runs "
                "WHERE status IN ('queued','running') GROUP BY status").fetchall()
        c = {"queued": 0, "running": 0}
        for st, n in rows:
            c[st] = int(n)
        return c

    def total_active(self) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM v2md_runs WHERE status IN ('queued','running')").fetchone()
        return int(row[0]) if row else 0

    def active_for(self, uid: str) -> int:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM v2md_runs "
                "WHERE status IN ('queued','running') AND owner=%s", (uid,)).fetchone()
        return int(row[0]) if row else 0

    def is_alive(self, run_id: str) -> bool:
        """dedup 判活：DB 视角该 run 是否仍 queued/running（他进程驱动也算活）。"""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM v2md_runs WHERE run_id=%s "
                "AND status IN ('queued','running')", (run_id,)).fetchone()
        return row is not None

    def get_status_url(self, run_id: str) -> tuple[str | None, str | None]:
        """SSE 收尾判定的 DB 兜底：两行内存皆 miss 时现读 (status, url)。"""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT status, record->>'url' FROM v2md_runs WHERE run_id=%s",
                (run_id,)).fetchone()
        return (row[0], row[1]) if row else (None, None)

    # ---------------------------------------------------------------- 心跳 / 活性

    def heartbeat(self, run_id: str) -> bool:
        """阶段循环头打点：刷新 updated 作跨进程活性信号。终态不续（返回 False）。"""
        with self.pool.connection() as conn:
            row = conn.execute(
                "UPDATE v2md_runs SET updated=now() WHERE run_id=%s "
                "AND status='running' RETURNING 1", (run_id,)).fetchone()
        return row is not None

    def note_claimed_by(self, run_id: str, proc: str) -> None:
        """best-effort 登记属主（空值才钉，防竞态覆盖真属主）；失败静默。"""
        try:
            with self.pool.connection() as conn:
                conn.execute(
                    "UPDATE v2md_runs SET claimed_by=%s WHERE run_id=%s AND claimed_by=''",
                    (proc, run_id))
        except Exception:
            pass

    def steal_if_unclaimed(self, run_id: str, lease_s: float, proc: str) -> dict | None:
        """崩溃残档接管入口：无主或租约过期才钉名，抢到即持行锁窗口内复核。

        条件 UPDATE 自带互斥：别的副本正在接管（claimed_by=它且租约未过期）→ 抢不到返 None。
        """
        with self.pool.connection() as conn:
            row = conn.execute("""
                UPDATE v2md_runs
                   SET claimed_by=%(proc)s, updated=now()
                 WHERE run_id=%(rid)s AND status='running'
                   AND (claimed_by='' OR EXTRACT(EPOCH FROM (now()-updated)) > %(lease)s)
                RETURNING record""",
                {"proc": proc, "rid": run_id, "lease": lease_s}).fetchone()
        return row[0] if row else None

    # ---------------------------------------------------------------- 启动恢复认领

    def recover_candidates(self, tmp_missing: Callable[[dict], bool],
                           error_note: str) -> list[dict]:
        """重启中断 run 的认领式恢复（替代 json 档 lifespan 的全表扫）。

        FOR UPDATE SKIP LOCKED：并发起的多副本只有一份拿到候选集，败者跳过。
        tmp 已没的不可续跑：直接翻 error（record+status 同语句迁移，留痕对齐 ≤20 条纪律）；
        其余返回 record dict 供调用方 _enqueue 重投（图内幂等重入）。
        """
        out: list[dict] = []
        with self.pool.connection() as conn:
            with conn.transaction():
                rows = conn.execute(
                    "SELECT run_id, record FROM v2md_runs "
                    "WHERE status='running' FOR UPDATE SKIP LOCKED").fetchall()
                for rid, rec in rows:
                    if tmp_missing(rec):
                        rec["status"] = "error"
                        rec["errors"] = [*(rec.get("errors") or []), error_note][-20:]
                        conn.execute("""
                            UPDATE v2md_runs SET record=%(rec)s, status='error',
                                                  claimed_by='', updated=now()
                             WHERE run_id=%(rid)s""", {"rec": Json(rec), "rid": rid})
                        continue
                    out.append(rec)
        return out

    # ---------------------------------------------------------------- watchdog 巡检

    def stale_running(self, timeout_s: float) -> list[dict]:
        """全盘 running 中「DB 侧最近活动超时」的候选行（watchdog DB 段用）。

        SQL 只按 GREATEST(阶段 ts, updated) 粗筛——updated 被 cmd 心跳通知刷新的行不出现；
        本进程 _LOCAL_HB 更年轻的活由 check_once 叠加复核。每行带 started（record->>'started'）
        供三层信号与 steal 持锁后复算。
        """
        cutoff = time.time() - timeout_s
        with self.pool.connection() as conn:
            rows = conn.execute(f"""
                SELECT run_id, record->>'started' AS started, claimed_by,
                       {_AGE_SQL}
                  FROM v2md_runs
                 WHERE status='running'
                   AND GREATEST(
                         COALESCE((SELECT max(EXTRACT(EPOCH FROM ((s.value)->>'ts')::timestamptz))
                                   FROM jsonb_each(record->'stages') AS s), 0.0),
                         EXTRACT(EPOCH FROM updated)) < %(cut)s""",
                {"cut": cutoff}).fetchall()
        cols = ("run_id", "started", "claimed_by", "last_active_epoch")
        return [dict(zip(cols, r)) for r in rows]

    def recheck_age(self, run_id: str) -> float:
        """steal 持锁后复核：当前 last_active_epoch（无行返 0=已被别处处置）。"""
        with self.pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_AGE_SQL} FROM v2md_runs WHERE run_id=%s", (run_id,)).fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    def mark_stuck_error(self, run_id: str, msg: str) -> bool:
        """大盘 watchdog 翻 error：record+status 同事务迁移（仍 running 才动，防竞态）。"""
        with self.pool.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    "SELECT record FROM v2md_runs WHERE run_id=%s AND status='running' "
                    "FOR UPDATE", (run_id,)).fetchone()
                if not row:
                    return False
                rec = dict(row[0])
                rec["status"] = "error"
                rec["errors"] = [*(rec.get("errors") or []), msg][-20:]
                conn.execute("""
                    UPDATE v2md_runs SET record=%(rec)s, status='error',
                                          claimed_by='', updated=now()
                     WHERE run_id=%(rid)s""", {"rec": Json(rec), "rid": run_id})
        return True

    # ---------------------------------------------------------------- 滑窗限流

    def submit_window_count(self, uid: str) -> int:
        """跨进程滑窗计数并登记本次：单语句 upsert 返回 60s 窗口内已计次数。

        同 uid 并发提交在 UPDATE 行锁上串行，窗口不丢不重；返回值即「本次之前」的
        在窗计数——调用方判 >= per_min 即限流（本次仍登记时间戳，与 json 档滑窗一致）。
        """
        with self.pool.connection() as conn:
            row = conn.execute("""
                INSERT INTO v2md_submit_log (uid, ts, count) VALUES (%(uid)s, now(), 1)
                ON CONFLICT (uid) DO UPDATE SET
                    ts    = now(),
                    count = CASE WHEN EXTRACT(EPOCH FROM (now() - v2md_submit_log.ts)) > 60
                                 THEN 1 ELSE v2md_submit_log.count + 1 END
                RETURNING count""", {"uid": uid}).fetchone()
        return int(row[0]) if row else 0

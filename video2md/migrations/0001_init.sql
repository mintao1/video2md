-- 0001_init：收编基线（P0-5）。
-- 此前 DDL 散在四个 store 构造函数里 lazy 首触隐式建表（pgstore.py ×3 +
-- auth/pg_store.py ×1，含 claimed_by 的 ALTER 补丁），自本基线起 migrations/ 是
-- schema 唯一真相，构造函数只接线不再建表。
-- 全部幂等（IF NOT EXISTS）：存量库（基线前建的表）重跑本脚本是 no-op，直接补记版本行。
-- 边界声明：langgraph PostgresSaver.setup() 自管的 checkpoints* 表族同库共存，
-- 属三方边界不收编（video2md/graph.py）。
CREATE TABLE IF NOT EXISTS v2md_runs (
    run_id     text PRIMARY KEY,
    owner      text NOT NULL DEFAULT '',
    status     text NOT NULL DEFAULT '',
    created    text NOT NULL DEFAULT '',
    updated    timestamptz NOT NULL DEFAULT now(),
    record     jsonb NOT NULL,
    claimed_by text NOT NULL DEFAULT ''      -- P4b 租约认领（原 ALTER 补丁收编，列序对齐存量库）
);
CREATE INDEX IF NOT EXISTS v2md_runs_status_idx ON v2md_runs (status);
CREATE INDEX IF NOT EXISTS v2md_runs_owner_idx ON v2md_runs (owner);
CREATE TABLE IF NOT EXISTS v2md_state (
    fp      text PRIMARY KEY,
    updated timestamptz NOT NULL DEFAULT now(),
    record  jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS v2md_submit_log (
    uid   text PRIMARY KEY,
    ts    timestamptz NOT NULL DEFAULT now(),
    count integer NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS v2md_users (
    uid     text PRIMARY KEY,
    created text NOT NULL DEFAULT '',
    record  jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS v2md_identities (
    provider    text NOT NULL,
    external_id text NOT NULL,
    uid         text NOT NULL,
    PRIMARY KEY (provider, external_id)
);

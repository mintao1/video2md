-- 0002_membership：会员/配额展示档（web 个人页 ProfileView 的真数据源）。
-- 只读展示路径：GET /me、GET /quota 读它；不参与 _admit_submission 强制（后续工作）。
-- 无 FK 到 v2pdf_users：种子时序上 membership 可先于身份行存在，展示档允许
-- 「未登录过的预置额度」。幂等：全 IF NOT EXISTS，存量库重跑 no-op。
-- plan 用 CHECK 锁死枚举（与前端 types User.plan 'free'|'pro'|'svip' 对齐）。
CREATE TABLE IF NOT EXISTS v2pdf_membership (
    uid                    text PRIMARY KEY,
    plan                   text NOT NULL DEFAULT 'free'
                             CHECK (plan IN ('free', 'pro', 'svip')),
    display_name           text NOT NULL DEFAULT '',
    avatar_hue             integer NOT NULL DEFAULT 258,
    ai_hours_used          double precision NOT NULL DEFAULT 0,
    ai_hours_total         double precision NOT NULL DEFAULT 0,
    storage_gb_used        double precision NOT NULL DEFAULT 0,
    storage_gb_total       double precision NOT NULL DEFAULT 0,
    stats_books            integer NOT NULL DEFAULT 0,
    stats_knowledge_points integer NOT NULL DEFAULT 0,
    stats_accuracy         double precision NOT NULL DEFAULT 0,
    record                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated                timestamptz NOT NULL DEFAULT now()
);

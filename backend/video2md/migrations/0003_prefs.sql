-- 0003_prefs：用户偏好（教材库收藏/阅读进度）——web 教材库页 GET/PUT /prefs 的数据源。
-- (uid, run_id) 复合主键；run 归属校验在端点层（_require_run_access），表不 FK
-- v2md_runs（json 档无处挂 FK，跨存储一致性由端点纪律保证，对齐 0002 membership 无 FK 口径）。
-- progress 用 CHECK 锁死 0-100（与前端 Book.progress 语义对齐）。
-- 幂等：全 IF NOT EXISTS，存量库重跑 no-op。
CREATE TABLE IF NOT EXISTS v2md_prefs (
    uid      text NOT NULL,
    run_id   text NOT NULL,
    fav      boolean NOT NULL DEFAULT false,
    progress integer NOT NULL DEFAULT 0
             CHECK (progress BETWEEN 0 AND 100),
    updated  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (uid, run_id)
);

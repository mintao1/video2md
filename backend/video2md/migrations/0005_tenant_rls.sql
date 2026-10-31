-- 0005_tenant_rls: tenant ownership, state partitioning and database roles.
--
-- The migration/owner role owns the schema.  The API connects as the
-- video2md_app role and receives only DML grants.  RLS is enabled even for
-- tables that are also used by background workers; owner/migrate roles are
-- explicitly allowed by the policy helper so queue recovery and migrations
-- keep working without granting BYPASSRLS.

-- Existing state rows predate tenant partitioning.  Their owner is copied
-- from the record, with the empty owner retained for CLI/development rows.
ALTER TABLE v2md_state ADD COLUMN IF NOT EXISTS owner text;
UPDATE v2md_state
   SET owner = COALESCE(record->>'owner', '')
 WHERE owner IS NULL;
UPDATE v2md_state
   SET owner = COALESCE(record->>'owner', '')
 WHERE record ? 'owner' AND owner IS DISTINCT FROM COALESCE(record->>'owner', '');
ALTER TABLE v2md_state ALTER COLUMN owner SET DEFAULT '';
ALTER TABLE v2md_state ALTER COLUMN owner SET NOT NULL;
ALTER TABLE v2md_state DROP CONSTRAINT IF EXISTS v2md_state_pkey;
ALTER TABLE v2md_state ADD CONSTRAINT v2md_state_pkey PRIMARY KEY (owner, fp);
CREATE INDEX IF NOT EXISTS v2md_state_fp_idx ON v2md_state (fp);

-- Prevent a caller from writing a row whose extracted tenant disagrees with
-- the JSON truth.  Legacy CLI rows may omit owner and remain owner=''.
UPDATE v2md_runs
   SET owner = COALESCE(record->>'owner', '')
 WHERE record ? 'owner' AND owner IS DISTINCT FROM COALESCE(record->>'owner', '');
ALTER TABLE v2md_runs DROP CONSTRAINT IF EXISTS v2md_runs_owner_record_ck;
ALTER TABLE v2md_runs ADD CONSTRAINT v2md_runs_owner_record_ck
    CHECK (NOT (record ? 'owner') OR COALESCE(record->>'owner', '') = owner);
ALTER TABLE v2md_state DROP CONSTRAINT IF EXISTS v2md_state_owner_record_ck;
ALTER TABLE v2md_state ADD CONSTRAINT v2md_state_owner_record_ck
    CHECK (NOT (record ? 'owner') OR COALESCE(record->>'owner', '') = owner);

-- Keep the run owner/run-id pair unique for future tenant-aware references.
-- Existing profile and preference tables intentionally remain FK-free: quota
-- rows may be pre-provisioned before first login and the current test/migration
-- order truncates parent and child tables independently.  Orphan cleanup is a
-- separate data-quality operation, not a reason to weaken this rollout.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'v2md_runs_owner_run_uq') THEN
        ALTER TABLE v2md_runs
            ADD CONSTRAINT v2md_runs_owner_run_uq UNIQUE (owner, run_id);
    END IF;
END
$$;

-- RLS helper.  The app role uses a concrete tenant_id per request/worker
-- checkout; owner/migrate roles are explicitly allowed for DDL/recovery.
CREATE OR REPLACE FUNCTION v2md_tenant_visible(row_owner text)
RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT current_user IN ('video2md_owner', 'video2md_migrate')
        OR COALESCE(NULLIF(current_setting('v2md.tenant_id', true), ''), '*') = '*'
        OR row_owner = current_setting('v2md.tenant_id', true)
$$;

-- Owner-bearing columns use one policy shape.  FORCE ensures table owners do
-- not accidentally become an unfiltered application path; owner/migrate are
-- explicitly exempted by the helper above.
ALTER TABLE v2md_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE v2md_runs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS v2md_runs_tenant ON v2md_runs;
CREATE POLICY v2md_runs_tenant ON v2md_runs
    USING (v2md_tenant_visible(owner))
    WITH CHECK (v2md_tenant_visible(owner));

ALTER TABLE v2md_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE v2md_state FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS v2md_state_tenant ON v2md_state;
CREATE POLICY v2md_state_tenant ON v2md_state
    USING (v2md_tenant_visible(owner))
    WITH CHECK (v2md_tenant_visible(owner));

ALTER TABLE v2md_submit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE v2md_submit_log FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS v2md_submit_log_tenant ON v2md_submit_log;
CREATE POLICY v2md_submit_log_tenant ON v2md_submit_log
    USING (v2md_tenant_visible(uid))
    WITH CHECK (v2md_tenant_visible(uid));

ALTER TABLE v2md_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE v2md_users FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS v2md_users_tenant ON v2md_users;
CREATE POLICY v2md_users_tenant ON v2md_users
    USING (v2md_tenant_visible(uid))
    WITH CHECK (v2md_tenant_visible(uid));

ALTER TABLE v2md_identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE v2md_identities FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS v2md_identities_tenant ON v2md_identities;
CREATE POLICY v2md_identities_tenant ON v2md_identities
    USING (v2md_tenant_visible(uid))
    WITH CHECK (v2md_tenant_visible(uid));

ALTER TABLE v2md_membership ENABLE ROW LEVEL SECURITY;
ALTER TABLE v2md_membership FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS v2md_membership_tenant ON v2md_membership;
CREATE POLICY v2md_membership_tenant ON v2md_membership
    USING (v2md_tenant_visible(uid))
    WITH CHECK (v2md_tenant_visible(uid));

ALTER TABLE v2md_prefs ENABLE ROW LEVEL SECURITY;
ALTER TABLE v2md_prefs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS v2md_prefs_tenant ON v2md_prefs;
CREATE POLICY v2md_prefs_tenant ON v2md_prefs
    USING (v2md_tenant_visible(uid))
    WITH CHECK (v2md_tenant_visible(uid));

ALTER TABLE v2md_llm_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE v2md_llm_settings FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS v2md_llm_settings_tenant ON v2md_llm_settings;
CREATE POLICY v2md_llm_settings_tenant ON v2md_llm_settings
    USING (v2md_tenant_visible(uid))
    WITH CHECK (v2md_tenant_visible(uid));

-- Remove accidental public access, then grant only the operations used by the
-- API.  This database also contains unrelated Buzuti tables owned by another
-- role, so never use ON ALL TABLES/SEQUENCES here: it would make this migration
-- role attempt to revoke/grant privileges on objects it does not own.
DO $$
DECLARE t record;
BEGIN
    FOR t IN
        SELECT tablename FROM pg_tables
         WHERE schemaname = 'public'
           AND (tablename LIKE 'v2md_%' OR tablename LIKE 'checkpoint%')
    LOOP
        EXECUTE format('REVOKE ALL ON TABLE %I.%I FROM PUBLIC', 'public', t.tablename);
    END LOOP;
    FOR t IN
        SELECT sequence_name FROM information_schema.sequences
         WHERE sequence_schema = 'public'
           AND (sequence_name LIKE 'v2md_%' OR sequence_name LIKE 'checkpoint%')
    LOOP
        EXECUTE format('REVOKE ALL ON SEQUENCE %I.%I FROM PUBLIC', 'public', t.sequence_name);
    END LOOP;
END
$$;
GRANT USAGE ON SCHEMA public TO video2md_app, video2md_owner, video2md_migrate;
DO $$
DECLARE t record;
BEGIN
    FOR t IN
        SELECT tablename FROM pg_tables
         WHERE schemaname = 'public'
           AND (tablename LIKE 'v2md_%' OR tablename LIKE 'checkpoint%')
    LOOP
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I.%I TO video2md_app', 'public', t.tablename);
        EXECUTE format('GRANT ALL PRIVILEGES ON TABLE %I.%I TO video2md_owner, video2md_migrate', 'public', t.tablename);
    END LOOP;
    FOR t IN
        SELECT sequence_name FROM information_schema.sequences
         WHERE sequence_schema = 'public'
           AND (sequence_name LIKE 'v2md_%' OR sequence_name LIKE 'checkpoint%')
    LOOP
        EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %I.%I TO video2md_app', 'public', t.sequence_name);
        EXECUTE format('GRANT ALL PRIVILEGES ON SEQUENCE %I.%I TO video2md_owner, video2md_migrate', 'public', t.sequence_name);
    END LOOP;
END
$$;
-- The API only reads the migration ledger during startup; it must not be able
-- to rewrite schema history and hide an incomplete deployment.
REVOKE INSERT, UPDATE, DELETE ON v2md_schema_version FROM video2md_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO video2md_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON TABLES TO video2md_owner, video2md_migrate;

-- Keep ownership with the migration role after the first hardening run.  The
-- ALTERs are intentionally explicit so a failed/partial rollout is visible.
ALTER TABLE v2md_runs OWNER TO video2md_migrate;
ALTER TABLE v2md_state OWNER TO video2md_migrate;
ALTER TABLE v2md_submit_log OWNER TO video2md_migrate;
ALTER TABLE v2md_users OWNER TO video2md_migrate;
ALTER TABLE v2md_identities OWNER TO video2md_migrate;
ALTER TABLE v2md_membership OWNER TO video2md_migrate;
ALTER TABLE v2md_prefs OWNER TO video2md_migrate;
ALTER TABLE v2md_llm_settings OWNER TO video2md_migrate;

#!/bin/sh
# Repeatable bootstrap for existing and fresh PostgreSQL volumes.
# The postgres service runs this as its initialized owner; passwords arrive
# through the mounted production env file and are never printed.
set -eu

: "${VIDEO2MD_APP_PASSWORD:?set VIDEO2MD_APP_PASSWORD in the production secret file}"
: "${VIDEO2MD_MIGRATE_PASSWORD:?set VIDEO2MD_MIGRATE_PASSWORD in the production secret file}"

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=app_password="$VIDEO2MD_APP_PASSWORD" \
  --set=migrate_password="$VIDEO2MD_MIGRATE_PASSWORD" <<'SQL'
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'video2md_app') THEN
        CREATE ROLE video2md_app;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'video2md_migrate') THEN
        CREATE ROLE video2md_migrate;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'video2md_owner') THEN
        CREATE ROLE video2md_owner NOLOGIN NOBYPASSRLS;
    END IF;
END
$$;

ALTER ROLE video2md_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS
    PASSWORD :'app_password';
ALTER ROLE video2md_migrate LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS
    PASSWORD :'migrate_password';
ALTER ROLE video2md_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;

-- The migration role needs DDL scope for the application schema, but no
-- database-level create/role/superuser powers.  API remains USAGE-only.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO video2md_migrate;

-- Existing installations may still have tables owned by POSTGRES_USER. Move
-- them before the application migration runs so video2md_migrate can apply
-- RLS/ownership changes without retaining a superuser DSN in the API.
DO $$
DECLARE t record;
BEGIN
    FOR t IN
        SELECT schemaname, tablename
          FROM pg_tables
         WHERE schemaname = 'public'
           AND (tablename LIKE 'v2md_%' OR tablename LIKE 'checkpoint%')
    LOOP
        EXECUTE format('ALTER TABLE %I.%I OWNER TO video2md_migrate', t.schemaname, t.tablename);
    END LOOP;
END
$$;
SQL

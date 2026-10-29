-- Bootstrap role names before the application migrations run.
-- Credentials are provisioned out-of-band (Kubernetes Secret / psql).  These
-- roles intentionally start NOLOGIN; migration 0005 applies ownership, RLS
-- and table grants after DDL exists.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'video2md_app') THEN
        CREATE ROLE video2md_app NOLOGIN NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'video2md_owner') THEN
        CREATE ROLE video2md_owner NOLOGIN NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'video2md_migrate') THEN
        CREATE ROLE video2md_migrate NOLOGIN NOBYPASSRLS;
    END IF;
END
$$;

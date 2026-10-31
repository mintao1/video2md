-- 0007_run_id_text_compat: widen legacy UUID run-id columns to text.
--
-- Early production installations created v2md_runs (and, in some cases,
-- v2md_prefs) with a UUID run_id column.  The application has always treated
-- run ids as opaque strings: historical imports contain short ids such as
-- ``8a9826c9`` while newly submitted runs use canonical UUID strings.  A
-- comparison against a UUID column makes PostgreSQL cast those historical
-- values and raises ``22P02 invalid input syntax for type uuid`` before the
-- endpoint can return the record.  Keep the old values and the existing
-- primary-key/index definitions, but make the storage type match the API
-- contract.
--
-- The guards keep this migration safe for databases where a table was created
-- by a newer baseline or where an optional table is not present yet.  It is
-- intentionally a one-way widening conversion and is idempotent after the
-- schema-version ledger records it.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'v2md_runs'
          AND column_name = 'run_id'
          AND data_type <> 'text'
    ) THEN
        ALTER TABLE v2md_runs
            ALTER COLUMN run_id TYPE text USING run_id::text;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'v2md_prefs'
          AND column_name = 'run_id'
          AND data_type <> 'text'
    ) THEN
        ALTER TABLE v2md_prefs
            ALTER COLUMN run_id TYPE text USING run_id::text;
    END IF;
END
$$;

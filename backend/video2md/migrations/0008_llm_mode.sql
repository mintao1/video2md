-- 0008_llm_mode: explicit per-user LLM mode.
--   auto   = use the project-provided LLM (service LLM_BASE_URL/LLM_MODEL)
--   custom = use the user's own OpenAI-compatible credentials
-- Existing rows predate the switch and were already routed to the user's own
-- credentials, so they default to 'custom' (behaviour unchanged). Users without a
-- row are implicitly 'auto'. Switching to auto keeps the saved ciphertext so the
-- user can switch back without re-entering the key.
ALTER TABLE v2md_llm_settings
    ADD COLUMN IF NOT EXISTS mode text NOT NULL DEFAULT 'custom';
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'v2md_llm_settings_mode_ck'
    ) THEN
        ALTER TABLE v2md_llm_settings
            ADD CONSTRAINT v2md_llm_settings_mode_ck CHECK (mode IN ('auto', 'custom'));
    END IF;
END
$$;

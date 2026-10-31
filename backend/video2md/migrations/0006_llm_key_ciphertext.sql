-- 0006_llm_key_ciphertext: prevent new plaintext LLM credentials.
-- Existing rows are deliberately NOT VALID so an operator can decrypt/re-encrypt
-- them with the configured V2MD_LLM_SETTINGS_KEY before validating the constraint.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'v2pdf_llm_settings_ciphertext_ck'
    ) THEN
        ALTER TABLE v2pdf_llm_settings
            ADD CONSTRAINT v2pdf_llm_settings_ciphertext_ck
            CHECK (api_key = '' OR api_key LIKE 'v1:aesgcm:%') NOT VALID;
    END IF;
END
$$;

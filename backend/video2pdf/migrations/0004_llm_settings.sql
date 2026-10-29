-- Private per-user credentials, never returned by the settings read API.
CREATE TABLE IF NOT EXISTS v2pdf_llm_settings (
    uid text PRIMARY KEY,
    base_url text NOT NULL,
    api_key text NOT NULL,
    model text NOT NULL DEFAULT '',
    updated timestamptz NOT NULL DEFAULT now()
);

-- Pages table for the AutoAgent page-builder workspace.
-- Backs DatabasePageStore (full HTML in `html`) and HybridPageStore
-- (metadata-only rows, body on disk).

CREATE TABLE IF NOT EXISTS pages (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    slug          TEXT NOT NULL,
    title         TEXT NOT NULL,
    agent_context TEXT NOT NULL DEFAULT '',
    html          TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_pages_user ON pages(user_id);

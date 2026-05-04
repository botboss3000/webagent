-- Agent templates (admin-configured, non-editable by user)
-- This is the blueprint for new agents. Only one row expected (id='default').
CREATE TABLE IF NOT EXISTS agent_templates (
    id              TEXT PRIMARY KEY DEFAULT 'default',
    system_prompt   TEXT NOT NULL DEFAULT '',
    max_turn_count  INTEGER NOT NULL DEFAULT 10,
    model           TEXT,              -- NULL → env var fallback
    provider        TEXT,              -- NULL → env var fallback
    temperature     REAL NOT NULL DEFAULT 0.0,
    max_tokens      INTEGER NOT NULL DEFAULT 4096,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Assigned agents (one per user, created on first chat)
CREATE TABLE IF NOT EXISTS agents (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL UNIQUE,
    system_prompt   TEXT NOT NULL DEFAULT '',
    max_turn_count  INTEGER NOT NULL DEFAULT 10,
    model           TEXT,              -- NULL → env var fallback
    provider        TEXT,              -- NULL → env var fallback
    temperature     REAL NOT NULL DEFAULT 0.0,
    max_tokens      INTEGER NOT NULL DEFAULT 4096,
    status          TEXT NOT NULL DEFAULT 'active',
    metadata        TEXT NOT NULL DEFAULT '{}',
    assigned_at     TEXT NOT NULL DEFAULT (datetime('now')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_agents_user ON agents(user_id);

-- Seed the default template (use INSERT OR IGNORE so it survives restarts)
INSERT OR IGNORE INTO agent_templates (id, system_prompt, max_turn_count)
VALUES ('default', '', 10);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY DEFAULT 'default_agent',
    max_turn_count INTEGER DEFAULT 10
);

INSERT OR IGNORE INTO agents (id, max_turn_count) VALUES ('default_agent', 10);
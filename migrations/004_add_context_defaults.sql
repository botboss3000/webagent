-- =============================================================================
-- Migration 004: Add context_defaults table
--
-- Provides a template/source table of default context documents that are
-- copied into a user's context_documents when they first interact.
-- =============================================================================

-- Table: context_defaults (shared across all users, no user_id column)
CREATE TABLE IF NOT EXISTS context_defaults (
    id TEXT PRIMARY KEY,
    context_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_context_defaults_type ON context_defaults(context_type);

-- Seed default rows (only if empty)
INSERT OR IGNORE INTO context_defaults (id, context_type, title, content, tags)
VALUES
(
    'dflt-agent-0001',
    'agent',
    'Agent Identity',
    'I am Bot Boss, an AI-powered assistant designed to help you manage tasks, run tools, search the web, and automate workflows. I have access to a growing library of tools and can create new ones on the fly.

My capabilities include:
- Web search and information retrieval
- File and code management
- Task automation and job scheduling
- Memory and context management
- Session history search',
    '["identity", "default"]'
),
(
    'dflt-user-0001',
    'user',
    'User Profile',
    'This document describes the user I am assisting. Customize this section with details about the user''s preferences, goals, and background to help me provide more personalized assistance.',
    '["profile", "default"]'
),
(
    'dflt-jobs-0001',
    'jobs',
    'Jobs & Tasks',
    'This section defines scheduled jobs, recurring tasks, and background processes. Jobs can be configured to run at specified intervals and can trigger tools or sequences of actions.

Define jobs as structured markdown or JSON blocks. Each job should specify its trigger, action, and any parameters.',
    '["jobs", "tasks", "default"]'
),
(
    'dflt-skills-0001',
    'skills',
    'Skills Registry',
    'Skills are reusable capabilities that can be loaded and invoked by the agent. Each skill has a name, description, and implementation. Skills can be created dynamically and versioned.

Active skills are loaded from the skills table and made available as tools. Create new skills by providing a name, description, parameter schema, and implementation code.',
    '["skills", "default"]'
);

-- Output confirmation
SELECT '✅ Migration 004: context_defaults table created and seeded' AS result;

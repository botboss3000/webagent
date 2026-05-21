"""
Local SQLite storage backend for webAgent.

Completely independent from Supabase. Uses a local SQLite database file.
Auto-creates tables on first use matching the Supabase schema.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import struct
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import numpy as np

from app.models.schemas import InteractionRecord
from app.db.interface import StorageBackend
from app.agent.embed import embed_text, EMBED_DIM

logger = logging.getLogger(__name__)

# Default path for the local database (alongside this module)
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "local.db")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return str(uuid.uuid4())


# FTS5 MATCH treats AND/OR/NOT/NEAR, quotes, and parens as syntax — never pass raw chat text.
_FTS5_QUERY_OPS = frozenset({"AND", "OR", "NOT", "NEAR"})


def _fts5_safe_match_query(raw: str, max_tokens: int = 12, max_token_len: int = 64) -> Optional[str]:
    """Turn free text into a conservative prefix OR-query safe for FTS5 MATCH."""
    if not raw or not raw.strip():
        return None
    tokens = re.findall(r"\w+", raw, flags=re.UNICODE)
    parts: List[str] = []
    for t in tokens:
        if len(t) < 2 or t.upper() in _FTS5_QUERY_OPS:
            continue
        if len(t) > max_token_len:
            t = t[:max_token_len]
        parts.append(f"{t}*")
        if len(parts) >= max_tokens:
            break
    if not parts:
        return None
    return " OR ".join(parts)


# ── Schema DDL ──────────────────────────────────────────────────────────────

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT,
    metadata TEXT,
    agent_id TEXT,
    participants TEXT DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS interactions (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    parent_id TEXT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_name TEXT,
    tool_call_id TEXT,
    channel TEXT,
    metadata TEXT,
    input TEXT,
    output TEXT,
    source TEXT,
    from_id TEXT,
    to_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_interactions_session ON interactions(session_id);
CREATE INDEX IF NOT EXISTS idx_interactions_created ON interactions(created_at);

CREATE TABLE IF NOT EXISTS session_summaries (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
    title TEXT,
    summary TEXT NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_summaries_user ON session_summaries(user_id);

CREATE TABLE IF NOT EXISTS agent_templates (
    id TEXT PRIMARY KEY DEFAULT 'default',
    max_turn_count INTEGER NOT NULL DEFAULT 10,
    model TEXT,
    provider TEXT,
    temperature REAL NOT NULL DEFAULT 0.0,
    max_tokens INTEGER NOT NULL DEFAULT 4096,
    metadata TEXT NOT NULL DEFAULT '{}',
    trigger_type TEXT NOT NULL DEFAULT 'user_input',
    trigger_key TEXT,
    loop_logic TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Agent templates seeded from app/context/agents/*.json — no hardcoded defaults

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    template_id TEXT,
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    is_user_default INTEGER NOT NULL DEFAULT 0,
    max_turn_count INTEGER NOT NULL DEFAULT 10,
    model TEXT,
    provider TEXT,
    temperature REAL NOT NULL DEFAULT 0.0,
    max_tokens INTEGER NOT NULL DEFAULT 4096,
    status TEXT NOT NULL DEFAULT 'active',
    metadata TEXT NOT NULL DEFAULT '{}',
    trigger_type TEXT NOT NULL DEFAULT 'user_input',
    trigger_key TEXT,
    loop_logic TEXT NOT NULL DEFAULT '[]',
    assigned_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    turn_count INTEGER NOT NULL DEFAULT 0,
    admin_users TEXT NOT NULL DEFAULT '[]',
    member_users TEXT NOT NULL DEFAULT '[]',
    user_mode TEXT NOT NULL DEFAULT 'anonymous'
);

-- ============================================================
-- Agent Prompts: per-slot content with optional per-user overrides.
-- One row per (agent_id, slot_name, user_id). user_id IS NULL = admin base;
-- non-null user_id = override owned by that user (incl. anon visitors).
-- Slot policy (order_index, lock, merge_mode) lives only on admin base rows.
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_prompts (
    id              TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    slot_name       TEXT NOT NULL,
    user_id         TEXT,
    order_index     INTEGER,
    lock            INTEGER,
    merge_mode      TEXT,
    content         TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by      TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_prompts_slot_user
    ON agent_prompts(agent_id, slot_name, IFNULL(user_id, ''));
CREATE INDEX IF NOT EXISTS idx_agent_prompts_agent ON agent_prompts(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_prompts_user  ON agent_prompts(user_id);

-- ============================================================
-- Agent Connections: per-agent channel/integration config
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_connections (
    id              TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    connection_type TEXT NOT NULL,
    section         TEXT NOT NULL DEFAULT 'channel',
    enabled         INTEGER NOT NULL DEFAULT 0,
    config          TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(agent_id, connection_type)
);

CREATE INDEX IF NOT EXISTS idx_agent_conn_agent ON agent_connections(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_conn_type ON agent_connections(connection_type);

-- ============================================================
-- Memory System: core knowledge brain
-- ============================================================

-- Core memory pages (compiled truth + timeline pattern)
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    slug            TEXT NOT NULL,
    page_type       TEXT NOT NULL CHECK (page_type IN (
                        'person', 'company', 'deal', 'meeting', 'project',
                        'idea', 'concept', 'writing', 'program', 'personal',
                        'media', 'inbox', 'archive'
                    )),
    title           TEXT NOT NULL,
    compiled_truth  TEXT NOT NULL DEFAULT '',
    timeline        TEXT NOT NULL DEFAULT '',
    frontmatter     TEXT NOT NULL DEFAULT '{}',  -- JSON
    content_hash    TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id);
CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(page_type);
CREATE INDEX IF NOT EXISTS idx_memories_updated ON memories(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_trgm ON memories(title COLLATE NOCASE);

-- FTS5 full-text search index
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    slug UNINDEXED,
    title,
    compiled_truth,
    timeline,
    content='memories',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS trg_memories_fts_insert
    AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, slug, title, compiled_truth, timeline)
    VALUES (new.rowid, new.slug, new.title, new.compiled_truth, new.timeline);
END;

CREATE TRIGGER IF NOT EXISTS trg_memories_fts_delete
    AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, slug, title, compiled_truth, timeline)
    VALUES ('delete', old.rowid, old.slug, old.title, old.compiled_truth, old.timeline);
END;

CREATE TRIGGER IF NOT EXISTS trg_memories_fts_update
    AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, slug, title, compiled_truth, timeline)
    VALUES ('delete', old.rowid, old.slug, old.title, old.compiled_truth, old.timeline);
    INSERT INTO memories_fts(rowid, slug, title, compiled_truth, timeline)
    VALUES (new.rowid, new.slug, new.title, new.compiled_truth, new.timeline);
END;

-- Chunked content with optional vector embeddings
CREATE TABLE IF NOT EXISTS memory_chunks (
    id            TEXT PRIMARY KEY,
    memory_id     TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    chunk_index   INTEGER NOT NULL,
    chunk_text    TEXT NOT NULL,
    chunk_source  TEXT NOT NULL DEFAULT 'compiled_truth'
                  CHECK (chunk_source IN ('compiled_truth', 'timeline')),
    embedding     BLOB,       -- numpy float32 array for local vector search
    token_count   INTEGER,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(memory_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_memory ON memory_chunks(memory_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON memory_chunks(chunk_source);

-- Typed knowledge graph edges
CREATE TABLE IF NOT EXISTS memory_links (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    from_slug     TEXT NOT NULL,
    to_slug       TEXT NOT NULL,
    link_type     TEXT NOT NULL CHECK (link_type IN (
                      'works_at', 'founded', 'invested_in', 'advises',
                      'attended', 'knows', 'partnered_with', 'acquired',
                      'competes_with', 'references', 'related_to'
                  )),
    context       TEXT,
    weight        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, from_slug, to_slug, link_type)
);

CREATE INDEX IF NOT EXISTS idx_links_from ON memory_links(from_slug);
CREATE INDEX IF NOT EXISTS idx_links_to ON memory_links(to_slug);
CREATE INDEX IF NOT EXISTS idx_links_type ON memory_links(link_type);
CREATE INDEX IF NOT EXISTS idx_links_user ON memory_links(user_id);

-- Append-only timeline event log
CREATE TABLE IF NOT EXISTS memory_timeline (
    id            TEXT PRIMARY KEY,
    memory_id     TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    event_date    TEXT NOT NULL,       -- ISO date YYYY-MM-DD
    source        TEXT NOT NULL,       -- 'chat', 'email', 'meeting', 'tweet', 'web', 'manual'
    summary       TEXT NOT NULL,
    detail        TEXT,
    source_ref    TEXT,               -- optional URL, file path, or interaction_id
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_timeline_memory ON memory_timeline(memory_id);
CREATE INDEX IF NOT EXISTS idx_timeline_date ON memory_timeline(event_date DESC);

CREATE TABLE IF NOT EXISTS tools (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    code TEXT NOT NULL,
    description TEXT NOT NULL,
    parameters TEXT NOT NULL DEFAULT '{}',
    language TEXT NOT NULL DEFAULT 'python',
    status TEXT NOT NULL DEFAULT 'active',
    created_by TEXT NOT NULL,
    stages TEXT NOT NULL DEFAULT '[]',
    destructive INTEGER NOT NULL DEFAULT 0,
    agent_types TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tools_name ON tools(name);
CREATE INDEX IF NOT EXISTS idx_tools_status ON tools(status);
CREATE INDEX IF NOT EXISTS idx_tools_creator ON tools(created_by);

-- tool_executions merged into interactions.metadata

CREATE TABLE IF NOT EXISTS agent_credentials (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    credential_type TEXT NOT NULL,
    encrypted_data TEXT NOT NULL,
    display_name TEXT,
    expires_at TEXT,
    scopes TEXT DEFAULT '[]',
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    requires_renewal INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- Skill System: agent capabilities with performance tracking
-- ============================================================

CREATE TABLE IF NOT EXISTS skills (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT,
    code            TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    base_skill_id   TEXT,                    -- NULL for official, points to parent for user forks
    is_official     INTEGER NOT NULL DEFAULT 0,
    tags            TEXT NOT NULL DEFAULT '[]', -- JSON array
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_skills_user ON skills(user_id);
CREATE INDEX IF NOT EXISTS idx_skills_name ON skills(name);
CREATE INDEX IF NOT EXISTS idx_skills_official ON skills(is_official);

-- Execution log: every skill invocation
CREATE TABLE IF NOT EXISTS skill_executions (
    id              TEXT PRIMARY KEY,
    skill_id        TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    user_id         TEXT NOT NULL,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    interaction_id  TEXT,                    -- links to interactions table
    success         INTEGER NOT NULL DEFAULT 1,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    steps_to_complete INTEGER NOT NULL DEFAULT 1,
    error_message   TEXT,
    input_params    TEXT DEFAULT '{}',        -- JSON
    output_summary  TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_exec_skill ON skill_executions(skill_id);
CREATE INDEX IF NOT EXISTS idx_exec_user ON skill_executions(user_id);
CREATE INDEX IF NOT EXISTS idx_exec_created ON skill_executions(created_at);
CREATE INDEX IF NOT EXISTS idx_exec_success ON skill_executions(success);

-- User feedback on skill outcomes
CREATE TABLE IF NOT EXISTS skill_feedback (
    id              TEXT PRIMARY KEY,
    skill_id        TEXT NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    execution_id    TEXT REFERENCES skill_executions(id) ON DELETE SET NULL,
    user_id         TEXT NOT NULL,
    feedback_type   TEXT NOT NULL CHECK (feedback_type IN ('positive', 'negative', 'correction')),
    message         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_feedback_skill ON skill_feedback(skill_id);
CREATE INDEX IF NOT EXISTS idx_feedback_user ON skill_feedback(user_id);
CREATE INDEX IF NOT EXISTS idx_feedback_type ON skill_feedback(feedback_type);

CREATE TABLE IF NOT EXISTS session_interrupts (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    interrupt_requested INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- Attachment storage
-- ============================================================

CREATE TABLE IF NOT EXISTS attachments (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    session_id      TEXT REFERENCES sessions(id),
    original_name   TEXT NOT NULL,
    mime_type       TEXT NOT NULL,
    size_bytes      INTEGER NOT NULL,
    storage_path    TEXT NOT NULL,
    metadata        TEXT NOT NULL DEFAULT '{}',  -- JSON: duration (voice), dimensions (image)
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_attachments_user ON attachments(user_id);
CREATE INDEX IF NOT EXISTS idx_attachments_session ON attachments(session_id);

-- ============================================================
-- Communication channels (Telegram, WhatsApp, SMS, etc.)
-- ============================================================

CREATE TABLE IF NOT EXISTS channel_identities (
    id              TEXT PRIMARY KEY,
    channel         TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    user_tier       TEXT NOT NULL DEFAULT 'anonymous'
                    CHECK (user_tier IN ('anonymous', 'channel_verified', 'full')),
    display_name    TEXT NOT NULL DEFAULT '',
    email           TEXT NOT NULL DEFAULT '',
    email_verified  INTEGER NOT NULL DEFAULT 0,
    linked_user_id  TEXT,
    metadata        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(channel, external_id)
);

CREATE INDEX IF NOT EXISTS idx_channel_user ON channel_identities(user_id);
CREATE INDEX IF NOT EXISTS idx_channel_channel_ext ON channel_identities(channel, external_id);

CREATE TABLE IF NOT EXISTS linking_codes (
    id              TEXT PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,
    source_user_id  TEXT NOT NULL,
    target_channel  TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT NOT NULL,
    used            INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_linking_codes_code ON linking_codes(code);

-- Generic inbound webhook registrations
-- ============================================================

CREATE TABLE IF NOT EXISTS webhook_registrations (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    name            TEXT NOT NULL,
    instructions    TEXT NOT NULL DEFAULT '',
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_webhook_reg_user ON webhook_registrations(user_id);

-- Inbound webhook event log
CREATE TABLE IF NOT EXISTS webhook_event_log (
    id              TEXT PRIMARY KEY,
    webhook_id      TEXT NOT NULL REFERENCES webhook_registrations(id) ON DELETE CASCADE,
    method          TEXT NOT NULL,
    headers         TEXT NOT NULL DEFAULT '{}',
    payload         TEXT NOT NULL DEFAULT '',
    response_status INTEGER NOT NULL DEFAULT 200,
    response_body   TEXT NOT NULL DEFAULT '',
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_webhook_log_hook ON webhook_event_log(webhook_id);
CREATE INDEX IF NOT EXISTS idx_webhook_log_created ON webhook_event_log(created_at DESC);

-- ============================================================
-- Auth Elements: per-user service credentials (LLM, Telegram, Google, etc.)
-- config holds non-sensitive settings (JSON).
-- secret_ref holds the actual secret (API key, token) — migrating to vault later.
-- ============================================================
CREATE TABLE IF NOT EXISTS auth_elements (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    service         TEXT NOT NULL,             -- 'llm', 'telegram', 'google', etc.
    label           TEXT NOT NULL DEFAULT 'default',
    config          TEXT NOT NULL DEFAULT '{}',  -- JSON: non-sensitive settings
    secret_ref      TEXT NOT NULL DEFAULT '',    -- secret value; later vault path
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_elements_user_service_label
    ON auth_elements(user_id, service, label);

-- ============================================================
-- Provider Ratings: Tracks the auto-updated ratings of parallel providers
-- ============================================================
CREATE TABLE IF NOT EXISTS provider_ratings (
    user_id         TEXT NOT NULL,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    rating          INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, provider, model)
);

-- ============================================================
-- User Profiles: admin flag and per-user preferences
-- ============================================================
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id             TEXT PRIMARY KEY,
    is_admin            INTEGER NOT NULL DEFAULT 0,
    default_agent_id    TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at       TEXT
);

"""


# ── Slot helpers ──────────────────────────────────────────────────────────────
# Prompts are stored as rows in `agent_prompts`, one per (agent, slot_name, user_id).
# user_id IS NULL = admin base (canonical); non-null = override owned by that user.

VALID_MERGE_MODES = ("replace", "append")


def _legacy_default_slots() -> List[dict]:
    """Built-in slot defaults used when a template/agent has none defined yet."""
    return [
        {"slot_name": "system",      "order_index": 10, "lock": True,  "merge_mode": "replace"},
        {"slot_name": "agent",       "order_index": 20, "lock": False, "merge_mode": "replace"},
        {"slot_name": "user",        "order_index": 30, "lock": False, "merge_mode": "replace"},
        {"slot_name": "skills",      "order_index": 40, "lock": False, "merge_mode": "replace"},
        {"slot_name": "tasks",       "order_index": 50, "lock": False, "merge_mode": "replace"},
        {"slot_name": "misc",        "order_index": 60, "lock": False, "merge_mode": "replace"},
        {"slot_name": "bootstrap_tools", "order_index": 90, "lock": True, "merge_mode": "replace"},
    ]


def _slot_apply(base: str, override: Optional[str], lock: bool, merge_mode: str) -> str:
    """Resolve a single slot's effective content given admin base + optional user override."""
    base = base or ""
    if lock or override is None:
        return base
    if merge_mode == "append":
        if not base.strip():
            return override or ""
        if not (override or "").strip():
            return base
        return base.rstrip() + "\n\n" + override.lstrip()
    # default / "replace"
    return override


class LocalBackend(StorageBackend):
    """SQLite implementation of StorageBackend."""

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or DEFAULT_DB_PATH
        self._write_lock = asyncio.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a new connection (thread-safe: each call gets its own)."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        conn = self._get_conn()
        try:
            # ── Pre-migration: handle old agents table ──
            # Old schema (v1): (id TEXT PK DEFAULT 'default_agent', max_turn_count INT)
            # New schema (v2): (id TEXT PK, user_id TEXT NOT NULL UNIQUE, ...)
            # Detect old schema and rename before SCHEMA_SQL runs
            cursor = conn.execute("PRAGMA table_info(agents)")
            cols = {row[1] for row in cursor.fetchall()}
            # Only rename when truly ancient v1 schema (no user_id AND no admin_users)
            if cols and "user_id" not in cols and "admin_users" not in cols:
                # Drop stale agents_v1 if it exists from a previous partial run
                conn.execute("DROP TABLE IF EXISTS agents_v1")
                conn.execute("ALTER TABLE agents RENAME TO agents_v1")
                conn.commit()

            conn.executescript(SCHEMA_SQL)
            conn.commit()

            # ── Migration: add channel column to interactions ──
            cursor = conn.execute("PRAGMA table_info(interactions)")
            cols = {row[1] for row in cursor.fetchall()}
            if "channel" not in cols:
                conn.execute("ALTER TABLE interactions ADD COLUMN channel TEXT")
                conn.commit()

            # ── Migration: add source column to interactions (optimizer tracking) ──
            cursor = conn.execute("PRAGMA table_info(interactions)")
            cols2 = {row[1] for row in cursor.fetchall()}
            if "source" not in cols2:
                conn.execute("ALTER TABLE interactions ADD COLUMN source TEXT DEFAULT 'user'")
                conn.commit()
                logger.info("Added interactions.source column")

            # ── Migration: add output column to interactions ──
            cursor = conn.execute("PRAGMA table_info(interactions)")
            cols3 = {row[1] for row in cursor.fetchall()}
            if "output" not in cols3:
                conn.execute("ALTER TABLE interactions ADD COLUMN output TEXT")
                conn.commit()
                logger.info("Added interactions.output column")

            # ── Migration: add from_id and to_id columns to interactions ──
            cursor = conn.execute("PRAGMA table_info(interactions)")
            cols_ft = {row[1] for row in cursor.fetchall()}
            if "from_id" not in cols_ft:
                conn.execute("ALTER TABLE interactions ADD COLUMN from_id TEXT")
                conn.execute("ALTER TABLE interactions ADD COLUMN to_id TEXT")
                conn.commit()
                logger.info("Added interactions.from_id and to_id columns")

            # ── Migration: add metadata column to sessions (for optimizer tracking) ──
            cursor = conn.execute("PRAGMA table_info(sessions)")
            sess_cols = {row[1] for row in cursor.fetchall()}
            if "metadata" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN metadata TEXT")
                conn.commit()
                logger.info("Added sessions.metadata column")

            conn.commit()

            # ── Post-migration: move data from old agents_v1 ──
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='agents_v1'"
            )
            if cursor.fetchone():
                _mig_now = _now_iso()
                conn.execute(
                    """INSERT OR IGNORE INTO agents
                       (id, max_turn_count, status, assigned_at, created_at, updated_at)
                       SELECT id, max_turn_count, 'active',
                              ?, ?, ?
                       FROM agents_v1 WHERE id = 'default_agent'""",
                    (_mig_now, _mig_now, _mig_now),
                )
                conn.execute("DROP TABLE agents_v1")
                conn.commit()
                logger.info("Agents table migration complete")

            # Drop stale legacy context_defaults table if it still exists.
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_defaults'"
            ).fetchone():
                conn.execute("DROP TABLE context_defaults")
                conn.commit()
                logger.info("Dropped legacy context_defaults table")

            # ── Migration: add turn_count column to agents ──
            cursor = conn.execute("PRAGMA table_info(agents)")
            agent_cols = {row[1] for row in cursor.fetchall()}
            if "turn_count" not in agent_cols:
                conn.execute("ALTER TABLE agents ADD COLUMN turn_count INTEGER NOT NULL DEFAULT 0")
                conn.commit()
                logger.info("Added agents.turn_count column")

            # ── Migration: add agent_id column to sessions ──
            cursor = conn.execute("PRAGMA table_info(sessions)")
            sess_cols = {row[1] for row in cursor.fetchall()}
            if "agent_id" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN agent_id TEXT")
                conn.commit()
                logger.info("Added sessions.agent_id column")

            # ── Migration: add participants column to sessions ──
            cursor = conn.execute("PRAGMA table_info(sessions)")
            sess_cols = {row[1] for row in cursor.fetchall()}
            if "participants" not in sess_cols:
                conn.execute("ALTER TABLE sessions ADD COLUMN participants TEXT DEFAULT '[]'")
                conn.commit()
                logger.info("Added sessions.participants column")

            # ── Migration: add template_id column to agents ──
            cursor = conn.execute("PRAGMA table_info(agents)")
            agent_cols = {row[1] for row in cursor.fetchall()}
            if "template_id" not in agent_cols:
                conn.execute("ALTER TABLE agents ADD COLUMN template_id TEXT")
                conn.commit()
                logger.info("Added agents.template_id column")

            # ── Migration: add context columns to agent_templates ──
            cursor = conn.execute("PRAGMA table_info(agent_templates)")
            at_cols = {row[1] for row in cursor.fetchall()}
            for col in ("agent_prompt", "user_prompt", "skills_prompt", "tasks_prompt", "misc_prompt"):
                if col not in at_cols:
                    conn.execute(f"ALTER TABLE agent_templates ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            conn.commit()

            # ── Migration: add context columns to agents ──
            cursor = conn.execute("PRAGMA table_info(agents)")
            ag_cols = {row[1] for row in cursor.fetchall()}
            for col in ("agent_prompt", "user_prompt", "skills_prompt", "tasks_prompt", "misc_prompt"):
                if col not in ag_cols:
                    conn.execute(f"ALTER TABLE agents ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            conn.commit()

            # ── Migration: add bootstrap_tools column ──
            for tbl in ("agents", "agent_templates"):
                tbl_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
                if "bootstrap_tools" not in tbl_cols:
                    conn.execute(f"ALTER TABLE {tbl} ADD COLUMN bootstrap_tools TEXT NOT NULL DEFAULT ''")
                    logger.info("Added bootstrap_tools column to %s", tbl)
            conn.commit()

            # ── Migration: add tool metadata columns ──
            tool_cols = {row[1] for row in conn.execute("PRAGMA table_info(tools)").fetchall()}
            for col, default in [("stages", "'[]'"), ("destructive", "0"), ("agent_types", "'[]'")]:
                if col not in tool_cols:
                    conn.execute(f"ALTER TABLE tools ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")
                    logger.info("Added %s column to tools", col)
            conn.commit()

            # ── Migration: add allowed_tools and custom_tool_ids to agents ──
            ag_cols2 = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            for col, default in [("allowed_tools", "'[]'"), ("custom_tool_ids", "'[]'")]:
                if col not in ag_cols2:
                    conn.execute(f"ALTER TABLE agents ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")
                    logger.info("Added agents.%s column", col)
            conn.commit()

            # ── Migration: copy _ctx → _prompt and drop old _ctx columns ──
            _ctx_map = [
                ("agent_ctx",  "agent_prompt"),
                ("user_ctx",   "user_prompt"),
                ("skills_ctx", "skills_prompt"),
                ("tasks_ctx",  "tasks_prompt"),
                ("misc_ctx",   "misc_prompt"),
            ]
            for tbl in ("agents", "agent_templates"):
                tbl_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
                old_cols_present = [old for old, _ in _ctx_map if old in tbl_cols]
                if old_cols_present:
                    # Copy data: only overwrite _prompt where it's currently empty
                    for old_col, new_col in _ctx_map:
                        if old_col in tbl_cols:
                            conn.execute(
                                f"UPDATE {tbl} SET {new_col} = {old_col} "
                                f"WHERE ({new_col} IS NULL OR {new_col} = '') AND {old_col} != ''"
                            )
                    conn.commit()
                    # Drop the old columns (requires SQLite 3.35+)
                    for old_col, _ in _ctx_map:
                        if old_col in tbl_cols:
                            try:
                                conn.execute(f"ALTER TABLE {tbl} DROP COLUMN {old_col}")
                            except Exception as drop_err:
                                logger.warning("Could not drop %s.%s: %s", tbl, old_col, drop_err)
                    conn.commit()
                    logger.info("Dropped _ctx columns from %s", tbl)

            # ── Migration: add multi-agent fields to agent_templates ──
            at_cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_templates)").fetchall()}
            _at_new_cols = [
                ("name",          "TEXT NOT NULL DEFAULT ''"),
                ("description",   "TEXT NOT NULL DEFAULT ''"),
                ("icon",          "TEXT NOT NULL DEFAULT ''"),
                ("can_be_default","INTEGER NOT NULL DEFAULT 1"),
                ("is_system",     "INTEGER NOT NULL DEFAULT 0"),
                ("is_pipeline",   "INTEGER NOT NULL DEFAULT 0"),
                ("access_level",  "TEXT NOT NULL DEFAULT 'all'"),
                ("trigger_description", "TEXT NOT NULL DEFAULT ''"),
                ("discoverable",  "INTEGER NOT NULL DEFAULT 0"),
            ]
            discoverable_was_missing = "discoverable" not in at_cols
            for col, col_def in _at_new_cols:
                if col not in at_cols:
                    conn.execute(f"ALTER TABLE agent_templates ADD COLUMN {col} {col_def}")
                    logger.info("Added agent_templates.%s column", col)
            if discoverable_was_missing:
                # Seed the default template as discoverable on first migration
                conn.execute("UPDATE agent_templates SET discoverable = 1 WHERE id = 'default'")
                logger.info("Seeded discoverable=1 for default agent_template")
            conn.commit()

            # ── Migration: add trigger_type/trigger_key/loop_logic columns ──
            for tbl in ("agents", "agent_templates"):
                tbl_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
                for col, col_def in [
                    ("trigger_type", "TEXT NOT NULL DEFAULT 'user_input'"),
                    ("trigger_key",  "TEXT"),
                    ("loop_logic",   "TEXT NOT NULL DEFAULT '[]'"),
                ]:
                    if col not in tbl_cols:
                        conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {col_def}")
                        logger.info("Added %s.%s column", tbl, col)
            conn.commit()

            # ── Migration: add multi-agent fields to agents ──
            ag_cols = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            _ag_new_cols = [
                ("is_user_default","INTEGER NOT NULL DEFAULT 0"),
                ("name",           "TEXT NOT NULL DEFAULT ''"),
                ("description",    "TEXT NOT NULL DEFAULT ''"),
            ]
            for col, col_def in _ag_new_cols:
                if col not in ag_cols:
                    conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {col_def}")
                    logger.info("Added agents.%s column", col)
            conn.commit()

            # ── Migration: drop UNIQUE constraint on agents.user_id ──
            # Original schema enforced one agent per user. Multi-agent model
            # allows many rows per user_id; recreate table without UNIQUE.
            ag_sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='agents'"
            ).fetchone()
            if ag_sql_row and "user_id TEXT NOT NULL UNIQUE" in (ag_sql_row[0] or ""):
                cols = [r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall()]
                col_list = ", ".join(cols)
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.executescript(f"""
                    CREATE TABLE agents_new (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL,
                        template_id TEXT,
                        owner_user_id TEXT,
                        name TEXT NOT NULL DEFAULT '',
                        description TEXT NOT NULL DEFAULT '',
                        is_user_default INTEGER NOT NULL DEFAULT 0,
                        system_prompt TEXT NOT NULL DEFAULT '',
                        max_turn_count INTEGER NOT NULL DEFAULT 10,
                        model TEXT,
                        provider TEXT,
                        temperature REAL NOT NULL DEFAULT 0.0,
                        max_tokens INTEGER NOT NULL DEFAULT 4096,
                        status TEXT NOT NULL DEFAULT 'active',
                        metadata TEXT NOT NULL DEFAULT '{{}}',
                        agent_prompt TEXT NOT NULL DEFAULT '',
                        user_prompt TEXT NOT NULL DEFAULT '',
                        skills_prompt TEXT NOT NULL DEFAULT '',
                        tasks_prompt TEXT NOT NULL DEFAULT '',
                        misc_prompt TEXT NOT NULL DEFAULT '',
                        bootstrap_tools TEXT NOT NULL DEFAULT '',
                        assigned_at TEXT NOT NULL DEFAULT (datetime('now')),
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                        turn_count INTEGER NOT NULL DEFAULT 0
                    );
                    INSERT INTO agents_new ({col_list}) SELECT {col_list} FROM agents;
                    DROP TABLE agents;
                    ALTER TABLE agents_new RENAME TO agents;
                    CREATE INDEX IF NOT EXISTS idx_agents_user ON agents(user_id);
                """)
                conn.execute("PRAGMA foreign_keys = ON")
                conn.commit()
                logger.info("Dropped UNIQUE constraint on agents.user_id")

            # ── Migration: backfill admin_users for existing default agents (legacy path) ──
            _ag_cols_uid = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            if "owner_user_id" in _ag_cols_uid and "user_id" in _ag_cols_uid:
                conn.execute(
                    """UPDATE agents SET owner_user_id = user_id
                       WHERE owner_user_id IS NULL
                       AND (template_id = 'default' OR template_id IS NULL)
                       AND user_id NOT LIKE 'opt_%'"""
                )
                conn.commit()

            # ── Migration: set is_user_default=1 for existing default agents ──
            conn.execute(
                """UPDATE agents SET is_user_default = 1
                   WHERE (template_id = 'default' OR template_id IS NULL)
                   AND is_user_default = 0"""
            )
            conn.commit()

            # ── Migration 010: backfill agents.name for user-owned agents ──
            # Any agent with owner_user_id set but no name yet gets the default 'autoAgent'.
            conn.execute(
                """UPDATE agents SET name = 'autoAgent'
                   WHERE (name IS NULL OR name = '')"""
            )
            conn.commit()
            logger.info("Backfilled agents.name = 'autoAgent' for user-owned agents")

            # ── Migration 011: add last_login_at to user_profiles ──
            cursor = conn.execute("PRAGMA table_info(user_profiles)")
            up_cols = {row[1] for row in cursor.fetchall()}
            if "last_login_at" not in up_cols:
                conn.execute("ALTER TABLE user_profiles ADD COLUMN last_login_at TEXT")
                conn.commit()
                logger.info("Added user_profiles.last_login_at column")

            # ── Migration 012: clear hardcoded model from user agents ──
            # Backend ignores agent.model; display now shows "default" when null.
            # Clear any stored model so existing agents show "default" in the UI.
            conn.execute(
                """UPDATE agents SET model = NULL
                   WHERE model IS NOT NULL"""
            )
            conn.commit()
            logger.info("Cleared agent.model for user-owned agents (now show 'default')")

            # ── Migration 013: add safety_policy to agents + requires_confirmation to tools ──
            ag_cols_013 = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            if "safety_policy" not in ag_cols_013:
                conn.execute("ALTER TABLE agents ADD COLUMN safety_policy TEXT NOT NULL DEFAULT '{}'")
                logger.info("Added agents.safety_policy column")
            tool_cols_013 = {row[1] for row in conn.execute("PRAGMA table_info(tools)").fetchall()}
            if "requires_confirmation" not in tool_cols_013:
                conn.execute("ALTER TABLE tools ADD COLUMN requires_confirmation INTEGER NOT NULL DEFAULT 0")
                logger.info("Added tools.requires_confirmation column")
            conn.commit()

            # ── Migration 014: add is_admin_agent to agent_templates + agents ──
            tpl_cols_014 = {row[1] for row in conn.execute("PRAGMA table_info(agent_templates)").fetchall()}
            if "is_admin_agent" not in tpl_cols_014:
                conn.execute("ALTER TABLE agent_templates ADD COLUMN is_admin_agent INTEGER NOT NULL DEFAULT 0")
                logger.info("Added agent_templates.is_admin_agent column")
            ag_cols_014 = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            if "is_admin_agent" not in ag_cols_014:
                conn.execute("ALTER TABLE agents ADD COLUMN is_admin_agent INTEGER NOT NULL DEFAULT 0")
                logger.info("Added agents.is_admin_agent column")
            conn.commit()

            # ── Seed: ensure admin_default always has is_admin=1 ──
            _mig_now2 = _now_iso()
            conn.execute(
                """INSERT INTO user_profiles (user_id, is_admin, created_at, updated_at)
                   VALUES ('admin_default', 1, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET is_admin = 1""",
                (_mig_now2, _mig_now2),
            )
            conn.commit()
            logger.info("Ensured admin_default is_admin=1")

            # ── Migration 015: backfill is_admin_agent=1 on agents rows from admin templates ──
            # Any row in the agents table whose template_id maps to an admin-flagged template
            # should inherit is_admin_agent=1.  We backfill by cross-referencing agent_templates.
            conn.execute(
                """UPDATE agents
                   SET is_admin_agent = 1
                   WHERE is_admin_agent = 0
                     AND template_id IN (
                         SELECT id FROM agent_templates WHERE is_admin_agent = 1
                     )"""
            )
            # Also directly flag any row whose template_id is 'admin-agent' in case the
            # agent_templates row hasn't been re-seeded yet during this startup pass.
            conn.execute(
                "UPDATE agents SET is_admin_agent = 1 WHERE template_id = 'admin-agent' AND is_admin_agent = 0"
            )
            conn.commit()
            logger.info("Migration 015: backfilled is_admin_agent on agents rows")

            # ── Migration 016: add admin_users + member_users to agents ──
            ag_cols_016 = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            if "admin_users" not in ag_cols_016:
                conn.execute("ALTER TABLE agents ADD COLUMN admin_users TEXT NOT NULL DEFAULT '[]'")
                logger.info("Added agents.admin_users column")
            if "member_users" not in ag_cols_016:
                conn.execute("ALTER TABLE agents ADD COLUMN member_users TEXT NOT NULL DEFAULT '[]'")
                logger.info("Added agents.member_users column")
            # Backfill: agent owner becomes the first admin
            _ag_cols_016b = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            if "user_id" in _ag_cols_016b:
                conn.execute(
                    """UPDATE agents SET admin_users = json_array(user_id)
                       WHERE admin_users = '[]' AND user_id IS NOT NULL AND user_id != ''"""
                )
            elif "owner_user_id" in _ag_cols_016b:
                conn.execute(
                    """UPDATE agents SET admin_users = json_array(owner_user_id)
                       WHERE admin_users = '[]' AND owner_user_id IS NOT NULL AND owner_user_id != ''"""
                )
            conn.commit()
            logger.info("Migration 016: added admin_users/member_users, backfilled agent owners as admins")

            # ── Migration 017: add user_mode to agents ──
            ag_cols_017 = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            if "user_mode" not in ag_cols_017:
                conn.execute("ALTER TABLE agents ADD COLUMN user_mode TEXT NOT NULL DEFAULT 'anonymous'")
                conn.commit()
                logger.info("Added agents.user_mode column")

            # ── Migration 018: drop agents.user_id and owner_user_id columns ──
            ag_cols_018 = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
            _cols_to_drop_018 = [c for c in ("user_id", "owner_user_id") if c in ag_cols_018]
            if _cols_to_drop_018:
                try:
                    conn.execute("DROP INDEX IF EXISTS idx_agents_user")
                    for _col in _cols_to_drop_018:
                        conn.execute(f"ALTER TABLE agents DROP COLUMN {_col}")
                    conn.commit()
                    logger.info("Migration 018: dropped agents columns: %s", _cols_to_drop_018)
                except Exception as _e018:
                    logger.warning("Migration 018: could not drop via ALTER TABLE DROP COLUMN (%s); falling back to table recreation", _e018)
                    conn.rollback()
                    # Build SELECT list from columns that exist (minus the ones to drop)
                    _keep_018 = [r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall() if r[1] not in ("user_id", "owner_user_id")]
                    col_list_018 = ", ".join(_keep_018)
                    conn.execute("PRAGMA foreign_keys = OFF")
                    # Use explicit DDL so PRIMARY KEY is preserved (CREATE TABLE ... AS SELECT drops constraints)
                    conn.executescript(f"""
                        CREATE TABLE agents_018 (
                            id TEXT PRIMARY KEY,
                            template_id TEXT,
                            name TEXT NOT NULL DEFAULT '',
                            description TEXT NOT NULL DEFAULT '',
                            is_user_default INTEGER NOT NULL DEFAULT 0,
                            system_prompt TEXT NOT NULL DEFAULT '',
                            max_turn_count INTEGER NOT NULL DEFAULT 10,
                            model TEXT,
                            provider TEXT,
                            temperature REAL NOT NULL DEFAULT 0.0,
                            max_tokens INTEGER NOT NULL DEFAULT 4096,
                            status TEXT NOT NULL DEFAULT 'active',
                            metadata TEXT NOT NULL DEFAULT '{{}}',
                            agent_prompt TEXT NOT NULL DEFAULT '',
                            user_prompt TEXT NOT NULL DEFAULT '',
                            skills_prompt TEXT NOT NULL DEFAULT '',
                            tasks_prompt TEXT NOT NULL DEFAULT '',
                            misc_prompt TEXT NOT NULL DEFAULT '',
                            bootstrap_tools TEXT NOT NULL DEFAULT '',
                            allowed_tools TEXT NOT NULL DEFAULT '[]',
                            custom_tool_ids TEXT NOT NULL DEFAULT '[]',
                            trigger_type TEXT NOT NULL DEFAULT 'user_input',
                            trigger_key TEXT,
                            loop_logic TEXT NOT NULL DEFAULT '[]',
                            safety_policy TEXT NOT NULL DEFAULT '{{}}',
                            is_admin_agent INTEGER NOT NULL DEFAULT 0,
                            assigned_at TEXT NOT NULL DEFAULT (datetime('now')),
                            created_at TEXT NOT NULL DEFAULT (datetime('now')),
                            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                            turn_count INTEGER NOT NULL DEFAULT 0,
                            admin_users TEXT NOT NULL DEFAULT '[]',
                            member_users TEXT NOT NULL DEFAULT '[]',
                            user_mode TEXT NOT NULL DEFAULT 'anonymous'
                        );
                        INSERT INTO agents_018 ({col_list_018}) SELECT {col_list_018} FROM agents;
                        DROP TABLE agents;
                        ALTER TABLE agents_018 RENAME TO agents;
                    """)
                    conn.execute("PRAGMA foreign_keys = ON")
                    conn.commit()
                    logger.info("Migration 018 (fallback): recreated agents table without user_id/owner_user_id")

            # ── Migration 019: repair agents table if PRIMARY KEY was lost by migration 018 fallback ──
            # The CREATE TABLE ... AS SELECT fallback drops all constraints; check and fix.
            _pk_col_019 = next(
                (r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall() if r[5] == 1),
                None,
            )
            if _pk_col_019 != "id":
                logger.warning("Migration 019: agents.id is not PRIMARY KEY (pk=%s); recreating table", _pk_col_019)
                _keep_019 = [r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall() if r[1] not in ("user_id", "owner_user_id")]
                col_list_019 = ", ".join(_keep_019)
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.executescript(f"""
                    CREATE TABLE agents_019 (
                        id TEXT PRIMARY KEY,
                        template_id TEXT,
                        name TEXT NOT NULL DEFAULT '',
                        description TEXT NOT NULL DEFAULT '',
                        is_user_default INTEGER NOT NULL DEFAULT 0,
                        system_prompt TEXT NOT NULL DEFAULT '',
                        max_turn_count INTEGER NOT NULL DEFAULT 10,
                        model TEXT,
                        provider TEXT,
                        temperature REAL NOT NULL DEFAULT 0.0,
                        max_tokens INTEGER NOT NULL DEFAULT 4096,
                        status TEXT NOT NULL DEFAULT 'active',
                        metadata TEXT NOT NULL DEFAULT '{{}}',
                        agent_prompt TEXT NOT NULL DEFAULT '',
                        user_prompt TEXT NOT NULL DEFAULT '',
                        skills_prompt TEXT NOT NULL DEFAULT '',
                        tasks_prompt TEXT NOT NULL DEFAULT '',
                        misc_prompt TEXT NOT NULL DEFAULT '',
                        bootstrap_tools TEXT NOT NULL DEFAULT '',
                        allowed_tools TEXT NOT NULL DEFAULT '[]',
                        custom_tool_ids TEXT NOT NULL DEFAULT '[]',
                        trigger_type TEXT NOT NULL DEFAULT 'user_input',
                        trigger_key TEXT,
                        loop_logic TEXT NOT NULL DEFAULT '[]',
                        safety_policy TEXT NOT NULL DEFAULT '{{}}',
                        is_admin_agent INTEGER NOT NULL DEFAULT 0,
                        assigned_at TEXT NOT NULL DEFAULT (datetime('now')),
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                        turn_count INTEGER NOT NULL DEFAULT 0,
                        admin_users TEXT NOT NULL DEFAULT '[]',
                        member_users TEXT NOT NULL DEFAULT '[]',
                        user_mode TEXT NOT NULL DEFAULT 'anonymous'
                    );
                    INSERT INTO agents_019 ({col_list_019}) SELECT {col_list_019} FROM agents;
                    DROP TABLE agents;
                    ALTER TABLE agents_019 RENAME TO agents;
                """)
                conn.execute("PRAGMA foreign_keys = ON")
                conn.commit()
                logger.info("Migration 019: agents table repaired with proper PRIMARY KEY")

            # ── Migration 021: drop legacy prompt columns from agents + agent_templates ──
            # All prompts now live in agent_prompts (one row per slot, with optional
            # per-user override rows). The old per-table prompt columns are removed.
            _legacy_prompt_cols = (
                "system_prompt", "agent_prompt", "user_prompt",
                "skills_prompt", "tasks_prompt", "misc_prompt", "bootstrap_tools",
            )

            def _drop_legacy_prompt_cols(table: str) -> None:
                tbl_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                to_drop = [c for c in _legacy_prompt_cols if c in tbl_cols]
                if not to_drop:
                    return
                for col in to_drop:
                    try:
                        conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
                    except Exception as drop_err:
                        logger.warning("Could not drop %s.%s: %s", table, col, drop_err)
                conn.commit()
                logger.info("Migration 021: dropped legacy prompt columns from %s: %s", table, to_drop)

            _drop_legacy_prompt_cols("agents")
            _drop_legacy_prompt_cols("agent_templates")

            # Ensure agent_prompts table exists (SCHEMA_SQL already covers this, but
            # be defensive in case this migration runs on a DB that pre-dates the schema change).
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_prompts (
                    id              TEXT PRIMARY KEY,
                    agent_id        TEXT NOT NULL,
                    slot_name       TEXT NOT NULL,
                    user_id         TEXT,
                    order_index     INTEGER,
                    lock            INTEGER,
                    merge_mode      TEXT,
                    content         TEXT NOT NULL DEFAULT '',
                    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_by      TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_prompts_slot_user
                    ON agent_prompts(agent_id, slot_name, IFNULL(user_id, ''));
                CREATE INDEX IF NOT EXISTS idx_agent_prompts_agent ON agent_prompts(agent_id);
                CREATE INDEX IF NOT EXISTS idx_agent_prompts_user  ON agent_prompts(user_id);
                """
            )
            conn.commit()

            # ── Migration 022: drop context_documents + context_templates tables ──
            # Prompts now live in agent_prompts (per-slot rows). Optimizer prompt
            # loader reads from app/context/optimizer_prompts/ markdown files.
            for _tbl in ("context_documents", "context_templates"):
                try:
                    conn.execute(f"DROP TABLE IF EXISTS {_tbl}")
                except Exception as _drop_err:
                    logger.warning("Migration 022: could not drop %s: %s", _tbl, _drop_err)
            conn.commit()

            # ── Migration 020: fix agent_connections FK if it references stale agents_v1 ──
            # SQLite auto-updates FK refs when a table is renamed with foreign_keys=ON.
            # If agents was renamed → agents_v1 during pre-migration, agent_connections
            # ends up with REFERENCES agents_v1(id) instead of agents(id).
            _ac_fk_rows = conn.execute("PRAGMA foreign_key_list(agent_connections)").fetchall()
            _ac_fk_wrong = any(
                r[2] != "agents" for r in _ac_fk_rows if r[3] == "agent_id"
            )
            if _ac_fk_wrong or not any(r[3] == "agent_id" for r in _ac_fk_rows):
                logger.warning("Migration 020: agent_connections FK is stale; recreating table")
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.executescript("""
                    CREATE TABLE agent_connections_020 (
                        id              TEXT PRIMARY KEY,
                        agent_id        TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                        connection_type TEXT NOT NULL,
                        section         TEXT NOT NULL DEFAULT 'channel',
                        enabled         INTEGER NOT NULL DEFAULT 0,
                        config          TEXT NOT NULL DEFAULT '{}',
                        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
                        UNIQUE(agent_id, connection_type)
                    );
                    INSERT OR IGNORE INTO agent_connections_020
                        SELECT id, agent_id, connection_type, section, enabled, config, created_at, updated_at
                        FROM agent_connections;
                    DROP TABLE agent_connections;
                    ALTER TABLE agent_connections_020 RENAME TO agent_connections;
                    CREATE INDEX IF NOT EXISTS idx_agent_conn_agent ON agent_connections(agent_id);
                    CREATE INDEX IF NOT EXISTS idx_agent_conn_type  ON agent_connections(connection_type);
                """)
                conn.execute("PRAGMA foreign_keys = ON")
                conn.commit()
                logger.info("Migration 020: agent_connections FK fixed to reference agents")
            # Clean up any stale agents_v1 left by the pre-migration rename
            _has_agents_v1 = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agents_v1'"
            ).fetchone()
            if _has_agents_v1:
                conn.execute("DROP TABLE agents_v1")
                conn.commit()
                logger.info("Migration 020: dropped stale agents_v1 table")

            # ── Seed: agent templates from app/context/agents/*.json (full schema) ──
            self._seed_agent_templates_from_json_files(conn)

        except Exception as e:
            logger.error("Error initializing local database: %s", e)
            raise
        finally:
            conn.close()

    # ---- Raw client access ----

    def get_raw_client(self) -> Any:
        """
        Return a proxy object compatible with supabase.Client's table() method.
        This allows code that uses the Supabase query builder directly
        (ToolLoader, ToolExecutionTracker, etc.) to work with minimal changes.
        """
        return _LocalTableProxy(self._db_path)

    # ---- Sessions ----

    async def assert_session_owned(self, user_id: str, session_id: str) -> None:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT id FROM sessions WHERE id = ? AND user_id = ?",
                (session_id, user_id),
            ).fetchone()
            if not row:
                raise PermissionError(
                    f"Session {session_id} not found or not owned by user {user_id}"
                )
        finally:
            conn.close()

    async def upsert_session_summary(
        self,
        user_id: str,
        session_id: str,
        summary: str,
        message_count: int,
        title: Optional[str] = None,
    ) -> None:
        conn = self._get_conn()
        try:
            existing = conn.execute(
                "SELECT id FROM session_summaries WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            now = _now_iso()
            if existing:
                conn.execute(
                    """UPDATE session_summaries
                       SET summary = ?, message_count = ?, title = COALESCE(?, title),
                           updated_at = ?
                       WHERE session_id = ?""",
                    (summary, message_count, title, now, session_id),
                )
            else:
                conn.execute(
                    """INSERT INTO session_summaries (id, user_id, session_id, title, summary, message_count, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (_uuid(), user_id, session_id, title, summary, message_count, now),
                )
            conn.commit()
            logger.debug("Upserted session summary for session %s", session_id)
        except Exception as e:
            logger.error("Error upserting session summary: %s", e)
            raise
        finally:
            conn.close()

    # ---- Interactions ----

    async def fetch_interactions(
        self, user_id: str, session_id: str
    ) -> List[InteractionRecord]:
        await self.assert_session_owned(user_id, session_id)
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, session_id, parent_id, role, content, tool_name, tool_call_id, channel, metadata, input, output, from_id, to_id, source, created_at FROM interactions WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
            return [InteractionRecord(**dict(r)) for r in rows]
        finally:
            conn.close()

    async def insert_interaction(
        self,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
        parent_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        channel: Optional[str] = None,
        metadata: Optional[str] = None,
        input_data: Optional[str] = None,
        output_data: Optional[str] = None,
        sender_id: Optional[str] = None,
        receiver_id: Optional[str] = None,
        source: Optional[str] = None,
    ) -> str:
        await self.assert_session_owned(user_id, session_id)
        # Optimizer/Finalizer sessions get source='optimizer' for all interactions
        if source is None and (session_id.startswith('optimizer-') or session_id.startswith('closer-')):
            source = 'optimizer'
        async with self._write_lock:
            conn = self._get_conn()
            try:
                interaction_id = _uuid()
                conn.execute(
                    "INSERT INTO interactions (id, session_id, parent_id, role, content, tool_name, tool_call_id, channel, metadata, input, output, source, from_id, to_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (interaction_id, session_id, parent_id, role, content, tool_name, tool_call_id, channel, metadata, input_data, output_data, source or 'user', sender_id, receiver_id),
                )
                conn.commit()
                logger.debug("Inserted interaction %s", interaction_id)
                return interaction_id
            except Exception as e:
                logger.error("Error inserting interaction: %s", e)
                raise
            finally:
                conn.close()

    async def copy_defaults_to_agent(self, agent_id: str, template_id: Optional[str] = None) -> int:
        """
        Copy admin-base prompt slots from the 'default' template into this agent.
        Only copies if the agent has no admin-base slot rows yet.

        Isolation gate: non-default templates do NOT inherit webAgent context.
        """
        # Isolation gate: only 'default' templates inherit webAgent context
        if template_id is not None and template_id != "default":
            logger.info(
                "Skipping context copy for template_id=%s (agent=%s)",
                template_id, agent_id[:8],
            )
            return 0
        conn = self._get_conn()
        try:
            existing = conn.execute(
                "SELECT 1 FROM agent_prompts WHERE agent_id = ? AND user_id IS NULL LIMIT 1",
                (agent_id,),
            ).fetchone()
            if existing:
                logger.debug("Agent %s already has slot rows — skipping copy", agent_id[:8])
                return 0

            tpl_rows = conn.execute(
                """SELECT slot_name, order_index, lock, merge_mode, content
                   FROM agent_prompts
                   WHERE agent_id = 'default' AND user_id IS NULL""",
            ).fetchall()
            if not tpl_rows:
                logger.warning("No default template slots found for context copy")
                return 0

            now = _now_iso()
            for row in tpl_rows:
                conn.execute(
                    """INSERT INTO agent_prompts
                       (id, agent_id, slot_name, user_id, order_index, lock, merge_mode, content, updated_at, updated_by)
                       VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 'system')""",
                    (_uuid(), agent_id, row["slot_name"], row["order_index"],
                     row["lock"], row["merge_mode"], row["content"], now),
                )
            conn.execute("UPDATE agents SET updated_at = ? WHERE id = ?", (now, agent_id))
            conn.commit()
            logger.info(
                "Copied %d slots from default template to agent %s",
                len(tpl_rows), agent_id[:8],
            )
            return len(tpl_rows)
        except Exception as e:
            logger.error("Error copying defaults to agent: %s", e)
            raise
        finally:
            conn.close()

    def _seed_agent_templates_from_json_files(self, conn: sqlite3.Connection) -> None:
        """
        Scan app/context/agents/*.json and upsert each into
        agent_templates table (config fields only) plus admin-base slot rows
        in agent_prompts keyed by agent_id = template_id.

        JSON files may declare slots explicitly via a `slots` array; if absent,
        legacy keys (agent_prompt/user_prompt/skills_prompt/tasks_prompt/
        misc_prompt/system_prompt/bootstrap_tools) are converted into slots
        using a sensible default order.
        """
        from app.context.md_seeder import scan_agent_json_files
        templates = scan_agent_json_files()
        if not templates:
            return
        now = _now_iso()
        for tpl in templates:
            conn.execute(
                """INSERT INTO agent_templates
                   (id, name, description, icon, max_turn_count, model, provider,
                    temperature, max_tokens, metadata,
                    can_be_default, is_system, is_pipeline, access_level,
                    is_admin_agent, discoverable, trigger_type, trigger_key, loop_logic,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    icon = excluded.icon,
                    max_turn_count = excluded.max_turn_count,
                    model = excluded.model,
                    provider = excluded.provider,
                    temperature = excluded.temperature,
                    max_tokens = excluded.max_tokens,
                    metadata = excluded.metadata,
                    can_be_default = excluded.can_be_default,
                    is_system = excluded.is_system,
                    is_pipeline = excluded.is_pipeline,
                    access_level = excluded.access_level,
                    is_admin_agent = excluded.is_admin_agent,
                    trigger_type = excluded.trigger_type,
                    trigger_key = excluded.trigger_key,
                    loop_logic = excluded.loop_logic,
                    updated_at = excluded.updated_at""",
                (tpl["id"], tpl.get("name", tpl["id"]), tpl.get("description", ""),
                 tpl.get("icon", ""), tpl["max_turn_count"],
                 tpl["model"], tpl["provider"], tpl["temperature"],
                 tpl["max_tokens"], tpl["metadata"],
                 tpl.get("can_be_default", 1), tpl.get("is_system", 0),
                 tpl.get("is_pipeline", 0), tpl.get("access_level", "all"),
                 1 if tpl.get("is_admin_agent") else 0,
                 1 if tpl.get("discoverable") else 0,
                 tpl.get("trigger_type", "user_input"), tpl.get("trigger_key"),
                 tpl.get("loop_logic", "[]"),
                 now, now),
            )

            # Seed slot rows under agent_id = template_id (user_id IS NULL = admin base).
            slots = self._slots_from_template_data(tpl)
            # Wipe existing template base slots before re-seeding so the JSON wins.
            conn.execute(
                "DELETE FROM agent_prompts WHERE agent_id = ? AND user_id IS NULL",
                (tpl["id"],),
            )
            for s in slots:
                conn.execute(
                    """INSERT INTO agent_prompts
                       (id, agent_id, slot_name, user_id, order_index, lock, merge_mode, content, updated_at, updated_by)
                       VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 'system')""",
                    (_uuid(), tpl["id"], s["slot_name"], s["order_index"],
                     1 if s.get("lock") else 0,
                     s.get("merge_mode", "replace"),
                     s.get("content", ""), now),
                )
        conn.commit()
        logger.info(
            "Upserted %d agent template(s) (with slot rows) from app/context/agents/*.json",
            len(templates),
        )

    @staticmethod
    def _slots_from_template_data(tpl: dict) -> List[dict]:
        """Build a list of slot dicts from a template JSON.

        Prefers an explicit `slots` array. Falls back to converting the legacy
        keys (system_prompt, agent_prompt, user_prompt, skills_prompt,
        tasks_prompt, misc_prompt, bootstrap_tools) into slots.
        """
        raw_slots = tpl.get("slots")
        if isinstance(raw_slots, list) and raw_slots:
            out: List[dict] = []
            for i, s in enumerate(raw_slots):
                if not isinstance(s, dict):
                    continue
                name = (s.get("slot_name") or "").strip()
                if not name:
                    continue
                out.append({
                    "slot_name": name,
                    "order_index": int(s.get("order_index", (i + 1) * 10)),
                    "lock": bool(s.get("lock", False)),
                    "merge_mode": s.get("merge_mode") if s.get("merge_mode") in VALID_MERGE_MODES else "replace",
                    "content": s.get("content", "") or "",
                })
            return out

        # Legacy: derive slots from flat keys.
        legacy_map = [
            ("system",          "system_prompt",    10, True),
            ("agent",           "agent_prompt",     20, False),
            ("user",            "user_prompt",      30, False),
            ("skills",          "skills_prompt",    40, False),
            ("tasks",           "tasks_prompt",     50, False),
            ("misc",            "misc_prompt",      60, False),
            ("bootstrap_tools", "bootstrap_tools",  90, True),
        ]
        out = []
        for slot_name, src_key, order, lock in legacy_map:
            content = tpl.get(src_key, "") or ""
            out.append({
                "slot_name": slot_name,
                "order_index": order,
                "lock": lock,
                "merge_mode": "replace",
                "content": content,
            })
        return out

    def _clone_template_slots(
        self,
        conn: sqlite3.Connection,
        source_id: str,
        target_id: str,
        now: Optional[str] = None,
    ) -> int:
        """Copy admin-base slot rows from a template (or another agent) to a new agent.

        Falls back to the 'default' template if no slots exist for source_id.
        """
        now = now or _now_iso()
        rows = conn.execute(
            """SELECT slot_name, order_index, lock, merge_mode, content
               FROM agent_prompts WHERE agent_id = ? AND user_id IS NULL
               ORDER BY order_index""",
            (source_id,),
        ).fetchall()
        if not rows and source_id != "default":
            rows = conn.execute(
                """SELECT slot_name, order_index, lock, merge_mode, content
                   FROM agent_prompts WHERE agent_id = 'default' AND user_id IS NULL
                   ORDER BY order_index""",
            ).fetchall()
        for r in rows:
            conn.execute(
                """INSERT INTO agent_prompts
                   (id, agent_id, slot_name, user_id, order_index, lock, merge_mode, content, updated_at, updated_by)
                   VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, 'system')""",
                (_uuid(), target_id, r["slot_name"], r["order_index"],
                 r["lock"], r["merge_mode"], r["content"], now),
            )
        return len(rows)

    # ---- Prompt slots: read / write / resolve ------------------------------

    async def list_slots(self, agent_id: str, user_id: Optional[str] = None) -> List[dict]:
        """Return admin-base slot rows for an agent, ordered by order_index.

        If user_id is provided, each row gains an `override_content` field
        (or None) showing the caller's user override, if any.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT id, slot_name, order_index, lock, merge_mode, content,
                          updated_at, updated_by
                   FROM agent_prompts
                   WHERE agent_id = ? AND user_id IS NULL
                   ORDER BY order_index ASC""",
                (agent_id,),
            ).fetchall()
            result: List[dict] = []
            override_map: Dict[str, str] = {}
            if user_id:
                ov_rows = conn.execute(
                    """SELECT slot_name, content FROM agent_prompts
                       WHERE agent_id = ? AND user_id = ?""",
                    (agent_id, user_id),
                ).fetchall()
                override_map = {r["slot_name"]: r["content"] for r in ov_rows}
            for r in rows:
                d = dict(r)
                d["lock"] = bool(d.get("lock"))
                if user_id:
                    d["override_content"] = override_map.get(d["slot_name"])
                result.append(d)
            return result
        finally:
            conn.close()

    async def resolve_prompts(self, agent_id: str, user_id: Optional[str] = None) -> List[dict]:
        """Return resolved per-slot content for an agent + caller.

        Each entry: {slot_name, order_index, lock, merge_mode, content, used_override}.
        Locked slots ignore overrides. Unlocked slots apply replace/append based
        on the slot's merge_mode.
        """
        slots = await self.list_slots(agent_id, user_id=user_id)
        out: List[dict] = []
        for s in slots:
            base = s.get("content", "") or ""
            override = s.get("override_content") if user_id else None
            lock = bool(s.get("lock"))
            mode = s.get("merge_mode") if s.get("merge_mode") in VALID_MERGE_MODES else "replace"
            resolved = _slot_apply(base, override, lock, mode)
            out.append({
                "slot_name": s["slot_name"],
                "order_index": s["order_index"],
                "lock": lock,
                "merge_mode": mode,
                "content": resolved,
                "used_override": (not lock and override is not None),
            })
        return out

    async def assemble_prompt(self, agent_id: str, user_id: Optional[str] = None) -> str:
        """Return all resolved slot content concatenated in order. Empty slots are skipped."""
        slots = await self.resolve_prompts(agent_id, user_id=user_id)
        parts: List[str] = []
        for s in slots:
            txt = (s.get("content") or "").strip()
            if txt:
                parts.append(txt)
        return "\n\n".join(parts)

    async def upsert_slot(
        self,
        agent_id: str,
        slot_name: str,
        order_index: int,
        lock: bool,
        merge_mode: str,
        content: str,
        updated_by: str = "admin",
    ) -> dict:
        """Insert or update an admin-base slot row (user_id IS NULL)."""
        if merge_mode not in VALID_MERGE_MODES:
            merge_mode = "replace"
        slot_name = (slot_name or "").strip()
        if not slot_name:
            raise ValueError("slot_name required")
        now = _now_iso()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                existing = conn.execute(
                    """SELECT id FROM agent_prompts
                       WHERE agent_id = ? AND slot_name = ? AND user_id IS NULL""",
                    (agent_id, slot_name),
                ).fetchone()
                if existing:
                    conn.execute(
                        """UPDATE agent_prompts SET
                              order_index = ?, lock = ?, merge_mode = ?,
                              content = ?, updated_at = ?, updated_by = ?
                           WHERE id = ?""",
                        (int(order_index), 1 if lock else 0, merge_mode,
                         content or "", now, updated_by, existing["id"]),
                    )
                    row_id = existing["id"]
                else:
                    row_id = _uuid()
                    conn.execute(
                        """INSERT INTO agent_prompts
                           (id, agent_id, slot_name, user_id, order_index, lock, merge_mode,
                            content, updated_at, updated_by)
                           VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)""",
                        (row_id, agent_id, slot_name, int(order_index),
                         1 if lock else 0, merge_mode, content or "", now, updated_by),
                    )
                conn.commit()
                return {
                    "id": row_id,
                    "agent_id": agent_id,
                    "slot_name": slot_name,
                    "order_index": int(order_index),
                    "lock": bool(lock),
                    "merge_mode": merge_mode,
                    "content": content or "",
                    "updated_at": now,
                    "updated_by": updated_by,
                }
            finally:
                conn.close()

    async def delete_slot(self, agent_id: str, slot_name: str) -> int:
        """Delete an admin-base slot AND every per-user override for that slot."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    "DELETE FROM agent_prompts WHERE agent_id = ? AND slot_name = ?",
                    (agent_id, slot_name),
                )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    async def reset_overrides(self, agent_id: str, slot_name: str) -> int:
        """Delete all user override rows for a slot. Leaves the admin base intact."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    """DELETE FROM agent_prompts
                       WHERE agent_id = ? AND slot_name = ? AND user_id IS NOT NULL""",
                    (agent_id, slot_name),
                )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    async def upsert_override(
        self,
        agent_id: str,
        slot_name: str,
        user_id: str,
        content: str,
        updated_by: str = "user",
    ) -> Optional[dict]:
        """Insert or update the caller's override row for a slot.

        Refuses (returns None) if the slot is admin-locked or undefined.
        """
        if not user_id:
            raise ValueError("user_id required for override write")
        now = _now_iso()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                base = conn.execute(
                    """SELECT lock FROM agent_prompts
                       WHERE agent_id = ? AND slot_name = ? AND user_id IS NULL""",
                    (agent_id, slot_name),
                ).fetchone()
                if not base:
                    return None
                if int(base["lock"] or 0) == 1:
                    return None
                existing = conn.execute(
                    """SELECT id FROM agent_prompts
                       WHERE agent_id = ? AND slot_name = ? AND user_id = ?""",
                    (agent_id, slot_name, user_id),
                ).fetchone()
                if existing:
                    conn.execute(
                        """UPDATE agent_prompts SET content = ?, updated_at = ?, updated_by = ?
                           WHERE id = ?""",
                        (content or "", now, updated_by, existing["id"]),
                    )
                    row_id = existing["id"]
                else:
                    row_id = _uuid()
                    conn.execute(
                        """INSERT INTO agent_prompts
                           (id, agent_id, slot_name, user_id, content, updated_at, updated_by)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (row_id, agent_id, slot_name, user_id, content or "", now, updated_by),
                    )
                conn.commit()
                return {
                    "id": row_id,
                    "agent_id": agent_id,
                    "slot_name": slot_name,
                    "user_id": user_id,
                    "content": content or "",
                    "updated_at": now,
                    "updated_by": updated_by,
                }
            finally:
                conn.close()

    async def delete_override(self, agent_id: str, slot_name: str, user_id: str) -> bool:
        """Remove the caller's override row for a slot. Returns True if a row was deleted."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                cur = conn.execute(
                    """DELETE FROM agent_prompts
                       WHERE agent_id = ? AND slot_name = ? AND user_id = ?""",
                    (agent_id, slot_name, user_id),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def replace_slots(
        self,
        agent_id: str,
        slots: List[dict],
        reset_overrides_for: Optional[List[str]] = None,
        updated_by: str = "admin",
    ) -> List[dict]:
        """Reconcile an agent's admin-base slot set against a desired list.

        - Slots in `slots` are upserted (matched by slot_name).
        - Admin-base rows whose slot_name is no longer in the payload are deleted
          (which cascades — also drops the slot's per-user overrides).
        - `reset_overrides_for` is a list of slot_names whose user overrides
          should be wiped at save time.
        Returns the new resolved admin-base list.
        """
        desired_names = {(s.get("slot_name") or "").strip() for s in slots if isinstance(s, dict)}
        desired_names.discard("")

        # 1. Reconcile: delete slots that no longer exist.
        conn = self._get_conn()
        try:
            existing_names = {r["slot_name"] for r in conn.execute(
                "SELECT slot_name FROM agent_prompts WHERE agent_id = ? AND user_id IS NULL",
                (agent_id,),
            ).fetchall()}
        finally:
            conn.close()
        for stale in existing_names - desired_names:
            await self.delete_slot(agent_id, stale)

        # 2. Upsert each slot.
        for s in slots:
            if not isinstance(s, dict):
                continue
            name = (s.get("slot_name") or "").strip()
            if not name:
                continue
            await self.upsert_slot(
                agent_id=agent_id,
                slot_name=name,
                order_index=int(s.get("order_index", 0) or 0),
                lock=bool(s.get("lock", False)),
                merge_mode=s.get("merge_mode", "replace"),
                content=s.get("content", "") or "",
                updated_by=updated_by,
            )

        # 3. Reset overrides where requested.
        if reset_overrides_for:
            for name in reset_overrides_for:
                if not name:
                    continue
                await self.reset_overrides(agent_id, name)

        return await self.list_slots(agent_id)

    # ---- Memory System (knowledge brain) ----

    async def memory_upsert(
        self,
        user_id: str,
        slug: str,
        page_type: str,
        title: str,
        compiled_truth: str = "",
        timeline: str = "",
        frontmatter: Optional[dict] = None,
    ) -> dict:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                now = _now_iso()
                existing = conn.execute(
                    "SELECT id FROM memories WHERE user_id = ? AND slug = ?",
                    (user_id, slug),
                ).fetchone()

                data = {
                    "user_id": user_id,
                    "slug": slug,
                    "page_type": page_type,
                    "title": title,
                    "compiled_truth": compiled_truth,
                    "timeline": timeline,
                    "frontmatter": json.dumps(frontmatter or {}),
                    "updated_at": now,
                }

                if existing:
                    set_parts = [f"{k} = ?" for k in data]
                    set_vals = list(data.values())
                    conn.execute(
                        f"UPDATE memories SET {', '.join(set_parts)} WHERE id = ?",
                        set_vals + [existing["id"]],
                    )
                    data["id"] = existing["id"]
                else:
                    data["id"] = _uuid()
                    data["created_at"] = now
                    cols = ", ".join(data.keys())
                    placeholders = ", ".join("?" for _ in data)
                    conn.execute(
                        f"INSERT INTO memories ({cols}) VALUES ({placeholders})",
                        list(data.values()),
                    )

                conn.commit()
                memory_id = data["id"]

                # ── Embed on write: chunk + embed + store ──
                embed_sources = []
                if compiled_truth and compiled_truth.strip():
                    embed_sources.append((compiled_truth, "compiled_truth"))
                if timeline and timeline.strip():
                    embed_sources.append((timeline, "timeline"))
                if embed_sources:
                    for text, source in embed_sources:
                        try:
                            await self._embed_and_store_chunks(conn, memory_id, text, source)
                        except Exception as chunk_err:
                            logger.warning("Chunk+embed failed for memory %s (%s): %s", slug, source, chunk_err)

                logger.debug("Memory upserted: %s (%s)", slug, "updated" if existing else "created")
                return data
            except Exception as e:
                logger.error("Error upserting memory %s: %s", slug, e)
                raise
            finally:
                conn.close()

    async def memory_get(self, user_id: str, slug: str) -> Optional[dict]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM memories WHERE user_id = ? AND slug = ?",
                (user_id, slug),
            ).fetchone()
            if row:
                result = dict(row)
                result["frontmatter"] = json.loads(result.get("frontmatter", "{}"))
                return result
            return None
        except Exception as e:
            logger.error("Error getting memory %s: %s", slug, e)
            raise
        finally:
            conn.close()

    async def memory_delete(self, user_id: str, slug: str) -> bool:
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "DELETE FROM memories WHERE user_id = ? AND slug = ?",
                (user_id, slug),
            )
            conn.commit()
            # CASCADE deletes memory_chunks, memory_timeline rows
            deleted = cursor.rowcount > 0
            if deleted:
                logger.debug("Deleted memory: %s", slug)
            return deleted
        except Exception as e:
            logger.error("Error deleting memory %s: %s", slug, e)
            raise
        finally:
            conn.close()

    async def memory_list(
        self, user_id: str, page_type: Optional[str] = None
    ) -> List[dict]:
        conn = self._get_conn()
        try:
            if page_type:
                rows = conn.execute(
                    "SELECT * FROM memories WHERE user_id = ? AND page_type = ? ORDER BY updated_at DESC",
                    (user_id, page_type),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memories WHERE user_id = ? ORDER BY updated_at DESC",
                    (user_id,),
                ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["frontmatter"] = json.loads(d.get("frontmatter", "{}"))
                result.append(d)
            return result
        except Exception as e:
            logger.error("Error listing memories: %s", e)
            raise
        finally:
            conn.close()

    async def memory_search(
        self, user_id: str, query: str, limit: int = 10
    ) -> List[dict]:
        """Hybrid search: FTS5 + vector cosine similarity, merged via RRF."""
        fts_task = asyncio.create_task(self._fts5_search(user_id, query, limit * 3))
        vec_task = asyncio.create_task(self._vector_search(user_id, query, limit * 3))

        fts_results, vec_results = await asyncio.gather(fts_task, vec_task, return_exceptions=True)
        if isinstance(fts_results, BaseException):
            logger.warning("FTS5 search failed: %s", fts_results)
            fts_results = []
        if isinstance(vec_results, BaseException):
            logger.warning("Vector search failed: %s", vec_results)
            vec_results = []

        if not vec_results:
            return fts_results[:limit] if fts_results else []
        if not fts_results:
            return vec_results[:limit]

        # ── RRF merge ──
        k = 60
        rrf_scores: Dict[str, float] = {}
        for rank, page in enumerate(fts_results, start=1):
            slug = page.get("slug", "")
            rrf_scores[slug] = rrf_scores.get(slug, 0.0) + 1.0 / (k + rank)
        for rank, page in enumerate(vec_results, start=1):
            slug = page.get("slug", "")
            rrf_scores[slug] = rrf_scores.get(slug, 0.0) + 1.0 / (k + rank)

        all_pages: Dict[str, dict] = {}
        for p in fts_results + vec_results:
            s = p.get("slug", "")
            if s and s not in all_pages:
                all_pages[s] = p

        merged = []
        for slug, score in sorted(rrf_scores.items(), key=lambda x: -x[1]):
            if slug in all_pages:
                entry = dict(all_pages[slug])
                entry["rank"] = round(score, 4)
                merged.append(entry)
        return merged[:limit]

    async def _fts5_search(
        self, user_id: str, query: str, limit: int = 10
    ) -> List[dict]:
        """FTS5-only keyword search (internal, called within hybrid memory_search)."""
        match_expr = _fts5_safe_match_query(query)
        if not match_expr:
            return []
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT m.*, rank FROM memories_fts fts
                   JOIN memories m ON m.rowid = fts.rowid
                   WHERE memories_fts MATCH ? AND m.user_id = ?
                   ORDER BY rank
                   LIMIT ?""",
                (match_expr, user_id, limit),
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["frontmatter"] = json.loads(d.get("frontmatter", "{}"))
                result.append(d)
            return result
        finally:
            conn.close()

    @staticmethod
    def _chunk_text(text: str, max_chars: int = 500) -> List[str]:
        """Split text into ~max_chars chunks, breaking at sentence boundaries."""
        if len(text) <= max_chars:
            return [text]
        chunks = []
        pos = 0
        while pos < len(text):
            end = min(pos + max_chars, len(text))
            if end < len(text):
                best_break = max(
                    text.rfind(". ", pos, end),
                    text.rfind(".\n", pos, end),
                    text.rfind("\n", pos, end),
                    text.rfind(". ", pos, end),
                    text.rfind(" ", pos + max_chars // 2, end),
                )
                if best_break > pos + max_chars // 2:
                    end = best_break + 1
            chunk = text[pos:end].strip()
            if chunk:
                chunks.append(chunk)
            pos = end
        return chunks

    async def _vector_search(
        self, user_id: str, query_text: str, limit: int = 10
    ) -> List[dict]:
        """Search memory pages by embedding cosine similarity."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT mc.memory_id, mc.embedding,
                          m.slug, m.title, m.compiled_truth, m.timeline,
                          m.page_type, m.frontmatter, m.created_at, m.updated_at
                   FROM memory_chunks mc
                   JOIN memories m ON m.id = mc.memory_id
                   WHERE m.user_id = ? AND mc.embedding IS NOT NULL""",
                (user_id,),
            ).fetchall()
            if not rows:
                return []
        finally:
            conn.close()

        # Get query embedding via OpenRouter
        try:
            query_vec_list = await embed_text(query_text)
        except Exception as e:
            logger.warning("Query embed failed, skipping vector search: %s", e)
            return []
        query_vec = np.array(query_vec_list, dtype=np.float32)

        # Build matrix from stored embeddings
        memory_ids = []
        vecs = []
        for r in rows:
            if r["embedding"]:
                vec = np.frombuffer(r["embedding"], dtype=np.float32)
                if vec.shape[0] == EMBED_DIM:
                    memory_ids.append(r["memory_id"])
                    vecs.append(vec)
        if not vecs:
            return []

        matrix = np.stack(vecs)
        norms = np.linalg.norm(matrix, axis=1)
        q_norm = np.linalg.norm(query_vec)
        scores = np.dot(matrix, query_vec) / (norms * q_norm + 1e-12)

        # Group by memory_id, keep best score per page
        page_best: Dict[str, float] = {}
        for i, mid in enumerate(memory_ids):
            s = float(scores[i])
            if mid not in page_best or s > page_best[mid]:
                page_best[mid] = s

        # Build result dicts from rows, matched by memory_id
        page_rows: Dict[str, dict] = {}
        for r in rows:
            mid = r["memory_id"]
            if mid not in page_rows:
                page_rows[mid] = {
                    "slug": r["slug"],
                    "title": r["title"],
                    "compiled_truth": r["compiled_truth"],
                    "timeline": r["timeline"],
                    "page_type": r["page_type"],
                    "frontmatter": json.loads(r["frontmatter"] or "{}"),
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }

        result = []
        for mid, score in sorted(page_best.items(), key=lambda x: -x[1]):
            if mid in page_rows:
                entry = dict(page_rows[mid])
                entry["rank"] = round(float(score), 4)
                result.append(entry)
                if len(result) >= limit:
                    break
        return result

    async def _embed_and_store_chunks(
        self, conn: sqlite3.Connection, memory_id: str, text: str, source: str
    ) -> None:
        """Chunk text, embed each chunk via OpenRouter, store in memory_chunks."""
        chunks = self._chunk_text(text, max_chars=500)
        # Remove old chunks for this memory_id + source
        conn.execute(
            "DELETE FROM memory_chunks WHERE memory_id = ? AND chunk_source = ?",
            (memory_id, source),
        )
        stored = 0
        for i, chunk in enumerate(chunks):
            if not chunk.strip():
                continue
            chunk_id = _uuid()
            embedding_blob = None
            try:
                emb_list = await embed_text(chunk)
                embedding_blob = struct.pack(f"{len(emb_list)}f", *emb_list)
            except Exception as e:
                logger.warning("Chunk embed failed (idx=%d mem=%s): %s", i, memory_id, e)
            conn.execute(
                """INSERT OR REPLACE INTO memory_chunks
                   (id, memory_id, chunk_index, chunk_text, chunk_source, embedding, token_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (chunk_id, memory_id, i, chunk, source, embedding_blob, len(chunk.split())),
            )
            stored += 1
        if stored:
            logger.debug("Stored %d chunks for memory %s (%s)", stored, memory_id, source)

    async def memory_add_link(
        self,
        user_id: str,
        from_slug: str,
        to_slug: str,
        link_type: str,
        context: Optional[str] = None,
    ) -> dict:
        conn = self._get_conn()
        try:
            now = _now_iso()
            lid = _uuid()
            conn.execute(
                """INSERT OR IGNORE INTO memory_links
                   (id, user_id, from_slug, to_slug, link_type, context, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (lid, user_id, from_slug, to_slug, link_type, context, now),
            )
            conn.commit()
            logger.debug("Added link: %s --%s--> %s", from_slug, link_type, to_slug)
            return {
                "id": lid, "user_id": user_id, "from_slug": from_slug,
                "to_slug": to_slug, "link_type": link_type, "context": context,
            }
        except Exception as e:
            logger.error("Error adding link: %s", e)
            raise
        finally:
            conn.close()

    async def memory_graph_query(
        self,
        user_id: str,
        node_slug: str,
        link_type: Optional[str] = None,
        direction: str = "both",
        depth: int = 2,
    ) -> List[dict]:
        """Traverse the knowledge graph. SQLite doesn't support recursive CTEs,
        so we do a BFS in Python up to the given depth."""
        conn = self._get_conn()
        try:
            visited = set()
            results = []
            queue = [(node_slug, 0)]

            while queue:
                current, current_depth = queue.pop(0)
                if current in visited or current_depth > depth:
                    continue
                visited.add(current)

                if direction in ("out", "both"):
                    sql = "SELECT * FROM memory_links WHERE user_id = ? AND from_slug = ?"
                    params = [user_id, current]
                    if link_type:
                        sql += " AND link_type = ?"
                        params.append(link_type)
                    rows = conn.execute(sql, params).fetchall()
                    for r in rows:
                        d = dict(r)
                        results.append(d)
                        if current_depth + 1 <= depth:
                            queue.append((d["to_slug"], current_depth + 1))

                if direction in ("in", "both"):
                    sql = "SELECT * FROM memory_links WHERE user_id = ? AND to_slug = ?"
                    params = [user_id, current]
                    if link_type:
                        sql += " AND link_type = ?"
                        params.append(link_type)
                    rows = conn.execute(sql, params).fetchall()
                    for r in rows:
                        d = dict(r)
                        results.append(d)
                        if current_depth + 1 <= depth:
                            queue.append((d["from_slug"], current_depth + 1))

            return results
        except Exception as e:
            logger.error("Error in graph query: %s", e)
            raise
        finally:
            conn.close()

    async def memory_add_timeline_entry(
        self,
        user_id: str,
        page_slug: str,
        event_date: str,
        source: str,
        summary: str,
        detail: Optional[str] = None,
    ) -> dict:
        conn = self._get_conn()
        try:
            # Find the memory page id
            page = conn.execute(
                "SELECT id FROM memories WHERE user_id = ? AND slug = ?",
                (user_id, page_slug),
            ).fetchone()
            if not page:
                raise ValueError(f"Memory page not found: {page_slug}")

            now = _now_iso()
            entry_id = _uuid()
            conn.execute(
                """INSERT INTO memory_timeline
                   (id, memory_id, event_date, source, summary, detail, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (entry_id, page["id"], event_date, source, summary, detail, now),
            )
            conn.commit()
            logger.debug("Added timeline entry to %s: %s", page_slug, summary[:50])
            return {
                "id": entry_id, "page_slug": page_slug, "event_date": event_date,
                "source": source, "summary": summary, "detail": detail,
            }
        except Exception as e:
            logger.error("Error adding timeline entry: %s", e)
            raise
        finally:
            conn.close()

    # ---- Session Search ----

    async def search_sessions(
        self, user_id: str, query: str, limit: int = 5
    ) -> List[dict]:
        conn = self._get_conn()
        try:
            # Try finding summaries first
            rows = conn.execute(
                """SELECT * FROM session_summaries
                   WHERE user_id = ? AND summary LIKE ?
                   ORDER BY updated_at DESC
                   LIMIT ?""",
                (user_id, f"%{query}%", limit),
            ).fetchall()
            if rows:
                logger.debug(
                    "Found %s session summaries for query %s", len(rows), query
                )
                return [dict(r) for r in rows]

            # Fallback: search interactions and find distinct sessions
            msg_rows = conn.execute(
                """SELECT session_id, content, created_at FROM interactions
                   WHERE content LIKE ?
                   LIMIT ?""",
                (f"%{query}%", limit * 5),
            ).fetchall()

            if not msg_rows:
                return []

            session_ids = list(set(r["session_id"] for r in msg_rows))

            sessions_rows = conn.execute(
                "SELECT id, title, created_at, updated_at FROM sessions WHERE id IN ({})".format(
                    ",".join("?" for _ in session_ids)
                ),
                session_ids,
            ).fetchall()

            results = []
            for s in sessions_rows:
                matched = [r for r in msg_rows if r["session_id"] == s["id"]]
                temp_summary = "Messages found: " + "; ".join(
                    r["content"][:100] for r in matched[:3]
                )
                results.append({
                    "session_id": s["id"],
                    "title": s.get("title", "Untitled"),
                    "summary": temp_summary,
                    "message_count": len(matched),
                    "updated_at": s.get("updated_at", ""),
                })
            logger.debug(
                "Found %s sessions via message search for query %s",
                len(results),
                query,
            )
            return results

        except Exception as e:
            logger.error("Error searching sessions: %s", e)
            raise
        finally:
            conn.close()

    # ---- Skills & Performance Tracking ----

    async def list_skills(self, user_id: str, limit: int = 50) -> List[dict]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT s.*,
                    COALESCE(e.total_exec, 0) AS total_executions,
                    COALESCE(e.success_count, 0) AS successful_executions,
                    e.avg_duration_ms
                   FROM skills s
                   LEFT JOIN (
                       SELECT skill_id,
                           COUNT(*) AS total_exec,
                           SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) AS success_count,
                           AVG(duration_ms) AS avg_duration_ms
                       FROM skill_executions
                       GROUP BY skill_id
                   ) e ON e.skill_id = s.id
                   WHERE s.user_id = ? AND s.is_active = 1
                   ORDER BY e.total_exec DESC
                   LIMIT ?""",
                (user_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error("Error listing skills: %s", e)
            raise
        finally:
            conn.close()

    async def skill_track_execution(
        self,
        skill_id: str,
        user_id: str,
        session_id: str,
        success: bool,
        duration_ms: int,
        interaction_id: Optional[str] = None,
        error_message: Optional[str] = None,
        input_params: Optional[dict] = None,
        output_summary: Optional[str] = None,
        steps_to_complete: int = 1,
    ) -> str:
        """Record a skill execution. Returns the execution id."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                eid = _uuid()
                now = _now_iso()
                conn.execute(
                    """INSERT INTO skill_executions
                       (id, skill_id, user_id, session_id, interaction_id,
                        success, duration_ms, steps_to_complete, error_message,
                        input_params, output_summary, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (eid, skill_id, user_id, session_id, interaction_id,
                     1 if success else 0, duration_ms, steps_to_complete, error_message,
                     json.dumps(input_params or {}), output_summary, now),
                )
                conn.commit()
                return eid
            except Exception as e:
                logger.error("Error tracking skill execution: %s", e)
                raise
            finally:
                conn.close()

    async def skill_get_rating(
        self, skill_id: str, user_id: Optional[str] = None
    ) -> dict:
        """
        Compute composite rating for a skill.
        Returns dict with score (0-100), success_rate, efficiency, feedback_score, execution_count.
        """
        conn = self._get_conn()
        try:
            # Get executions
            if user_id:
                rows = conn.execute(
                    "SELECT success, duration_ms, steps_to_complete FROM skill_executions WHERE skill_id = ? AND user_id = ?",
                    (skill_id, user_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT success, duration_ms, steps_to_complete FROM skill_executions WHERE skill_id = ?",
                    (skill_id,),
                ).fetchall()

            total = len(rows)
            if total == 0:
                return {
                    "skill_id": skill_id,
                    "score": None,
                    "success_rate": None,
                    "efficiency": None,
                    "feedback_score": None,
                    "execution_count": 0,
                    "avg_duration_ms": None,
                }

            successes = sum(1 for r in rows if r["success"])
            success_rate = successes / total

            avg_steps = sum(r["steps_to_complete"] for r in rows) / total
            avg_duration = sum(r["duration_ms"] for r in rows) / total

            # Efficiency: compare to 1.0 as baseline (1 step is ideal)
            efficiency = max(0, 1.0 - (avg_steps - 1.0) / 5.0) if avg_steps > 0 else 1.0

            # Feedback score
            fb_rows = conn.execute(
                "SELECT feedback_type FROM skill_feedback WHERE skill_id = ?",
                (skill_id,),
            ).fetchall()
            pos = sum(1 for r in fb_rows if r["feedback_type"] == "positive")
            neg = sum(1 for r in fb_rows if r["feedback_type"] == "negative")
            feedback_score = (pos - neg) / (pos + neg + 1) if (pos + neg) > 0 else 0

            # Composite: 40% success rate, 30% efficiency, 20% feedback, 10% recency bonus
            recency = min(1.0, total / 20.0)  # plateaus at 20+ executions
            score = round((
                success_rate * 0.4 +
                efficiency * 0.3 +
                (feedback_score + 1) / 2 * 0.2 +  # normalize feedback to 0-1
                recency * 0.1
            ) * 100, 1)

            return {
                "skill_id": skill_id,
                "score": score,
                "success_rate": round(success_rate * 100, 1),
                "efficiency": round(efficiency * 100, 1),
                "feedback_score": round(feedback_score, 2),
                "execution_count": total,
                "avg_duration_ms": round(avg_duration, 0),
            }
        except Exception as e:
            logger.error("Error getting skill rating: %s", e)
            raise
        finally:
            conn.close()

    async def skill_add_feedback(
        self,
        skill_id: str,
        user_id: str,
        feedback_type: str,
        execution_id: Optional[str] = None,
        message: Optional[str] = None,
    ) -> str:
        """Record user feedback on a skill execution. Returns the feedback id."""
        conn = self._get_conn()
        try:
            fid = _uuid()
            now = _now_iso()
            conn.execute(
                """INSERT INTO skill_feedback
                   (id, skill_id, execution_id, user_id, feedback_type, message, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (fid, skill_id, execution_id, user_id, feedback_type, message, now),
            )
            conn.commit()
            return fid
        except Exception as e:
            logger.error("Error adding skill feedback: %s", e)
            raise
        finally:
            conn.close()

    async def skill_get_id_by_name(self, user_id: str, name: str) -> Optional[str]:
        """Look up a skill's id by name for a user."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT id FROM skills WHERE user_id = ? AND name = ? AND is_active = 1 LIMIT 1",
                (user_id, name),
            ).fetchone()
            return row["id"] if row else None
        finally:
            conn.close()
    
    async def get_agent_for_user(self, user_id: str) -> Optional[dict]:
        default_id = await self.get_user_default_agent_id(user_id)
        if default_id:
            agent = await self.get_agent_by_id(default_id)
            if agent:
                return agent
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT * FROM agents WHERE is_user_default = 1
                   AND EXISTS (SELECT 1 FROM json_each(admin_users) WHERE value = ?) LIMIT 1""", (user_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def get_agent_by_id(self, agent_id: str) -> Optional[dict]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def fetch_agent_with_context(
        self,
        user_id: str,
        context_types: Optional[List[str]] = None,
    ) -> Optional[dict]:
        """Fetch the user's default agent + its resolved prompt slots."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT * FROM agents WHERE is_user_default = 1
                   AND EXISTS (SELECT 1 FROM json_each(admin_users) WHERE value = ?) LIMIT 1""",
                (user_id,)
            ).fetchone()
            if not row:
                return None
            agent_dict = dict(row)
            agent_dict["context_documents"] = await self._docs_for_caller(
                agent_dict["id"], user_id, context_types,
            )
            return agent_dict
        except Exception as e:
            logger.error("Error fetching agent with context: %s", e)
            raise
        finally:
            conn.close()

    async def fetch_agent_by_id_with_context(
        self,
        agent_id: str,
        context_types: Optional[List[str]] = None,
        user_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Fetch one agent by id plus its resolved prompt slots for `user_id`.

        If user_id is None, returns admin-base content only.
        """
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
            if not row:
                return None
            agent_dict = dict(row)
            agent_dict["context_documents"] = await self._docs_for_caller(
                agent_id, user_id, context_types,
            )
            return agent_dict
        except Exception as e:
            logger.error("Error fetching agent by id with context: %s", e)
            raise
        finally:
            conn.close()

    async def _docs_for_caller(
        self,
        agent_id: str,
        user_id: Optional[str],
        context_types: Optional[List[str]] = None,
    ) -> List[dict]:
        """Resolved per-slot content shaped as legacy context_documents list."""
        slots = await self.resolve_prompts(agent_id, user_id=user_id)
        docs: List[dict] = []
        for s in slots:
            content = (s.get("content") or "").strip()
            if not content:
                continue
            docs.append({
                "id": s["slot_name"],
                "context_type": s["slot_name"],
                "title": s["slot_name"],
                "content": s["content"],
                "tags": [],
            })
        if context_types:
            docs = [d for d in docs if d["context_type"] in context_types]
        return docs

    async def create_agent_for_user(self, user_id: str) -> dict:
        conn = self._get_conn()
        try:
            # Always seed from JSON files to ensure latest values
            self._seed_agent_templates_from_json_files(conn)

            # Clone the default template
            tpl = conn.execute(
                "SELECT * FROM agent_templates WHERE id = 'default'"
            ).fetchone()
            if not tpl:
                logger.warning(
                    "No 'default' agent template found after JSON seeding — "
                    "check app/context/agents/default.json"
                )
                raise ValueError("No default agent template available")

            tpl_data = dict(tpl)
            now = _now_iso()
            agent_id = _uuid()
            conn.execute(
                """INSERT INTO agents
                   (id, name,
                    max_turn_count, model, provider,
                    temperature, max_tokens, status, metadata,
                    trigger_type, trigger_key, loop_logic,
                    is_user_default, admin_users, assigned_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, 1, ?, ?, ?, ?)""",
                (agent_id, tpl_data.get("name", "autoAgent"),
                 tpl_data["max_turn_count"],
                 tpl_data["model"],
                 tpl_data["provider"],
                 tpl_data["temperature"],
                 tpl_data["max_tokens"],
                 tpl_data["metadata"],
                 tpl_data.get("trigger_type", "user_input"),
                 tpl_data.get("trigger_key"),
                 tpl_data.get("loop_logic", "[]"),
                 json.dumps([user_id]),
                 now, now, now),
            )
            self._clone_template_slots(conn, source_id="default", target_id=agent_id, now=now)
            conn.commit()

            # Set as user's default agent in user_profiles
            conn.execute(
                """INSERT INTO user_profiles (user_id, is_admin, default_agent_id, created_at, updated_at)
                   VALUES (?, 0, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     default_agent_id = excluded.default_agent_id,
                     updated_at = excluded.updated_at""",
                (user_id, agent_id, now, now),
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            logger.info(
                "Created agent %s for user %s from JSON template",
                agent_id, user_id,
            )
            return dict(row)
        except Exception as e:
            logger.error("Error creating agent for user %s: %s", user_id, e)
            raise
        finally:
            conn.close()

    async def increment_agent_turn_count(self, agent_id: str) -> int:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT turn_count FROM agents WHERE id = ?", (agent_id,)
                ).fetchone()
                current = row["turn_count"] if row else 0
                new_count = current + 1
                conn.execute(
                    "UPDATE agents SET turn_count = ?, updated_at = ? WHERE id = ?",
                    (new_count, _now_iso(), agent_id),
                )
                conn.commit()
                return new_count
            except Exception as e:
                logger.error("Error incrementing turn count for agent %s: %s", agent_id, e)
                raise
            finally:
                conn.close()

    async def get_default_template(self) -> dict:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM agent_templates WHERE id = 'default'"
            ).fetchone()
            if row:
                return dict(row)
            logger.warning(
                "No 'default' agent template in DB — check app/context/agents/default.json"
            )
            # Fallback: minimal dict — JSON is the real source of truth
            return {
                "id": "default",
                "system_prompt": "",
                "max_turn_count": 10,
                "model": None,
                "provider": None,
                "temperature": 0.0,
                "max_tokens": 4096,
                "metadata": "{}",
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
            }
        finally:
            conn.close()

    async def get_max_turn_count(self, agent_id: str = "default_agent") -> int:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT max_turn_count FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            if row:
                return row["max_turn_count"]
            logger.warning("Agent %s not found for max_turn_count lookup", agent_id)
            return 10
        finally:
            conn.close()

    async def seed_agent_templates(self) -> int:
        """Re-seed agent_templates from app/context/agents/*.json. Returns count."""
        conn = self._get_conn()
        try:
            before = conn.execute(
                "SELECT COUNT(*) FROM agent_templates"
            ).fetchone()[0]
            self._seed_agent_templates_from_json_files(conn)
            after = conn.execute(
                "SELECT COUNT(*) FROM agent_templates"
            ).fetchone()[0]
            return after - before
        finally:
            conn.close()

    # ---- Agent Resolution & Session Binding ----

    async def resolve_agent(self, user_id: str, template_id: str) -> dict:
        """
        Resolve an agent for a user + template combo.

        1. Look for existing active agent row with matching user_id + template_id.
        2. If found as 'active', return it (with status 'active').
        3. If not found, return a virtual dict with status='template' and all
           fields from the agent_templates table, ready for the caller to
           materialize into a real agents row.
        4. If no template found, raise ValueError.
        """
        conn = self._get_conn()
        try:
            # Ensure templates are seeded from JSON
            self._seed_agent_templates_from_json_files(conn)

            # Check for existing agent with matching admin_users + template_id
            row = conn.execute(
                """SELECT * FROM agents WHERE template_id = ?
                   AND EXISTS (SELECT 1 FROM json_each(admin_users) WHERE value = ?) LIMIT 1""",
                (template_id, user_id),
            ).fetchone()
            if row:
                ad = dict(row)
                if ad.get("status") == "active":
                    return ad

            # Look up the template
            tpl = conn.execute(
                "SELECT * FROM agent_templates WHERE id = ?", (template_id,)
            ).fetchone()
            if not tpl:
                raise ValueError(f"No agent template found for id '{template_id}'")

            tpl_data = dict(tpl)
            return {
                "status": "template",
                "template_id": template_id,
                "name": tpl_data.get("name", ""),
                "max_turn_count": tpl_data.get("max_turn_count", 10),
                "model": tpl_data.get("model"),
                "provider": tpl_data.get("provider"),
                "temperature": tpl_data.get("temperature", 0.0),
                "max_tokens": tpl_data.get("max_tokens", 4096),
                "metadata": tpl_data.get("metadata", "{}"),
                "trigger_type": tpl_data.get("trigger_type", "user_input"),
                "trigger_key": tpl_data.get("trigger_key"),
                "loop_logic": tpl_data.get("loop_logic", "[]"),
            }
        except ValueError:
            raise
        except Exception as e:
            logger.error("Error resolving agent: %s", e)
            raise RuntimeError(
                f"Failed to resolve agent for template_id={template_id}: {e}"
            )
        finally:
            conn.close()

    async def bind_session_to_agent(self, session_id: str, agent_id: str) -> None:
        """Bind a session to an agent by setting sessions.agent_id."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE sessions SET agent_id = ?, updated_at = ? WHERE id = ?",
                    (agent_id, _now_iso(), session_id),
                )
                conn.commit()
                logger.debug("Bound session %s to agent %s", session_id, agent_id)
            except Exception as e:
                logger.error("Error binding session to agent: %s", e)
                raise
            finally:
                conn.close()

    async def get_session_agent_id(self, session_id: str) -> Optional[str]:
        """Get the agent_id bound to a session."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT agent_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return row["agent_id"] if row else None
        except Exception as e:
            logger.error("Error getting session agent_id: %s", e)
            raise
        finally:
            conn.close()

    # ---- Session Participants ----

    async def add_session_participant(
        self, session_id: str, participant_id: str, role: str
    ) -> None:
        """Add a participant to a session. role is 'user' or 'agent'."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT participants FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            participants = json.loads(row[0]) if row and row[0] else []
            # Don't add duplicate
            if not any(p.get("id") == participant_id for p in participants):
                participants.append({"id": participant_id, "role": role})
                conn.execute(
                    "UPDATE sessions SET participants=? WHERE id=?",
                    (json.dumps(participants), session_id),
                )
                conn.commit()
        finally:
            conn.close()

    async def remove_session_participant(
        self, session_id: str, participant_id: str
    ) -> None:
        """Remove a participant from a session by id."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT participants FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            participants = json.loads(row[0]) if row and row[0] else []
            participants = [p for p in participants if p.get("id") != participant_id]
            conn.execute(
                "UPDATE sessions SET participants=? WHERE id=?",
                (json.dumps(participants), session_id),
            )
            conn.commit()
        finally:
            conn.close()

    async def is_session_participant(
        self, session_id: str, participant_id: str, role: Optional[str] = None
    ) -> bool:
        """Check if participant_id is in a session. If role specified, also checks role matches."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT participants FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            participants = json.loads(row[0]) if row and row[0] else []
            for p in participants:
                if p.get("id") == participant_id:
                    if role is None or p.get("role") == role:
                        return True
            return False
        finally:
            conn.close()

    async def get_session_participants(
        self, session_id: str
    ) -> List[dict]:
        """Return the full participants array for a session."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT participants FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            return json.loads(row[0]) if row and row[0] else []
        finally:
            conn.close()

    async def get_or_resolve_session_agent(
        self,
        session_id: str,
        user_id: str,
        template_id: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Get the agent for a session, creating/binding it if needed.

        1. If ``sessions.agent_id`` is set → ``fetch_agent_by_id_with_context(agent_id)``
           (direct FK lookup, zero inference chain).
        2. If not set → ``resolve_agent(user_id, template_id)`` to obtain the agent.
           - If status is 'template' or 'filesystem', materialize a real ``agents`` row.
           - Call ``bind_session_to_agent(session_id, agent_id)``.
           - Return ``fetch_agent_by_id_with_context(agent_id)``.
        """
        # Check existing binding first
        existing_id = await self.get_session_agent_id(session_id)
        if existing_id:
            agent = await self.fetch_agent_by_id_with_context(existing_id, user_id=user_id)
            if agent:
                return agent
            logger.warning(
                "Session %s bound to agent %s but agent not found — re-resolving",
                session_id[:8], existing_id[:8],
            )

        # No binding or agent gone — resolve and bind
        agent = await self.resolve_agent(user_id, template_id)

        # If virtual (template/filesystem), materialize as real agents row
        if agent.get("status") in ("template", "filesystem"):
            agent_id = _uuid()
            conn = self._get_conn()
            try:
                now = _now_iso()
                _owner = user_id
                conn.execute(
                    """INSERT INTO agents
                       (id, template_id, name, max_turn_count, model, provider,
                        temperature, max_tokens, status, metadata, trigger_type, trigger_key, loop_logic,
                        is_admin_agent, admin_users, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (agent_id, template_id,
                     agent.get("name", ""),
                     agent.get("max_turn_count", 10),
                     agent.get("model"),
                     agent.get("provider"),
                     agent.get("temperature", 0.0),
                     agent.get("max_tokens", 4096),
                     json.dumps(agent.get("metadata", {})),
                     agent.get("trigger_type", "user_input"),
                     agent.get("trigger_key"),
                     agent.get("loop_logic", "[]"),
                     1 if agent.get("is_admin_agent") else 0,
                     json.dumps([user_id]),
                     now, now),
                )
                # Clone admin-base slots from the source template into the new agent row.
                self._clone_template_slots(
                    conn,
                    source_id=template_id or "default",
                    target_id=agent_id,
                    now=now,
                )
                conn.commit()
            except Exception as e:
                logger.error("Error materializing agent: %s", e)
                raise
            finally:
                conn.close()
            agent["id"] = agent_id

        # Bind session to agent
        if agent.get("id"):
            await self.bind_session_to_agent(session_id, agent["id"])
            # Auto-register user and agent as participants
            if not await self.is_session_participant(session_id, agent["id"], 'agent'):
                await self.add_session_participant(session_id, agent["id"], 'agent')
            if not await self.is_session_participant(session_id, user_id, 'user'):
                await self.add_session_participant(session_id, user_id, 'user')

        # Fetch with context (by ID, not user_id)
        if agent.get("id"):
            return await self.fetch_agent_by_id_with_context(agent["id"], user_id=user_id)
        return agent

    # ---- Interrupt Handling ----

    async def set_interrupt(self, session_id: str) -> None:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO session_interrupts (session_id, interrupt_requested, created_at) 
                       VALUES (?, 1, ?) 
                       ON CONFLICT(session_id) DO UPDATE SET interrupt_requested = 1, created_at = ?""",
                    (session_id, _now_iso(), _now_iso())
                )
                conn.commit()
            except Exception as e:
                logger.error("Error setting interrupt for %s: %s", session_id, e)
                raise
            finally:
                conn.close()

    async def clear_interrupt(self, session_id: str) -> None:
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "DELETE FROM session_interrupts WHERE session_id = ?",
                    (session_id,)
                )
                conn.commit()
            except Exception as e:
                logger.error("Error clearing interrupt for %s: %s", session_id, e)
                raise
            finally:
                conn.close()

    # ---- Attachments ----

    async def insert_attachment(
        self,
        user_id: str,
        session_id: str,
        original_name: str,
        mime_type: str,
        size_bytes: int,
        storage_path: str,
        metadata: Optional[dict] = None,
    ) -> str:
        """Insert an attachment record. Returns the attachment id."""
        att_id = _uuid()
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO attachments (id, user_id, session_id, original_name, mime_type, size_bytes, storage_path, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (att_id, user_id, session_id, original_name, mime_type, size_bytes, storage_path,
                 json.dumps(metadata or {})),
            )
            conn.commit()
            logger.debug("Inserted attachment %s: %s", att_id, original_name)
            return att_id
        finally:
            conn.close()

    async def get_attachment(self, attachment_id: str) -> Optional[dict]:
        """Get a single attachment by id."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
            ).fetchone()
            if not row:
                return None
            return dict(row)
        finally:
            conn.close()

    async def get_session_attachments(self, session_id: str) -> List[dict]:
        """Get all attachments for a session."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM attachments WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def delete_attachment(self, attachment_id: str) -> bool:
        """Delete an attachment record by id."""
        conn = self._get_conn()
        try:
            cur = conn.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    async def check_interrupt(self, session_id: str) -> bool:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT interrupt_requested FROM session_interrupts WHERE session_id = ?",
                (session_id,)
            ).fetchone()
            return bool(row and row["interrupt_requested"])
        except Exception as e:
            logger.error("Error checking interrupt for %s: %s", session_id, e)
            return False
        finally:
            conn.close()

    # ---- Webhook Registrations ----

    async def register_webhook(
        self,
        user_id: str,
        name: str,
        instructions: str = "",
    ) -> dict:
        """Create a generic inbound webhook registration."""
        from datetime import datetime, timezone
        webhook_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO webhook_registrations (id, user_id, name, instructions, active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 1, ?, ?)""",
                (webhook_id, user_id, name, instructions, now, now),
            )
            conn.commit()
            return {
                "id": webhook_id,
                "user_id": user_id,
                "name": name,
                "instructions": instructions,
                "active": True,
                "created_at": now,
            }
        finally:
            conn.close()

    async def get_webhook(self, webhook_id: str) -> Optional[dict]:
        """Get a webhook registration by id."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM webhook_registrations WHERE id = ?", (webhook_id,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["active"] = bool(d["active"])
            return d
        finally:
            conn.close()

    async def list_webhooks(self, user_id: str) -> List[dict]:
        """List all webhook registrations for a user."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM webhook_registrations WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["active"] = bool(d["active"])
                result.append(d)
            return result
        finally:
            conn.close()

    async def delete_webhook(self, webhook_id: str, user_id: str) -> bool:
        """Delete a webhook registration (scoped to user_id)."""
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "DELETE FROM webhook_registrations WHERE id = ? AND user_id = ?",
                (webhook_id, user_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    async def log_webhook_event(
        self,
        webhook_id: str,
        method: str,
        headers: str,
        payload: str,
        response_status: int,
        response_body: str,
        duration_ms: int,
    ) -> str:
        """Log an incoming webhook event. Returns the event id."""
        event_id = str(uuid.uuid4())
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO webhook_event_log (id, webhook_id, method, headers, payload,
                   response_status, response_body, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, webhook_id, method, headers, payload,
                 response_status, response_body, duration_ms),
            )
            conn.commit()
            return event_id
        finally:
            conn.close()

    async def get_webhook_logs(
        self, webhook_id: str, limit: int = 20
    ) -> List[dict]:
        """Get recent webhook events for a registration."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM webhook_event_log WHERE webhook_id = ? ORDER BY created_at DESC LIMIT ?",
                (webhook_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---- Auth Elements ----

    async def auth_element_get(
        self, user_id: str, service: str, label: str = "default"
    ) -> Optional[dict]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM auth_elements WHERE user_id = ? AND service = ? AND label = ?",
                (user_id, service, label),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def auth_element_set(
        self,
        user_id: str,
        service: str,
        config: dict,
        secret_ref: str = "",
        label: str = "default",
    ) -> dict:
        import uuid
        conn = self._get_conn()
        try:
            existing = conn.execute(
                "SELECT id FROM auth_elements WHERE user_id = ? AND service = ? AND label = ?",
                (user_id, service, label),
            ).fetchone()
            now = _now_iso()
            config_json = __import__('json').dumps(config)
            if existing:
                conn.execute(
                    "UPDATE auth_elements SET config = ?, secret_ref = ?, updated_at = ? WHERE id = ?",
                    (config_json, secret_ref, now, existing[0]),
                )
                row_id = existing[0]
            else:
                row_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO auth_elements (id, user_id, service, label, config, secret_ref, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (row_id, user_id, service, label, config_json, secret_ref, now, now),
                )
            conn.commit()
            return {
                "id": row_id,
                "user_id": user_id,
                "service": service,
                "label": label,
                "config": config_json,
                "secret_ref": secret_ref,
                "is_active": 1,
            }
        finally:
            conn.close()

    async def auth_element_list(
        self, user_id: str, service: Optional[str] = None
    ) -> List[dict]:
        conn = self._get_conn()
        try:
            if service:
                rows = conn.execute(
                    "SELECT * FROM auth_elements WHERE user_id = ? AND service = ? ORDER BY created_at",
                    (user_id, service),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM auth_elements WHERE user_id = ? ORDER BY service, label",
                    (user_id,),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def auth_element_delete(
        self, user_id: str, service: str, label: str = "default"
    ) -> bool:
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "DELETE FROM auth_elements WHERE user_id = ? AND service = ? AND label = ?",
                (user_id, service, label),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # ---- Provider Ratings ----

    async def get_provider_ratings(self, user_id: str) -> dict:
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT provider, model, rating FROM provider_ratings WHERE user_id = ?",
                (user_id,)
            )
            return {(row[0], row[1]): row[2] for row in cur.fetchall()}
        finally:
            conn.close()

    async def update_provider_rating(self, user_id: str, provider: str, model: str, delta: int) -> int:
        conn = self._get_conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO provider_ratings (user_id, provider, model, rating)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, provider, model) DO UPDATE SET
                    rating = rating + ?
                RETURNING rating
                """,
                (user_id, provider, model, delta, delta)
            )
            row = cur.fetchone()
            if row:
                new_rating = row[0]
            else:
                 new_rating = delta
            conn.commit()
            return new_rating
        finally:
            conn.close()

    # ──────────────────────────────────────────────────────────────────────────
    # User Profiles
    # ──────────────────────────────────────────────────────────────────────────

    async def get_user_profile(self, user_id: str) -> Optional[dict]:
        """Return the user_profiles row for user_id, or None if not found."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def upsert_user_profile(self, user_id: str, **kwargs) -> dict:
        """Create or update a user_profiles row. Returns the full updated row."""
        async with self._write_lock:
            conn = self._get_conn()
            try:
                now = _now_iso()
                existing = conn.execute(
                    "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
                ).fetchone()
                if existing:
                    if kwargs:
                        sets = ", ".join(f"{k} = ?" for k in kwargs)
                        vals = list(kwargs.values()) + [now, user_id]
                        conn.execute(
                            f"UPDATE user_profiles SET {sets}, updated_at = ? WHERE user_id = ?",
                            vals,
                        )
                else:
                    cols = ["user_id", "created_at", "updated_at"] + list(kwargs.keys())
                    placeholders = ", ".join("?" for _ in cols)
                    vals = [user_id, now, now] + list(kwargs.values())
                    conn.execute(
                        f"INSERT INTO user_profiles ({', '.join(cols)}) VALUES ({placeholders})",
                        vals,
                    )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
                ).fetchone()
                return dict(row)
            finally:
                conn.close()

    async def is_user_admin(self, user_id: str) -> bool:
        """Return True if the user has is_admin = 1."""
        profile = await self.get_user_profile(user_id)
        return bool(profile and profile.get("is_admin"))

    async def set_user_admin(self, user_id: str, is_admin: bool) -> dict:
        """Set the is_admin flag for a user. Creates the profile row if needed."""
        return await self.upsert_user_profile(user_id, is_admin=1 if is_admin else 0)

    # ──────────────────────────────────────────────────────────────────────────
    # Multi-agent: template listing and custom agent CRUD
    # ──────────────────────────────────────────────────────────────────────────

    async def list_agent_templates(self, include_admin: bool = False, discoverable_only: bool = False) -> List[dict]:
        """
        Return agent_templates that are user-visible (is_pipeline=0).
        If include_admin=False, excludes access_level='admin_only' templates.
        If discoverable_only=True, only returns templates with discoverable=1 (ignored when include_admin=True).
        """
        conn = self._get_conn()
        try:
            if include_admin:
                rows = conn.execute(
                    "SELECT * FROM agent_templates WHERE is_pipeline = 0 ORDER BY is_system DESC, name ASC"
                ).fetchall()
            elif discoverable_only:
                rows = conn.execute(
                    "SELECT * FROM agent_templates WHERE is_pipeline = 0 AND access_level != 'admin_only' AND discoverable = 1 ORDER BY is_system DESC, name ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM agent_templates WHERE is_pipeline = 0 AND access_level != 'admin_only' ORDER BY is_system DESC, name ASC"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def update_agent_template_fields(self, template_id: str, updates: dict) -> Optional[dict]:
        """
        Update allowed fields on an agent_templates row.
        Returns the updated row, or None if not found.
        Allowed fields: discoverable.
        """
        ALLOWED = {"discoverable"}
        safe = {k: v for k, v in updates.items() if k in ALLOWED}
        if not safe:
            return None
        conn = self._get_conn()
        try:
            existing = conn.execute(
                "SELECT * FROM agent_templates WHERE id = ?", (template_id,)
            ).fetchone()
            if not existing:
                return None
            set_clause = ", ".join(f"{k} = ?" for k in safe)
            conn.execute(
                f"UPDATE agent_templates SET {set_clause}, updated_at = ? WHERE id = ?",
                (*safe.values(), _now_iso(), template_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM agent_templates WHERE id = ?", (template_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    async def list_agents_for_user(self, user_id: str, include_admin: bool = False) -> List[dict]:
        """
        Return all agents visible to a user:
        - System agent templates (is_pipeline=0), filtered by access_level
        - User's own custom agents (owner_user_id = user_id, is_system=0 equivalent)
        Each item includes a 'source' key: 'template' or 'custom'.
        Custom agents also carry their is_user_default flag.
        """
        # 1. System templates the user can see
        templates = await self.list_agent_templates(include_admin=include_admin)
        result = []
        for tpl in templates:
            entry = dict(tpl)
            entry["source"] = "template"
            entry["is_user_default"] = 0
            result.append(entry)

        # 2. User's agents — both assigned (user_id) and custom-created (owner_user_id)
        seen_ids: set = set()
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT * FROM agents
                   WHERE EXISTS (
                           SELECT 1 FROM json_each(admin_users)
                           WHERE value = ?
                         )
                      OR EXISTS (
                           SELECT 1 FROM json_each(member_users)
                           WHERE value = ?
                         )
                   ORDER BY created_at ASC""",
                (user_id, user_id),
            ).fetchall()
            for row in rows:
                entry = dict(row)
                if entry["id"] in seen_ids:
                    continue
                seen_ids.add(entry["id"])
                entry["source"] = "custom"
                result.append(entry)
        finally:
            conn.close()

        # 2b. Aggregate agents from sibling .db files (parallel agent databases)
        db_dir = os.path.dirname(os.path.abspath(self._db_path))
        primary_name = os.path.basename(self._db_path)
        try:
            sibling_paths = [
                os.path.join(db_dir, f)
                for f in os.listdir(db_dir)
                if f.endswith(".db") and f != primary_name
            ]
        except OSError:
            sibling_paths = []

        for sibling_path in sorted(sibling_paths):
            try:
                sconn = sqlite3.connect(sibling_path)
                sconn.row_factory = sqlite3.Row
                sconn.execute("PRAGMA journal_mode=WAL")
                sconn.execute("PRAGMA busy_timeout=5000")
                try:
                    srows = sconn.execute(
                        """SELECT * FROM agents
                           WHERE EXISTS (
                                   SELECT 1 FROM json_each(admin_users)
                                   WHERE value = ?
                                 )
                              OR EXISTS (
                                   SELECT 1 FROM json_each(member_users)
                                   WHERE value = ?
                                 )
                           ORDER BY created_at ASC""",
                        (user_id, user_id),
                    ).fetchall()
                    for row in srows:
                        entry = dict(row)
                        if entry["id"] in seen_ids:
                            continue
                        seen_ids.add(entry["id"])
                        entry["source"] = "custom"
                        result.append(entry)
                finally:
                    sconn.close()
            except Exception as e:
                logger.debug("Skipping sibling DB %s: %s", sibling_path, e)

        # Mark the user's current default
        profile = await self.get_user_profile(user_id)
        default_id = profile.get("default_agent_id") if profile else None
        for entry in result:
            if default_id and entry.get("id") == default_id:
                entry["is_user_default"] = 1
        return result

    # ── Custom agent CRUD ─────────────────────────────────────────────────────

    async def create_custom_agent(
        self, user_id: str, name: str, description: str = "", template_id: str = "default"
    ) -> dict:
        """
        Create a new custom agent for a user, cloned from the specified template.
        Returns the new agents row as a dict (with source='custom').
        """
        import uuid as _uuid_mod
        conn = self._get_conn()
        try:
            self._seed_agent_templates_from_json_files(conn)
            tpl_row = conn.execute(
                "SELECT * FROM agent_templates WHERE id = ?", (template_id,)
            ).fetchone()
            tpl = dict(tpl_row) if tpl_row else {}
            # Fall back to default if the requested template doesn't exist
            if not tpl and template_id != "default":
                tpl_row = conn.execute(
                    "SELECT * FROM agent_templates WHERE id = 'default'"
                ).fetchone()
                tpl = dict(tpl_row) if tpl_row else {}
                template_id = "default"
        finally:
            conn.close()

        if not tpl:
            from app.context.md_seeder import scan_agent_json_files
            for entry in scan_agent_json_files():
                if entry.get("id") == template_id or entry.get("id") == "default":
                    tpl = entry
                    template_id = entry.get("id", "default")
                    break

        agent_id = str(_uuid_mod.uuid4())
        now = _now_iso()
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO agents
                   (id, name, description,
                    max_turn_count, model, provider,
                    temperature, max_tokens, metadata,
                    template_id, is_user_default,
                    allowed_tools, custom_tool_ids,
                    trigger_type, trigger_key, loop_logic,
                    safety_policy, is_admin_agent,
                    admin_users,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,0,'[]','[]',?,?,?,'{}',?,?,?,?)""",
                (
                    agent_id, name, description,
                    tpl.get("max_turn_count", 40),
                    tpl.get("model", ""),
                    tpl.get("provider", ""),
                    tpl.get("temperature", 0.7),
                    tpl.get("max_tokens", 8192),
                    tpl.get("metadata", "{}"),
                    template_id,
                    tpl.get("trigger_type", "user_input"),
                    tpl.get("trigger_key"),
                    tpl.get("loop_logic", "[]"),
                    1 if tpl.get("is_admin_agent") else 0,
                    json.dumps([user_id]),
                    now, now,
                ),
            )
            self._clone_template_slots(conn, source_id=template_id, target_id=agent_id, now=now)
            conn.commit()
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        finally:
            conn.close()

        result = dict(row) if row else {"id": agent_id}
        result["source"] = "custom"
        return result

    async def delete_custom_agent(self, agent_id: str, user_id: str) -> bool:
        """
        Delete a custom agent. Caller must be in admin_users.
        System agents (template rows) cannot be deleted via this path.
        Also drops every agent_prompts row for the agent (admin base + overrides).
        Returns True if a row was deleted, False if not found or not owned.
        """
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                """DELETE FROM agents
                   WHERE id = ? AND is_user_default = 0
                   AND EXISTS (SELECT 1 FROM json_each(admin_users) WHERE value = ?)""",
                (agent_id, user_id),
            )
            if cursor.rowcount > 0:
                conn.execute("DELETE FROM agent_prompts WHERE agent_id = ?", (agent_id,))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    async def get_user_default_agent_id(self, user_id: str) -> Optional[str]:
        """Return the user's preferred default_agent_id, or None if not set."""
        profile = await self.get_user_profile(user_id)
        if profile:
            return profile.get("default_agent_id")
        return None

    async def set_user_default_agent(self, user_id: str, agent_id: str) -> None:
        """
        Set the user's preferred default agent.
        Upserts a user_profiles row and updates default_agent_id.
        """
        now = _now_iso()
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO user_profiles (user_id, is_admin, default_agent_id, created_at, updated_at)
                   VALUES (?, 0, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     default_agent_id = excluded.default_agent_id,
                     updated_at = excluded.updated_at""",
                (user_id, agent_id, now, now),
            )
            conn.commit()
        finally:
            conn.close()

    async def update_agent_fields(
        self,
        agent_id: str,
        user_id: str,
        updates: dict,
    ) -> Optional[dict]:
        """
        Update editable fields on a custom agent. Caller must be in admin_users.
        Prompt slots are NOT updated here — use upsert_slot / replace_slots instead.
        Returns the updated agent row dict, or None if not found/not owned.
        """
        ALLOWED = {
            "name", "description", "max_turn_count",
            "model", "temperature", "max_tokens",
            "allowed_tools", "custom_tool_ids",
            "trigger_type", "trigger_key", "loop_logic",
            "safety_policy", "user_mode", "metadata",
        }
        safe = {}
        for k, v in updates.items():
            if k not in ALLOWED:
                continue
            # Serialize list/dict fields to JSON strings for storage
            if k in ("allowed_tools", "custom_tool_ids", "loop_logic") and isinstance(v, list):
                v = json.dumps(v)
            if k in ("safety_policy", "metadata") and isinstance(v, dict):
                v = json.dumps(v)
            safe[k] = v
        if not safe:
            # Nothing to update; return current state
            conn = self._get_conn()
            try:
                row = conn.execute(
                    """SELECT * FROM agents WHERE id = ?
                       AND EXISTS (SELECT 1 FROM json_each(admin_users) WHERE value = ?)""",
                    (agent_id, user_id),
                ).fetchone()
            finally:
                conn.close()
            return dict(row) if row else None

        now = _now_iso()
        safe["updated_at"] = now
        set_clause = ", ".join(f"{k} = ?" for k in safe)
        values = list(safe.values()) + [agent_id, user_id]

        conn = self._get_conn()
        try:
            cursor = conn.execute(
                f"UPDATE agents SET {set_clause} WHERE id = ? AND EXISTS (SELECT 1 FROM json_each(admin_users) WHERE value = ?)",
                values,
            )
            conn.commit()
            if cursor.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        finally:
            conn.close()

        result = dict(row) if row else None
        if result:
            result["source"] = "custom"
        return result

    # ---- Agent Connections ----

    async def get_agent_connections(self, agent_id: str) -> List[dict]:
        """Return all agent_connections rows for an agent."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM agent_connections WHERE agent_id = ? ORDER BY section, connection_type",
                (agent_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def upsert_agent_connection(
        self,
        agent_id: str,
        connection_type: str,
        section: str,
        enabled: bool,
        config: dict,
    ) -> dict:
        """Insert or update a connection row. Returns the final row."""
        now = _now_iso()
        config_str = json.dumps(config)
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """INSERT INTO agent_connections
                           (id, agent_id, connection_type, section, enabled, config, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(agent_id, connection_type) DO UPDATE SET
                           section = excluded.section,
                           enabled = excluded.enabled,
                           config  = excluded.config,
                           updated_at = excluded.updated_at""",
                    (_uuid(), agent_id, connection_type, section,
                     1 if enabled else 0, config_str, now, now),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM agent_connections WHERE agent_id = ? AND connection_type = ?",
                    (agent_id, connection_type),
                ).fetchone()
                return dict(row) if row else {}
            finally:
                conn.close()

    async def get_all_connections_by_type(self, connection_type: str) -> List[dict]:
        """Return all enabled connections of a given type across all agents."""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM agent_connections WHERE connection_type = ? AND enabled = 1",
                (connection_type,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ── Agent membership (admin_users / member_users) ───────────────────────

    async def get_agent_roles(self, agent_id: str) -> dict:
        """Return {'admin_users': [...], 'member_users': [...]} for an agent."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT admin_users, member_users FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            if not row:
                return {"admin_users": [], "member_users": []}
            return {
                "admin_users": json.loads(row["admin_users"] or "[]"),
                "member_users": json.loads(row["member_users"] or "[]"),
            }
        finally:
            conn.close()

    async def add_agent_member(self, agent_id: str, user_id: str) -> bool:
        """
        Add user_id to an agent's member_users list if not already present.
        Returns True if the user was newly added, False if already a member.
        """
        roles = await self.get_agent_roles(agent_id)
        if user_id in roles["member_users"] or user_id in roles["admin_users"]:
            return False
        new_members = roles["member_users"] + [user_id]
        now = _now_iso()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE agents SET member_users = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(new_members), now, agent_id),
                )
                conn.commit()
            finally:
                conn.close()
        return True

    async def add_agent_admin(self, agent_id: str, user_id: str) -> bool:
        """
        Add user_id to an agent's admin_users list if not already present.
        Returns True if newly added, False if already an admin.
        """
        roles = await self.get_agent_roles(agent_id)
        if user_id in roles["admin_users"]:
            return False
        new_admins = roles["admin_users"] + [user_id]
        now = _now_iso()
        async with self._write_lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "UPDATE agents SET admin_users = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(new_admins), now, agent_id),
                )
                conn.commit()
            finally:
                conn.close()
        return True

    async def backfill_agent_admin_users(self) -> int:
        """
        One-time migration: for agents where admin_users is empty, no owner can be determined.
        Returns number of rows updated.
        """
        conn = self._get_conn()
        try:
            rows = conn.execute(
                """SELECT id FROM agents
                   WHERE admin_users = '[]' OR admin_users IS NULL OR admin_users = ''"""
            ).fetchall()
            updated = 0
            now = _now_iso()
            for row in rows:
                owner = None
                if owner:
                    conn.execute(
                        "UPDATE agents SET admin_users = ?, updated_at = ? WHERE id = ?",
                        (json.dumps([owner]), now, row["id"]),
                    )
                    updated += 1
            conn.commit()
            return updated
        finally:
            conn.close()

    async def is_agent_member(self, agent_id: str, user_id: str) -> bool:
        """Return True if user_id is a member or admin of the agent."""
        roles = await self.get_agent_roles(agent_id)
        return user_id in roles["member_users"] or user_id in roles["admin_users"]


class _LocalTableProxy:
    """
    Emulates supabase.Client.table() so that code using the Supabase query builder
    (ToolLoader, ToolExecutionTracker, admin/review, registry) can work with SQLite.

    Usage: proxy.table("tools").select("*").eq("status", "active").execute()
    """

    def __init__(self, db_path: str):
        self._db_path = db_path

    def table(self, table_name: str) -> "_LocalQueryBuilder":
        return _LocalQueryBuilder(self._db_path, table_name)


class _LocalQueryBuilder:
    """
    Minimal query builder that mimics the supabase query chain:
        .select(columns).eq(field, value).in_(field, values).order(...).limit(n).execute()
    Returns objects with .data (list of dicts) matching supabase's response shape.
    """

    def __init__(self, db_path: str, table_name: str):
        self._db_path = db_path
        self._table_name = table_name
        self._select_cols = "*"
        self._filters: List[tuple[str, str, Any]] = []  # (op, field, value)
        self._order_by: List[tuple[str, bool]] = []  # (field, desc)
        self._limit_val: Optional[int] = None
        self._count: bool = False

    def select(self, columns: str = "*") -> "_LocalQueryBuilder":
        self._select_cols = columns
        return self

    def eq(self, field: str, value: Any) -> "_LocalQueryBuilder":
        self._filters.append(("eq", field, value))
        return self

    def in_(self, field: str, values: list) -> "_LocalQueryBuilder":
        self._filters.append(("in", field, values))
        return self

    def neq(self, field: str, value: Any) -> "_LocalQueryBuilder":
        self._filters.append(("neq", field, value))
        return self

    def ilike(self, field: str, pattern: str) -> "_LocalQueryBuilder":
        self._filters.append(("ilike", field, pattern))
        return self

    def order(self, field: str, *, desc: bool = False) -> "_LocalQueryBuilder":
        self._order_by.append((field, desc))
        return self

    def limit(self, n: int) -> "_LocalQueryBuilder":
        self._limit_val = n
        return self

    def _build_where(self) -> tuple[str, list]:
        """Build WHERE clause from accumulated filters. Returns (sql_fragment, params)."""
        clauses = []
        params: list = []
        for op, field, value in self._filters:
            if op == "eq":
                clauses.append(f"{field} = ?")
                params.append(value)
            elif op == "neq":
                clauses.append(f"{field} != ?")
                params.append(value)
            elif op == "in":
                placeholders = ",".join("?" for _ in value)
                clauses.append(f"{field} IN ({placeholders})")
                params.extend(value)
            elif op == "ilike":
                clauses.append(f"{field} LIKE ?")
                params.append(value)
        if clauses:
            return " WHERE " + " AND ".join(clauses), params
        return "", []

    def execute(self) -> Any:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            # ---- UPDATE ----
            if hasattr(self, '_update_data') and self._update_data is not None:
                set_parts = [f"{k} = ?" for k in self._update_data]
                set_params = list(self._update_data.values())
                where_clause, where_params = self._build_where()
                sql = f"UPDATE {self._table_name} SET {', '.join(set_parts)}{where_clause}"
                conn.execute(sql, set_params + where_params)
                conn.commit()
                # Return a result with the updated data (mimics supabase behavior)
                return _LocalQueryResult([self._update_data])

            # ---- DELETE ----
            if hasattr(self, '_is_delete') and self._is_delete:
                where_clause, where_params = self._build_where()
                sql = f"DELETE FROM {self._table_name}{where_clause}"
                cursor = conn.execute(sql, where_params)
                conn.commit()
                return _LocalQueryResult([])

            # ---- INSERT ----
            if hasattr(self, '_insert_data') and self._insert_data is not None:
                if not self._insert_data:
                    return _LocalQueryResult([])
                import uuid as _uuid_mod
                columns = list(self._insert_data[0].keys())
                placeholders = ",".join("?" for _ in columns)
                col_names = ",".join(columns)
                inserted_rows = []
                for row_data in self._insert_data:
                    # Auto-generate UUID for id if missing or None
                    if 'id' in row_data and not row_data['id']:
                        row_data['id'] = str(_uuid_mod.uuid4())
                    params = [row_data.get(c) for c in columns]
                    conn.execute(
                        f"INSERT INTO {self._table_name} ({col_names}) VALUES ({placeholders})",
                        params,
                    )
                    inserted_rows.append(row_data)
                conn.commit()
                return _LocalQueryResult(inserted_rows)

            # ---- UPSERT ----
            if hasattr(self, '_upsert_data') and self._upsert_data is not None:
                import uuid as _uuid_mod
                data = dict(self._upsert_data)
                conflict_col = getattr(self, '_on_conflict', '')
                columns = list(data.keys())
                placeholders = ",".join("?" for _ in columns)
                col_names = ",".join(columns)
                params = [data.get(c) for c in columns]

                if conflict_col:
                    # Support both single col and composite keys (comma-separated)
                    conflict_cols = [c.strip() for c in conflict_col.split(",")]
                    where_parts = [f"{c} = ?" for c in conflict_cols]
                    where_vals = []
                    for c in conflict_cols:
                        val = data.get(c)
                        if val is not None:
                            where_vals.append(val)
                        else:
                            # Fall back to the raw conflict_col string (legacy)
                            where_vals.append(data.get(conflict_col))
                            break
                    existing = conn.execute(
                        f"SELECT 1 FROM {self._table_name} WHERE {' AND '.join(where_parts)} LIMIT 1",
                        where_vals,
                    ).fetchone()
                    if existing:
                        set_parts = [f"{k} = ?" for k in columns]
                        set_params = [data.get(c) for c in columns]
                        conn.execute(
                            f"UPDATE {self._table_name} SET {', '.join(set_parts)} WHERE {' AND '.join(where_parts)}",
                            set_params + where_vals,
                        )
                    else:
                        if 'id' in data and not data['id']:
                            data['id'] = str(_uuid_mod.uuid4())
                        conn.execute(
                            f"INSERT INTO {self._table_name} ({col_names}) VALUES ({placeholders})",
                            params,
                        )
                else:
                    if 'id' in data and not data['id']:
                        data['id'] = str(_uuid_mod.uuid4())
                    conn.execute(
                        f"INSERT INTO {self._table_name} ({col_names}) VALUES ({placeholders})",
                        params,
                    )
                conn.commit()
                return _LocalQueryResult([data])

            # ---- SELECT (default) ----
            sql = f"SELECT {self._select_cols} FROM {self._table_name}"
            where_clause, where_params = self._build_where()
            sql += where_clause

            if self._order_by:
                order_parts = []
                for field, desc in self._order_by:
                    order_parts.append(f"{field} {'DESC' if desc else 'ASC'}")
                sql += " ORDER BY " + ", ".join(order_parts)

            if self._limit_val is not None:
                sql += f" LIMIT {self._limit_val}"

            rows = conn.execute(sql, where_params).fetchall()
            return _LocalQueryResult([dict(r) for r in rows])
        finally:
            conn.close()

    def update(self, data: dict) -> "_LocalQueryBuilder":
        self._update_data = data
        return self

    def delete(self) -> "_LocalQueryBuilder":
        self._is_delete = True
        return self

    def insert(self, data: dict | list) -> "_LocalQueryBuilder":
        self._insert_data = data if isinstance(data, list) else [data]
        return self

    def upsert(self, data: dict, on_conflict: str = "") -> "_LocalQueryBuilder":
        self._upsert_data = data
        self._on_conflict = on_conflict
        return self


class _LocalQueryResult:
    def __init__(self, data):
        self.data = data
    def __bool__(self):
        return bool(self.data)

"""
Local SQLite storage backend for webAgent.

Completely independent from Supabase. Uses a local SQLite database file.
Auto-creates tables on first use matching the Supabase schema.
"""

import json
import logging
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from app.models.schemas import InteractionRecord
from app.db.interface import StorageBackend

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
    system_prompt TEXT NOT NULL DEFAULT '',
    max_turn_count INTEGER NOT NULL DEFAULT 10,
    model TEXT,
    provider TEXT,
    temperature REAL NOT NULL DEFAULT 0.0,
    max_tokens INTEGER NOT NULL DEFAULT 4096,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO agent_templates (id, system_prompt, max_turn_count)
VALUES ('default', '', 10);

CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE,
    system_prompt TEXT NOT NULL DEFAULT '',
    max_turn_count INTEGER NOT NULL DEFAULT 10,
    model TEXT,
    provider TEXT,
    temperature REAL NOT NULL DEFAULT 0.0,
    max_tokens INTEGER NOT NULL DEFAULT 4096,
    status TEXT NOT NULL DEFAULT 'active',
    metadata TEXT NOT NULL DEFAULT '{}',
    assigned_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_agents_user ON agents(user_id);

CREATE TABLE IF NOT EXISTS context_documents (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id),
    context_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_context_agent ON context_documents(agent_id);
CREATE INDEX IF NOT EXISTS idx_context_type ON context_documents(context_type);

CREATE TABLE IF NOT EXISTS context_templates (
    id TEXT PRIMARY KEY,
    context_type TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_context_templates_type ON context_templates(context_type);

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

"""


class LocalBackend(StorageBackend):
    """SQLite implementation of StorageBackend."""

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or DEFAULT_DB_PATH
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """Get a new connection (thread-safe: each call gets its own)."""
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
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
            if cols and "user_id" not in cols:
                conn.execute("ALTER TABLE agents RENAME TO agents_v1")
                conn.commit()

            # Upgrade legacy context_documents (user_id) before SCHEMA_SQL adds indexes on agent_id
            self._migrate_context_documents_to_agent_id(conn)

            conn.executescript(SCHEMA_SQL)
            conn.commit()

            # ── Migration: add channel column to interactions ──
            cursor = conn.execute("PRAGMA table_info(interactions)")
            cols = {row[1] for row in cursor.fetchall()}
            if "channel" not in cols:
                conn.execute("ALTER TABLE interactions ADD COLUMN channel TEXT")
                conn.commit()

            conn.commit()

            # ── Post-migration: move data from old agents_v1 ──
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='agents_v1'"
            )
            if cursor.fetchone():
                conn.execute(
                    """INSERT OR IGNORE INTO agents
                       (id, user_id, max_turn_count, status, assigned_at, created_at, updated_at)
                       SELECT id, 'migrated_default', max_turn_count, 'active',
                              datetime('now'), datetime('now'), datetime('now')
                       FROM agents_v1 WHERE id = 'default_agent'"""
                )
                conn.execute("DROP TABLE agents_v1")
                conn.commit()
                logger.info("Agents table migration complete")

            # ── Migration: context_defaults -> context_templates ──
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='context_defaults'"
            )
            if cursor.fetchone():
                logger.info("Migrating context_defaults -> context_templates")
                conn.executescript("""
                    INSERT OR IGNORE INTO context_templates (id, context_type, title, content, tags, created_at, updated_at)
                    SELECT id, context_type, title, content, tags, created_at, updated_at FROM context_defaults;
                    DROP TABLE context_defaults;
                """)
                conn.commit()
                logger.info("Context templates migration complete")

        except Exception as e:
            logger.error("Error initializing local database: %s", e)
            raise
        finally:
            conn.close()

    def _migrate_context_documents_to_agent_id(self, conn: sqlite3.Connection) -> None:
        """Migrate legacy context_documents.user_id → agent_id (one-time)."""
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='context_documents'"
        )
        if not cur.fetchone():
            return
        cols = {row[1] for row in conn.execute("PRAGMA table_info(context_documents)").fetchall()}
        if "user_id" not in cols:
            return
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agents'"
        ).fetchone():
            logger.warning(
                "Skipping context_documents migration: agents table not present yet",
            )
            return
        logger.info("Migrating context_documents from user_id to agent_id")
        try:
            conn.execute("ALTER TABLE context_documents ADD COLUMN agent_id TEXT")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            """
            UPDATE context_documents SET agent_id = (
                SELECT agents.id FROM agents WHERE agents.user_id = context_documents.user_id LIMIT 1
            )
            """
        )
        deleted = conn.execute(
            "DELETE FROM context_documents WHERE agent_id IS NULL"
        ).rowcount
        if deleted:
            logger.warning(
                "Removed %s context_documents rows with no matching agent", deleted
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS context_documents_new (
                id TEXT PRIMARY KEY,
                agent_id TEXT NOT NULL REFERENCES agents(id),
                context_type TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            INSERT INTO context_documents_new (id, agent_id, context_type, title, content, tags, created_at, updated_at)
            SELECT id, agent_id, context_type, title, content, tags, created_at, updated_at FROM context_documents
            """
        )
        conn.execute("DROP TABLE context_documents")
        conn.execute("ALTER TABLE context_documents_new RENAME TO context_documents")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_context_agent ON context_documents(agent_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_context_type ON context_documents(context_type)"
        )
        conn.commit()
        logger.info("context_documents migration to agent_id complete")

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
                "SELECT id, session_id, parent_id, role, content, tool_name, tool_call_id, channel, metadata, input, created_at FROM interactions WHERE session_id = ? ORDER BY created_at ASC",
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
    ) -> str:
        await self.assert_session_owned(user_id, session_id)
        conn = self._get_conn()
        try:
            interaction_id = _uuid()
            conn.execute(
                "INSERT INTO interactions (id, session_id, parent_id, role, content, tool_name, tool_call_id, channel, metadata, input) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (interaction_id, session_id, parent_id, role, content, tool_name, tool_call_id, channel, metadata, input_data),
            )
            conn.commit()
            logger.debug("Inserted interaction %s", interaction_id)
            return interaction_id
        except Exception as e:
            logger.error("Error inserting interaction: %s", e)
            raise
        finally:
            conn.close()

    # ---- Context Defaults ----

    async def fetch_context_defaults(
        self, context_types: List[str]
    ) -> List[dict]:
        conn = self._get_conn()
        try:
            placeholders = ",".join("?" for _ in context_types)
            rows = conn.execute(
                f"""SELECT id, context_type, title, content, tags, created_at, updated_at
                    FROM context_templates
                    WHERE context_type IN ({placeholders})""",
                context_types,
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                try:
                    d["tags"] = json.loads(d["tags"])
                except (json.JSONDecodeError, TypeError):
                    d["tags"] = []
                result.append(d)
            logger.debug("Fetched %s context default rows", len(result))
            return result
        except Exception as e:
            logger.error("Error fetching context defaults: %s", e)
            raise
        finally:
            conn.close()

    async def copy_defaults_to_agent(self, agent_id: str) -> int:
        """
        Copy template rows into context_documents for this agent.
        Only copies types not already present for this agent.
        """
        conn = self._get_conn()
        try:
            exists = conn.execute(
                "SELECT 1 FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            if not exists:
                return 0

            defaults = conn.execute(
                "SELECT context_type, title, content, tags FROM context_templates"
            ).fetchall()

            if not defaults:
                return 0

            existing_types = set(
                r["context_type"]
                for r in conn.execute(
                    "SELECT DISTINCT context_type FROM context_documents WHERE agent_id = ?",
                    (agent_id,),
                ).fetchall()
            )

            copied = 0
            for d in defaults:
                if d["context_type"] in existing_types:
                    continue
                conn.execute(
                    """INSERT INTO context_documents (id, agent_id, context_type, title, content, tags)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (_uuid(), agent_id, d["context_type"], d["title"], d["content"], d["tags"]),
                )
                copied += 1

            conn.commit()
            if copied > 0:
                logger.info(
                    "Copied %s default context rows to agent %s", copied, agent_id
                )
            return copied
        except Exception as e:
            logger.error("Error copying defaults to agent: %s", e)
            raise
        finally:
            conn.close()

    # ---- Context Documents ----

    async def fetch_context_documents(
        self,
        agent_id: str,
        context_types: Optional[List[str]] = None,
    ) -> List[dict]:
        conn = self._get_conn()
        try:
            if context_types:
                placeholders = ",".join("?" for _ in context_types)
                rows = conn.execute(
                    f"""SELECT id, agent_id, context_type, title, content, tags, created_at, updated_at
                        FROM context_documents
                        WHERE agent_id = ? AND context_type IN ({placeholders})
                        ORDER BY context_type, title""",
                    (agent_id, *context_types),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, agent_id, context_type, title, content, tags, created_at, updated_at
                       FROM context_documents
                       WHERE agent_id = ?
                       ORDER BY context_type, title""",
                    (agent_id,),
                ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                try:
                    d["tags"] = json.loads(d["tags"])
                except (json.JSONDecodeError, TypeError):
                    d["tags"] = []
                result.append(d)
            logger.debug(
                "Fetched %s context rows for agent %s", len(result), agent_id
            )
            return result
        except Exception as e:
            logger.error("Error fetching context documents: %s", e)
            raise
        finally:
            conn.close()

    async def get_context_document(
        self, agent_id: str, context_id: str
    ) -> Optional[dict]:
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT id, agent_id, context_type, title, content, tags,
                          created_at, updated_at
                   FROM context_documents WHERE id = ? AND agent_id = ?""",
                (context_id, agent_id),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            try:
                d["tags"] = json.loads(d["tags"])
            except (json.JSONDecodeError, TypeError):
                d["tags"] = []
            return d
        finally:
            conn.close()

    async def update_context_document_content(
        self, agent_id: str, context_id: str, content: str
    ) -> None:
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                """UPDATE context_documents SET content = ?, updated_at = ?
                   WHERE id = ? AND agent_id = ?""",
                (content, _now_iso(), context_id, agent_id),
            )
            conn.commit()
            if cursor.rowcount == 0:
                raise PermissionError(
                    "Context document not found or not owned by this agent",
                )
            logger.debug("Updated context row %s for agent %s", context_id, agent_id)
        except PermissionError:
            raise
        except Exception as e:
            logger.error("Error updating context row: %s", e)
            raise
        finally:
            conn.close()

    async def insert_document(
        self,
        agent_id: str,
        context_type: str,
        title: str,
        content: str,
        tags: Optional[List[str]] = None,
    ) -> str:
        conn = self._get_conn()
        try:
            doc_id = _uuid()
            conn.execute(
                """INSERT INTO context_documents (id, agent_id, context_type, title, content, tags)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (doc_id, agent_id, context_type, title, content, json.dumps(tags or [])),
            )
            conn.commit()
            logger.debug(
                "Inserted context %s type=%s for agent %s",
                doc_id, context_type, agent_id,
            )
            return doc_id
        except Exception as e:
            logger.error("Error inserting document: %s", e)
            raise
        finally:
            conn.close()

    async def delete_context_row(self, agent_id: str, context_id: str) -> None:
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "DELETE FROM context_documents WHERE id = ? AND agent_id = ?",
                (context_id, agent_id),
            )
            conn.commit()
            if cursor.rowcount == 0:
                raise PermissionError(
                    "Context document not found or not owned by this agent",
                )
            logger.debug("Deleted context row %s for agent %s", context_id, agent_id)
        except PermissionError:
            raise
        except Exception as e:
            logger.error("Error deleting context row: %s", e)
            raise
        finally:
            conn.close()

    async def delete_all_documents_for_agent(self, agent_id: str) -> int:
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "DELETE FROM context_documents WHERE agent_id = ?", (agent_id,)
            )
            conn.commit()
            deleted = cursor.rowcount
            logger.debug(
                "Deleted %s context rows for agent %s", deleted, agent_id
            )
            return deleted
        except Exception as e:
            logger.error("Error deleting context: %s", e)
            raise
        finally:
            conn.close()

    async def fetch_context_documents_for_agent(
        self,
        agent_id: str,
        context_types: Optional[List[str]] = None,
    ) -> List[dict]:
        agent = await self.get_agent_by_id(agent_id)
        if not agent:
            return []
        uid = agent["user_id"]
        conn = self._get_conn()
        try:
            if context_types:
                placeholders = ",".join("?" * len(context_types))
                sql = (
                    f"""SELECT id, user_id, context_type, title, content, tags,
                               created_at, updated_at
                        FROM context_documents
                        WHERE user_id = ? AND context_type IN ({placeholders})
                        ORDER BY context_type, title"""
                )
                rows = conn.execute(sql, (uid, *context_types)).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, user_id, context_type, title, content, tags,
                              created_at, updated_at
                       FROM context_documents
                       WHERE user_id = ?
                       ORDER BY context_type, title""",
                    (uid,),
                ).fetchall()
            result: List[dict] = []
            keys = [
                "id", "user_id", "context_type", "title", "content", "tags",
                "created_at", "updated_at",
            ]
            for row in rows:
                d = dict(zip(keys, row))
                try:
                    d["tags"] = json.loads(d["tags"])
                except (json.JSONDecodeError, TypeError):
                    d["tags"] = []
                result.append(d)
            return result
        finally:
            conn.close()

    async def get_context_document_for_agent(
        self, agent_id: str, context_id: str
    ) -> Optional[dict]:
        agent = await self.get_agent_by_id(agent_id)
        if not agent:
            return None
        uid = agent["user_id"]
        conn = self._get_conn()
        try:
            row = conn.execute(
                """SELECT id, user_id, context_type, title, content, tags,
                          created_at, updated_at
                   FROM context_documents WHERE id = ? AND user_id = ?""",
                (context_id, uid),
            ).fetchone()
            if not row:
                return None
            keys = [
                "id", "user_id", "context_type", "title", "content", "tags",
                "created_at", "updated_at",
            ]
            d = dict(zip(keys, row))
            try:
                d["tags"] = json.loads(d["tags"])
            except (json.JSONDecodeError, TypeError):
                d["tags"] = []
            return d
        finally:
            conn.close()

    async def update_context_document_content_for_agent(
        self, agent_id: str, context_id: str, content: str
    ) -> None:
        agent = await self.get_agent_by_id(agent_id)
        if not agent:
            raise PermissionError("Unknown agent")
        uid = agent["user_id"]
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                """UPDATE context_documents SET content = ?, updated_at = ?
                   WHERE id = ? AND user_id = ?""",
                (content, _now_iso(), context_id, uid),
            )
            conn.commit()
            if cursor.rowcount == 0:
                raise PermissionError(
                    "Context document not found or not owned by this agent",
                )
            logger.debug("Updated context row %s (agent-scoped)", context_id)
        except PermissionError:
            raise
        except Exception as e:
            logger.error("Error updating context row (agent-scoped): %s", e)
            raise
        finally:
            conn.close()

    async def insert_context_document_for_agent(
        self,
        agent_id: str,
        context_type: str,
        title: str,
        content: str,
        tags: Optional[List[str]] = None,
    ) -> str:
        agent = await self.get_agent_by_id(agent_id)
        if not agent:
            raise PermissionError("Unknown agent")
        uid = agent["user_id"]
        return await self.insert_document(
            uid, context_type, title, content, tags=tags,
        )

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
        """FTS5 keyword search across memory pages."""
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
        except Exception as e:
            logger.error("Error searching memories: %s", e)
            raise
        finally:
            conn.close()

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
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM agents WHERE user_id = ?", (user_id,)
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

    async def create_agent_for_user(self, user_id: str) -> dict:
        conn = self._get_conn()
        try:
            # Clone the default template
            tpl = conn.execute(
                "SELECT * FROM agent_templates WHERE id = 'default'"
            ).fetchone()
            if not tpl:
                # Fallback: inline default values
                tpl_data = {
                    "system_prompt": "",
                    "max_turn_count": 10,
                    "model": None,
                    "provider": None,
                    "temperature": 0.0,
                    "max_tokens": 4096,
                    "metadata": "{}",
                }
            else:
                tpl_data = dict(tpl)

            now = _now_iso()
            agent_id = _uuid()
            conn.execute(
                """INSERT INTO agents
                   (id, user_id, system_prompt, max_turn_count, model, provider,
                    temperature, max_tokens, status, metadata,
                    assigned_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
                (agent_id, user_id,
                 tpl_data.get("system_prompt", ""),
                 tpl_data.get("max_turn_count", 10),
                 tpl_data.get("model"),
                 tpl_data.get("provider"),
                 tpl_data.get("temperature", 0.0),
                 tpl_data.get("max_tokens", 4096),
                 tpl_data.get("metadata", "{}"),
                 now, now, now),
            )
            conn.commit()

            row = conn.execute(
                "SELECT * FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            logger.info("Created agent %s for user %s", agent_id, user_id)
            return dict(row)
        except Exception as e:
            logger.error("Error creating agent for user %s: %s", user_id, e)
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
            # Return sensible defaults if table empty
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
            return 10  # Default if agent not found
        finally:
            conn.close()

    # ---- Interrupt Handling ----

    async def set_interrupt(self, session_id: str) -> None:
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


# ── Proxy for code that uses supabase.Client.table() directly ──────────────

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
                    # Check if row exists
                    existing = conn.execute(
                        f"SELECT 1 FROM {self._table_name} WHERE {conflict_col} = ? LIMIT 1",
                        (data.get(conflict_col),),
                    ).fetchone()
                    if existing:
                        set_parts = [f"{k} = ?" for k in columns]
                        set_params = [data.get(c) for c in columns]
                        conn.execute(
                            f"UPDATE {self._table_name} SET {', '.join(set_parts)} WHERE {conflict_col} = ?",
                            set_params + [data.get(conflict_col)],
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
            params = where_params

            if self._order_by:
                order_parts = []
                for field, desc in self._order_by:
                    order_parts.append(f"{field} {'DESC' if desc else 'ASC'}")
                sql += " ORDER BY " + ", ".join(order_parts)

            if self._limit_val is not None:
                sql += f" LIMIT {self._limit_val}"

            rows = conn.execute(sql, params).fetchall()
            return _LocalQueryResult([dict(r) for r in rows])
        finally:
            conn.close()

    def update(self, data: dict) -> "_LocalQueryBuilder":
        """Start an UPDATE query. Chain with .eq() filters, then .execute()."""
        self._update_data = data
        return self

    def delete(self) -> "_LocalQueryBuilder":
        """Start a DELETE query. Chain with .eq() filters, then .execute()."""
        self._is_delete = True
        return self

    def insert(self, data: dict | list) -> "_LocalQueryBuilder":
        """Start an INSERT query. Chain with .execute()."""
        self._insert_data = data if isinstance(data, list) else [data]
        return self

    def upsert(self, data: dict, on_conflict: str = "") -> "_LocalQueryBuilder":
        """Start an UPSERT query. Chain with .execute()."""
        self._upsert_data = data
        self._on_conflict = on_conflict
        return self


class _LocalQueryResult:
    """Mimics supabase's query result with .data attribute."""

    def __init__(self, data: List[dict]):
        self.data = data
    def __bool__(self):
        return bool(self.data)

"""
Canonical table definitions for webAgent.

Each table is represented as a dict:
{
    "name": str,
    "columns": [Column(name, type, nullable, default, primary_key, unique, references)],
    "constraints": [str]  # raw constraint strings (e.g. CHECK, UNIQUE composite)
}

Column.type is dialect-neutral (e.g. "TEXT", "INTEGER", "REAL", "JSON", "BLOB",
"TIMESTAMP"). The renderer translates per-dialect.

Indexes and triggers live in separate lists keyed by table name.
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Column:
    name: str
    type: str
    nullable: bool = True
    default: Optional[str] = None  # raw SQL default expression (e.g. "'{}'", "0", "now()")
    primary_key: bool = False
    unique: bool = False
    references: Optional[str] = None  # e.g. "sessions(id)"
    on_delete: Optional[str] = None   # e.g. "CASCADE"
    # For type == "VECTOR": embedding dimensionality. Postgres renders
    # vector(<dim>) (pgvector); SQLite/MySQL fall back to BLOB (raw float32 bytes).
    vector_dim: Optional[int] = None


@dataclass
class Table:
    name: str
    columns: List[Column] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)  # composite UNIQUE, CHECK


@dataclass
class Index:
    name: str
    table: str
    columns: str  # raw column list, e.g. "user_id, slug" or "title COLLATE NOCASE"
    unique: bool = False


@dataclass
class FtsTable:
    """SQLite FTS5 virtual table. Postgres uses tsvector instead (rendered as TEXT + GIN)."""
    name: str
    content_table: str
    indexed_columns: List[str]
    unindexed_columns: List[str] = field(default_factory=list)


@dataclass
class Trigger:
    """SQLite-only trigger (FTS sync). Skipped by Postgres/MySQL renderers."""
    name: str
    body: str  # raw SQL body, SQLite syntax


# ── Tables ──────────────────────────────────────────────────────────────────

TABLES: List[Table] = [
    Table("sessions", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("title", "TEXT"),
        Column("metadata", "TEXT"),
        Column("agent_id", "TEXT"),
        Column("participants", "TEXT", default="'[]'"),
        Column("pinned", "INTEGER", nullable=False, default="0"),
        Column("sort_order", "INTEGER"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("interactions", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("session_id", "TEXT", nullable=False, references="sessions(id)"),
        Column("parent_id", "TEXT"),
        Column("role", "TEXT", nullable=False),
        Column("content", "TEXT", nullable=False),
        Column("tool_name", "TEXT"),
        Column("tool_call_id", "TEXT"),
        Column("channel", "TEXT"),
        Column("metadata", "TEXT"),
        Column("input", "TEXT"),
        Column("output", "TEXT"),
        Column("source", "TEXT"),
        Column("from_id", "TEXT"),
        Column("to_id", "TEXT"),
        # Stream-persistence ordering (added 2026-05-24). NULL on legacy rows;
        # callers fall back to created_at ordering when session_seq IS NULL.
        Column("session_seq", "INTEGER"),
        Column("turn_id", "TEXT"),
        Column("turn_seq", "INTEGER"),
        # Message lifecycle (added 2026-05-29). Legacy + finished rows are
        # 'complete'; an assistant answer is 'streaming' while being generated,
        # then 'complete' / 'interrupted' / 'error'. Makes the partial answer
        # durable (survives RunBuffer eviction and server restarts).
        Column("status", "TEXT", nullable=False, default="'complete'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    # Per-session durable run state — the in-flight (or recently finished) agent
    # turn. Lets a cold/second device (even after a server restart) discover an
    # active run from the DB and where the live stream is up to, independent of
    # the in-memory RunBuffer. See app/agent/run_manager.py.
    Table("session_runs", [
        Column("session_id", "TEXT", nullable=False, primary_key=True, references="sessions(id)"),
        Column("user_id", "TEXT", nullable=False),
        Column("agent_id", "TEXT"),
        Column("turn_id", "TEXT"),
        Column("assistant_interaction_id", "TEXT"),
        Column("status", "TEXT", nullable=False, default="'running'"),
        Column("latest_session_seq", "INTEGER", nullable=False, default="0"),
        Column("error", "TEXT"),
        Column("started_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        # Self-healing / auto-resume bookkeeping (see migrations/025_run_self_healing.sql).
        Column("stop_cause", "TEXT"),
        Column("origin", "TEXT"),
        Column("resume_attempts", "INTEGER", nullable=False, default="0"),
        Column("max_resume_attempts", "INTEGER"),
        Column("heartbeat_at", "TIMESTAMP"),
        Column("next_resume_at", "TIMESTAMP"),
        Column("owner_token", "TEXT"),
        Column("lease_expires_at", "TIMESTAMP"),
        Column("relaunch_ctx", "TEXT"),
    ]),

    # Diagnostic flight-recorder (see app/agent/diagnostics.py). Rolling,
    # auto-pruned log of server warnings/errors, agent-loop pipeline problems and
    # run outcomes. session_id/agent_id are free TEXT (NOT foreign keys) so a
    # record outlives the row it referenced and a delete never cascades it away.
    Table("diagnostics", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("ts", "TEXT", nullable=False),
        Column("level", "TEXT", nullable=False),
        Column("category", "TEXT", nullable=False),
        Column("source", "TEXT"),
        Column("message", "TEXT", nullable=False),
        Column("detail", "TEXT"),
        Column("session_id", "TEXT"),
        Column("turn_id", "TEXT"),
        Column("agent_id", "TEXT"),
        Column("user_id", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    # Client render recorder (see app/agent/render_recorder.py + ui/js/recorder.js).
    # Rolling, auto-pruned record of what the BROWSER rendered and felt — HTML
    # snapshots, DOM-mutation deltas, lag/long-task metrics, JS errors, console
    # warnings and failed network calls. Every row carries session_seq, the same
    # monotonic per-session counter on WS events / interactions, so a render
    # moment joins exactly to the interaction / diagnostics row beside it.
    Table("render_recordings", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("ts", "TEXT", nullable=False),
        Column("recv_ts", "TEXT"),
        Column("kind", "TEXT", nullable=False),
        Column("session_id", "TEXT"),
        Column("turn_id", "TEXT"),
        Column("session_seq", "INTEGER"),
        Column("user_id", "TEXT"),
        Column("agent_id", "TEXT"),
        Column("client_id", "TEXT"),
        Column("seq", "INTEGER"),
        Column("url", "TEXT"),
        Column("level", "TEXT"),
        Column("label", "TEXT"),
        Column("value_num", "REAL"),
        Column("detail", "TEXT"),
        Column("html", "TEXT"),
        Column("html_bytes", "INTEGER"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("session_summaries", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("session_id", "TEXT", nullable=False, unique=True, references="sessions(id)"),
        Column("title", "TEXT"),
        Column("summary", "TEXT", nullable=False),
        Column("message_count", "INTEGER", nullable=False, default="0"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("agent_templates", [
        Column("id", "TEXT", nullable=False, primary_key=True, default="'default'"),
        Column("max_turn_count", "INTEGER", nullable=False, default="0"),
        Column("max_wall_seconds", "REAL"),
        Column("max_identical_tool_calls", "INTEGER", nullable=False, default="0"),
        Column("max_stall_strikes", "INTEGER", nullable=False, default="0"),
        Column("model", "TEXT"),
        Column("provider", "TEXT"),
        Column("temperature", "REAL", nullable=False, default="0.0"),
        Column("max_tokens", "INTEGER", nullable=False, default="4096"),
        Column("metadata", "TEXT", nullable=False, default="'{}'"),
        Column("trigger_type", "TEXT", nullable=False, default="'user_input'"),
        Column("trigger_key", "TEXT"),
        Column("loop_logic", "TEXT", nullable=False, default="'[]'"),
        Column("abilities", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("agents", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("template_id", "TEXT"),
        Column("name", "TEXT", nullable=False, default="''"),
        Column("description", "TEXT", nullable=False, default="''"),
        Column("is_user_default", "INTEGER", nullable=False, default="0"),
        Column("max_turn_count", "INTEGER", nullable=False, default="0"),
        Column("max_wall_seconds", "REAL"),
        Column("max_identical_tool_calls", "INTEGER", nullable=False, default="0"),
        Column("max_stall_strikes", "INTEGER", nullable=False, default="0"),
        Column("model", "TEXT"),
        Column("provider", "TEXT"),
        Column("temperature", "REAL", nullable=False, default="0.0"),
        Column("max_tokens", "INTEGER", nullable=False, default="4096"),
        Column("status", "TEXT", nullable=False, default="'active'"),
        Column("metadata", "TEXT", nullable=False, default="'{}'"),
        Column("trigger_type", "TEXT", nullable=False, default="'user_input'"),
        Column("trigger_key", "TEXT"),
        Column("loop_logic", "TEXT", nullable=False, default="'[]'"),
        Column("assigned_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("turn_count", "INTEGER", nullable=False, default="0"),
        Column("admin_users", "TEXT", nullable=False, default="'[]'"),
        Column("member_users", "TEXT", nullable=False, default="'[]'"),
        Column("authorized_users", "TEXT", nullable=False, default="'[]'"),
        Column("user_mode", "TEXT", nullable=False, default="'anonymous'"),
        Column("sort_order", "INTEGER"),
    ]),

    Table("agent_prompts", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("agent_id", "TEXT", nullable=False),
        Column("slot_name", "TEXT", nullable=False),
        Column("user_id", "TEXT"),
        Column("order_index", "INTEGER"),
        Column("lock", "INTEGER"),
        Column("merge_mode", "TEXT"),
        Column("content", "TEXT", nullable=False, default="''"),
        # Origin version this row was cloned from (in agent_prompt_templates).
        # NULL on user-override rows and on legacy rows pre-versioning.
        Column("template_version", "INTEGER"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_by", "TEXT"),
    ]),

    # Canonical prompt-slot templates. Source of truth for slot defaults.
    # JSON files in data/agents/*.json seed this table; admin edits
    # promoted here are protected from JSON re-seed via source='admin'.
    # When a new agent is created from a template, rows here are cloned
    # into agent_prompts under that agent's id.
    Table("agent_prompt_templates", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("template_id", "TEXT", nullable=False),
        Column("slot_name", "TEXT", nullable=False),
        Column("order_index", "INTEGER", nullable=False, default="0"),
        Column("lock", "INTEGER", nullable=False, default="0"),
        Column("merge_mode", "TEXT", nullable=False, default="'replace'"),
        Column("content", "TEXT", nullable=False, default="''"),
        Column("version", "INTEGER", nullable=False, default="1"),
        # 'json' = seeded from JSON file (re-seed may overwrite).
        # 'admin' = promoted from admin UI edit (re-seed SKIPS this row).
        Column("source", "TEXT", nullable=False, default="'json'"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_by", "TEXT", nullable=False, default="'system'"),
    ], constraints=[
        "UNIQUE(template_id, slot_name)",
        "CHECK (source IN ('json','admin'))",
    ]),

    # Generic key/value store for cross-cutting runtime metadata
    # (manifest hashes, schema versions, feature toggles, etc.).
    # Use sparingly — prefer typed tables for domain data.
    Table("app_meta", [
        Column("key", "TEXT", nullable=False, primary_key=True),
        Column("value", "TEXT", nullable=False, default="''"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("agent_connections", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("agent_id", "TEXT", nullable=False, references="agents(id)", on_delete="CASCADE"),
        Column("connection_type", "TEXT", nullable=False),
        Column("section", "TEXT", nullable=False, default="'channel'"),
        Column("enabled", "INTEGER", nullable=False, default="0"),
        Column("config", "TEXT", nullable=False, default="'{}'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=["UNIQUE(agent_id, connection_type)"]),

    Table("agent_abilities", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("agent_id", "TEXT", nullable=False, references="agents(id)", on_delete="CASCADE"),
        Column("ability_id", "TEXT", nullable=False),
        Column("source", "TEXT", nullable=False, default="'platform'"),
        Column("enabled", "INTEGER", nullable=False, default="0"),
        Column("byo_client_id", "TEXT", nullable=False, default="''"),
        Column("byo_client_secret_ref", "TEXT", nullable=False, default="''"),
        Column("config", "TEXT", nullable=False, default="'{}'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=["UNIQUE(agent_id, ability_id)"]),

    Table("memories", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("slug", "TEXT", nullable=False),
        Column("page_type", "TEXT", nullable=False),
        Column("title", "TEXT", nullable=False),
        Column("compiled_truth", "TEXT", nullable=False, default="''"),
        Column("timeline", "TEXT", nullable=False, default="''"),
        Column("frontmatter", "TEXT", nullable=False, default="'{}'"),
        Column("content_hash", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "UNIQUE(user_id, slug)",
        "CHECK (page_type IN ('person','company','deal','meeting','project','idea','concept','writing','program','personal','media','inbox','archive'))",
    ]),

    Table("memory_chunks", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("memory_id", "TEXT", nullable=False, references="memories(id)", on_delete="CASCADE"),
        Column("chunk_index", "INTEGER", nullable=False),
        Column("chunk_text", "TEXT", nullable=False),
        Column("chunk_source", "TEXT", nullable=False, default="'compiled_truth'"),
        Column("embedding", "VECTOR", vector_dim=1536),
        Column("token_count", "INTEGER"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "UNIQUE(memory_id, chunk_index)",
        "CHECK (chunk_source IN ('compiled_truth', 'timeline'))",
    ]),

    Table("memory_links", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("from_slug", "TEXT", nullable=False),
        Column("to_slug", "TEXT", nullable=False),
        Column("link_type", "TEXT", nullable=False),
        Column("context", "TEXT"),
        Column("weight", "INTEGER", nullable=False, default="1"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "UNIQUE(user_id, from_slug, to_slug, link_type)",
        "CHECK (link_type IN ('works_at','founded','invested_in','advises','attended','knows','partnered_with','acquired','competes_with','references','related_to'))",
    ]),

    Table("memory_timeline", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("memory_id", "TEXT", nullable=False, references="memories(id)", on_delete="CASCADE"),
        Column("event_date", "TEXT", nullable=False),
        Column("source", "TEXT", nullable=False),
        Column("summary", "TEXT", nullable=False),
        Column("detail", "TEXT"),
        Column("source_ref", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("tools", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("name", "TEXT", nullable=False),
        Column("code", "TEXT", nullable=False),
        Column("description", "TEXT", nullable=False),
        Column("parameters", "TEXT", nullable=False, default="'{}'"),
        Column("language", "TEXT", nullable=False, default="'python'"),
        Column("status", "TEXT", nullable=False, default="'active'"),
        Column("created_by", "TEXT", nullable=False),
        Column("stages", "TEXT", nullable=False, default="'[]'"),
        Column("destructive", "INTEGER", nullable=False, default="0"),
        Column("agent_types", "TEXT", nullable=False, default="'[]'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("agent_credentials", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("tool_name", "TEXT", nullable=False),
        Column("credential_type", "TEXT", nullable=False),
        Column("encrypted_data", "TEXT", nullable=False),
        Column("display_name", "TEXT"),
        Column("expires_at", "TEXT"),
        Column("scopes", "TEXT", default="'[]'"),
        Column("last_used_at", "TEXT"),
        Column("use_count", "INTEGER", nullable=False, default="0"),
        Column("is_active", "INTEGER", nullable=False, default="1"),
        Column("requires_renewal", "INTEGER", nullable=False, default="0"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("skills", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("name", "TEXT", nullable=False),
        Column("description", "TEXT"),
        Column("code", "TEXT"),
        Column("version", "INTEGER", nullable=False, default="1"),
        Column("base_skill_id", "TEXT"),
        Column("is_official", "INTEGER", nullable=False, default="0"),
        Column("tags", "TEXT", nullable=False, default="'[]'"),
        Column("is_active", "INTEGER", nullable=False, default="1"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("skill_executions", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("skill_id", "TEXT", nullable=False, references="skills(id)", on_delete="CASCADE"),
        Column("user_id", "TEXT", nullable=False),
        Column("session_id", "TEXT", nullable=False, references="sessions(id)"),
        Column("interaction_id", "TEXT"),
        Column("success", "INTEGER", nullable=False, default="1"),
        Column("duration_ms", "INTEGER", nullable=False, default="0"),
        Column("steps_to_complete", "INTEGER", nullable=False, default="1"),
        Column("error_message", "TEXT"),
        Column("input_params", "TEXT", default="'{}'"),
        Column("output_summary", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("skill_feedback", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("skill_id", "TEXT", nullable=False, references="skills(id)", on_delete="CASCADE"),
        Column("execution_id", "TEXT", references="skill_executions(id)", on_delete="SET NULL"),
        Column("user_id", "TEXT", nullable=False),
        Column("feedback_type", "TEXT", nullable=False),
        Column("message", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=["CHECK (feedback_type IN ('positive','negative','correction'))"]),

    Table("session_interrupts", [
        Column("session_id", "TEXT", nullable=False, primary_key=True, references="sessions(id)", on_delete="CASCADE"),
        Column("interrupt_requested", "INTEGER", nullable=False, default="1"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("attachments", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("session_id", "TEXT", references="sessions(id)"),
        Column("original_name", "TEXT", nullable=False),
        Column("mime_type", "TEXT", nullable=False),
        Column("size_bytes", "INTEGER", nullable=False),
        Column("storage_path", "TEXT", nullable=False),
        Column("storage_provider", "TEXT", nullable=False, default="'local'"),
        Column("metadata", "TEXT", nullable=False, default="'{}'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("channel_identities", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("channel", "TEXT", nullable=False),
        Column("external_id", "TEXT", nullable=False),
        Column("user_id", "TEXT", nullable=False),
        Column("user_tier", "TEXT", nullable=False, default="'anonymous'"),
        Column("display_name", "TEXT", nullable=False, default="''"),
        Column("email", "TEXT", nullable=False, default="''"),
        Column("email_verified", "INTEGER", nullable=False, default="0"),
        Column("linked_user_id", "TEXT"),
        Column("metadata", "TEXT", nullable=False, default="'{}'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "UNIQUE(channel, external_id)",
        "CHECK (user_tier IN ('anonymous','channel_verified','full'))",
    ]),

    Table("linking_codes", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("code", "TEXT", nullable=False, unique=True),
        Column("source_user_id", "TEXT", nullable=False),
        Column("target_channel", "TEXT", nullable=False, default="''"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("expires_at", "TEXT", nullable=False),
        Column("used", "INTEGER", nullable=False, default="0"),
    ]),

    Table("webhook_registrations", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("name", "TEXT", nullable=False),
        Column("instructions", "TEXT", nullable=False, default="''"),
        Column("active", "INTEGER", nullable=False, default="1"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("webhook_event_log", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("webhook_id", "TEXT", nullable=False, references="webhook_registrations(id)", on_delete="CASCADE"),
        Column("method", "TEXT", nullable=False),
        Column("headers", "TEXT", nullable=False, default="'{}'"),
        Column("payload", "TEXT", nullable=False, default="''"),
        Column("response_status", "INTEGER", nullable=False, default="200"),
        Column("response_body", "TEXT", nullable=False, default="''"),
        Column("duration_ms", "INTEGER", nullable=False, default="0"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("auth_elements", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("service", "TEXT", nullable=False),
        Column("label", "TEXT", nullable=False, default="'default'"),
        Column("config", "TEXT", nullable=False, default="'{}'"),
        Column("secret_ref", "TEXT", nullable=False, default="''"),
        Column("is_active", "INTEGER", nullable=False, default="1"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("provider_ratings", [
        Column("user_id", "TEXT", nullable=False),
        Column("provider", "TEXT", nullable=False),
        Column("model", "TEXT", nullable=False),
        Column("rating", "INTEGER", nullable=False, default="0"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=["PRIMARY KEY (user_id, provider, model)"]),

    Table("user_profiles", [
        Column("user_id", "TEXT", nullable=False, primary_key=True),
        Column("is_admin", "INTEGER", nullable=False, default="0"),
        Column("default_agent_id", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("last_login_at", "TEXT"),
    ]),

    Table("data_sources", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("name", "TEXT", nullable=False),
        Column("type", "TEXT", nullable=False),
        Column("config", "TEXT", nullable=False, default="'{}'"),
        Column("auth_element_id", "TEXT"),
        Column("schema_cache", "TEXT", nullable=False, default="'{}'"),
        Column("safety_policy", "TEXT", nullable=False, default="'{}'"),
        Column("status", "TEXT", nullable=False, default="'unverified'"),
        Column("last_test_message", "TEXT"),
        Column("last_tested_at", "TIMESTAMP"),
        Column("last_introspected_at", "TIMESTAMP"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "UNIQUE(user_id, name)",
        "CHECK (type IN ('sql_postgres','sql_mysql','rest_api','doc_store','web_search_domain','notion','confluence','shopify','airtable','google_sheets'))",
        "CHECK (status IN ('unverified','active','error','disabled'))",
    ]),

    Table("agent_data_sources", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("agent_id", "TEXT", nullable=False, references="agents(id)", on_delete="CASCADE"),
        Column("data_source_id", "TEXT", nullable=False, references="data_sources(id)", on_delete="CASCADE"),
        Column("tool_alias", "TEXT"),
        Column("enabled", "INTEGER", nullable=False, default="1"),
        Column("inject_schema_in_prompt", "INTEGER", nullable=False, default="1"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=["UNIQUE(agent_id, data_source_id)"]),

    Table("doc_chunks", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("data_source_id", "TEXT", nullable=False, references="data_sources(id)", on_delete="CASCADE"),
        Column("source_ref", "TEXT", nullable=False, default="''"),
        Column("chunk_index", "INTEGER", nullable=False, default="0"),
        Column("chunk_text", "TEXT", nullable=False),
        Column("content_hash", "TEXT"),
        Column("embedding", "VECTOR", vector_dim=1536),
        Column("token_count", "INTEGER"),
        Column("metadata", "TEXT", nullable=False, default="'{}'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    # Tenant key metadata. NO key material lives here — wrapped DEKs live in
    # the configured SecretsBackend at "wa:dek:<user_id>:v<key_version>".
    # This table only tracks which versions exist and which is active per tenant.
    Table("tenant_key_meta", [
        Column("user_id", "TEXT", nullable=False),
        Column("key_version", "INTEGER", nullable=False),
        Column("algo", "TEXT", nullable=False, default="'fernet'"),
        Column("status", "TEXT", nullable=False, default="'active'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("retired_at", "TIMESTAMP"),
    ], constraints=[
        "PRIMARY KEY (user_id, key_version)",
        "CHECK (status IN ('active','retired'))",
    ]),

    # ── Billing / monetization ──
    # Effective config is the platform row merged with an agent-scoped row.
    # scope: 'platform' or 'agent:<agent_id>'.
    Table("billing_configs", [
        Column("scope", "TEXT", nullable=False, primary_key=True),
        Column("strategy", "TEXT", nullable=False, default="'free'"),
        Column("allowed_strategies", "TEXT", nullable=False, default="'[]'"),
        Column("allowed_processors", "TEXT", nullable=False, default="'[]'"),
        Column("rate_card_default_llm", "TEXT", nullable=False, default="'{}'"),
        Column("rate_card_byo_llm", "TEXT", nullable=False, default="'{}'"),
        Column("platform_fee_pct", "REAL", nullable=False, default="0"),
        Column("platform_fee_flat_cents", "INTEGER", nullable=False, default="0"),
        Column("trial_config", "TEXT", nullable=False, default="'{}'"),
        Column("subscription_price_cents", "INTEGER", nullable=False, default="0"),
        Column("currency", "TEXT", nullable=False, default="'usd'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_by", "TEXT"),
    ], constraints=[
        "CHECK (strategy IN ('free','credits','per_message','per_token','subscription','trial'))",
    ]),

    Table("usage_events", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("agent_id", "TEXT", nullable=False),
        Column("user_id", "TEXT", nullable=False),
        Column("interaction_id", "TEXT"),
        Column("input_tokens", "INTEGER", nullable=False, default="0"),
        Column("output_tokens", "INTEGER", nullable=False, default="0"),
        Column("provider_cost_cents", "INTEGER", nullable=False, default="0"),
        Column("end_user_charge_cents", "INTEGER", nullable=False, default="0"),
        Column("platform_fee_cents", "INTEGER", nullable=False, default="0"),
        Column("agent_admin_earnings_cents", "INTEGER", nullable=False, default="0"),
        Column("strategy", "TEXT", nullable=False, default="'free'"),
        Column("is_byo_llm", "INTEGER", nullable=False, default="0"),
        Column("is_trial", "INTEGER", nullable=False, default="0"),
        Column("is_exempt", "INTEGER", nullable=False, default="0"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("wallets", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("owner_type", "TEXT", nullable=False),
        Column("owner_id", "TEXT", nullable=False),
        Column("balance_cents", "INTEGER", nullable=False, default="0"),
        Column("hold_cents", "INTEGER", nullable=False, default="0"),
        Column("currency", "TEXT", nullable=False, default="'usd'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "UNIQUE(owner_type, owner_id, currency)",
        "CHECK (owner_type IN ('user','agent_admin'))",
    ]),

    Table("wallet_transactions", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("wallet_id", "TEXT", nullable=False),
        Column("delta_cents", "INTEGER", nullable=False),
        Column("kind", "TEXT", nullable=False),
        Column("ref_id", "TEXT"),
        Column("note", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "CHECK (kind IN ('purchase','usage','refund','platform_fee','earnings','hold','release'))",
    ]),

    Table("subscriptions", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("agent_id", "TEXT", nullable=False),
        Column("processor", "TEXT", nullable=False),
        Column("external_subscription_id", "TEXT"),
        Column("status", "TEXT", nullable=False, default="'pending'"),
        Column("current_period_end", "TIMESTAMP"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "UNIQUE(user_id, agent_id)",
        "CHECK (status IN ('pending','active','past_due','cancelled','expired'))",
    ]),

    Table("trials", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("agent_id", "TEXT", nullable=False),
        Column("started_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("expires_at", "TIMESTAMP"),
        Column("messages_remaining", "INTEGER"),
        Column("tokens_remaining", "INTEGER"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "UNIQUE(user_id, agent_id)",
    ]),

    Table("payment_accounts", [
        Column("user_id", "TEXT", nullable=False),
        Column("processor", "TEXT", nullable=False),
        Column("external_account_id", "TEXT"),
        Column("onboarding_complete", "INTEGER", nullable=False, default="0"),
        Column("metadata", "TEXT", nullable=False, default="'{}'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "PRIMARY KEY (user_id, processor)",
    ]),

    Table("payments", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("processor", "TEXT", nullable=False),
        Column("external_payment_id", "TEXT"),
        Column("user_id", "TEXT", nullable=False),
        Column("agent_id", "TEXT"),
        Column("amount_cents", "INTEGER", nullable=False, default="0"),
        Column("currency", "TEXT", nullable=False, default="'usd'"),
        Column("kind", "TEXT", nullable=False),
        Column("status", "TEXT", nullable=False, default="'pending'"),
        Column("metadata", "TEXT", nullable=False, default="'{}'"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "CHECK (kind IN ('purchase','subscription','one_off'))",
        "CHECK (status IN ('pending','completed','failed','refunded'))",
    ]),

    # Exemption rules. Three kinds:
    #   'agent'           — whole agent is free (agent_id set, user_id NULL)
    #   'user'            — user is exempt globally (user_id set, agent_id NULL)
    #   'user_for_agent'  — user is exempt for one agent (both set)
    Table("billing_exemptions", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("kind", "TEXT", nullable=False),
        Column("agent_id", "TEXT"),
        Column("user_id", "TEXT"),
        Column("granted_by_user_id", "TEXT"),
        Column("reason", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "CHECK (kind IN ('agent','user','user_for_agent'))",
    ]),

    # ── Automation / events (cron + push/poll triggers; see migration 016) ──
    Table("agent_automations", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("agent_id", "TEXT", nullable=False),
        Column("owner_user_id", "TEXT", nullable=False),
        Column("task_label", "TEXT", nullable=False, default="''"),
        Column("prompt", "TEXT", nullable=False, default="''"),
        Column("schedule_cron", "TEXT", nullable=False, default="''"),
        Column("schedule_natural", "TEXT", nullable=False, default="''"),
        Column("timezone", "TEXT", nullable=False, default="'UTC'"),
        Column("channel", "TEXT"),
        Column("channel_recipient", "TEXT"),
        Column("silent", "INTEGER", nullable=False, default="0"),
        Column("enabled", "INTEGER", nullable=False, default="1"),
        Column("next_run_at", "TEXT"),
        Column("last_run_at", "TEXT"),
        Column("last_status", "TEXT"),
        Column("last_error", "TEXT"),
        Column("last_session_id", "TEXT"),
        Column("source_hash", "TEXT", nullable=False, default="''"),
        Column("fire_token", "TEXT"),
        Column("external_job_id", "TEXT"),
        Column("external_provider", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("agent_event_subscriptions", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("agent_id", "TEXT", nullable=False),
        Column("owner_user_id", "TEXT", nullable=False),
        Column("source", "TEXT", nullable=False),
        Column("event_type", "TEXT", nullable=False),
        Column("filter_json", "TEXT", nullable=False, default="'{}'"),
        Column("task_label", "TEXT", nullable=False, default="''"),
        Column("prompt", "TEXT", nullable=False, default="''"),
        Column("trigger_natural", "TEXT", nullable=False, default="''"),
        Column("channel", "TEXT"),
        Column("channel_recipient", "TEXT"),
        Column("silent", "INTEGER", nullable=False, default="0"),
        Column("enabled", "INTEGER", nullable=False, default="1"),
        Column("external_subscription_id", "TEXT"),
        Column("external_resource_id", "TEXT"),
        Column("external_expiration_at", "TEXT"),
        Column("external_metadata", "TEXT", nullable=False, default="'{}'"),
        Column("poll_cursor", "TEXT"),
        Column("poll_interval_seconds", "INTEGER"),
        Column("last_polled_at", "TEXT"),
        Column("last_event_at", "TEXT"),
        Column("last_event_external_id", "TEXT"),
        Column("last_status", "TEXT"),
        Column("last_error", "TEXT"),
        Column("last_session_id", "TEXT"),
        Column("fire_count", "INTEGER", nullable=False, default="0"),
        Column("source_hash", "TEXT", nullable=False, default="''"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    Table("event_deliveries", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("subscription_id", "TEXT", nullable=False),
        Column("source", "TEXT", nullable=False),
        Column("event_type", "TEXT", nullable=False),
        Column("event_external_id", "TEXT", nullable=False),
        Column("owner_user_id", "TEXT", nullable=False),
        Column("agent_id", "TEXT", nullable=False),
        Column("session_id", "TEXT"),
        Column("status", "TEXT", nullable=False, default="'pending'"),
        Column("error", "TEXT"),
        Column("payload_excerpt", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ]),

    # Visualizer workspace pages (see migration 019).
    Table("pages", [
        Column("id", "TEXT", nullable=False, primary_key=True),
        Column("user_id", "TEXT", nullable=False),
        Column("slug", "TEXT", nullable=False),
        Column("title", "TEXT", nullable=False),
        Column("agent_context", "TEXT", nullable=False, default="''"),
        Column("html", "TEXT"),
        Column("created_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
        Column("updated_at", "TIMESTAMP", nullable=False, default="CURRENT_TIMESTAMP"),
    ], constraints=[
        "UNIQUE(user_id, slug)",
    ]),
]


# ── Indexes ─────────────────────────────────────────────────────────────────

INDEXES: List[Index] = [
    Index("idx_interactions_session", "interactions", "session_id"),
    Index("idx_interactions_created", "interactions", "created_at"),
    Index("idx_interactions_session_seq", "interactions", "session_id, session_seq"),
    Index("idx_interactions_turn", "interactions", "turn_id"),
    Index("idx_diagnostics_ts", "diagnostics", "ts"),
    Index("idx_diagnostics_level", "diagnostics", "level"),
    Index("idx_diagnostics_category", "diagnostics", "category"),
    Index("idx_diagnostics_session", "diagnostics", "session_id"),
    Index("idx_render_rec_ts", "render_recordings", "ts"),
    Index("idx_render_rec_session", "render_recordings", "session_id"),
    Index("idx_render_rec_kind", "render_recordings", "kind"),
    Index("idx_render_rec_seq", "render_recordings", "session_seq"),
    Index("idx_summaries_user", "session_summaries", "user_id"),
    Index("idx_agent_prompts_agent", "agent_prompts", "agent_id"),
    Index("idx_agent_prompts_user", "agent_prompts", "user_id"),
    Index("idx_agent_prompt_templates_tpl", "agent_prompt_templates", "template_id"),
    Index("idx_agent_conn_agent", "agent_connections", "agent_id"),
    Index("idx_agent_conn_type", "agent_connections", "connection_type"),
    Index("idx_agent_ability_agent", "agent_abilities", "agent_id"),
    Index("idx_agent_ability_id", "agent_abilities", "ability_id"),
    Index("idx_memories_user", "memories", "user_id"),
    Index("idx_memories_type", "memories", "page_type"),
    Index("idx_memories_updated", "memories", "updated_at DESC"),
    Index("idx_chunks_memory", "memory_chunks", "memory_id"),
    Index("idx_chunks_source", "memory_chunks", "chunk_source"),
    Index("idx_links_from", "memory_links", "from_slug"),
    Index("idx_links_to", "memory_links", "to_slug"),
    Index("idx_links_type", "memory_links", "link_type"),
    Index("idx_links_user", "memory_links", "user_id"),
    Index("idx_timeline_memory", "memory_timeline", "memory_id"),
    Index("idx_timeline_date", "memory_timeline", "event_date DESC"),
    Index("idx_tools_status", "tools", "status"),
    Index("idx_tools_creator", "tools", "created_by"),
    Index("idx_skills_user", "skills", "user_id"),
    Index("idx_skills_name", "skills", "name"),
    Index("idx_skills_official", "skills", "is_official"),
    Index("idx_exec_skill", "skill_executions", "skill_id"),
    Index("idx_exec_user", "skill_executions", "user_id"),
    Index("idx_exec_created", "skill_executions", "created_at"),
    Index("idx_exec_success", "skill_executions", "success"),
    Index("idx_feedback_skill", "skill_feedback", "skill_id"),
    Index("idx_feedback_user", "skill_feedback", "user_id"),
    Index("idx_feedback_type", "skill_feedback", "feedback_type"),
    Index("idx_attachments_user", "attachments", "user_id"),
    Index("idx_attachments_session", "attachments", "session_id"),
    Index("idx_channel_user", "channel_identities", "user_id"),
    Index("idx_channel_channel_ext", "channel_identities", "channel, external_id"),
    Index("idx_linking_codes_code", "linking_codes", "code"),
    Index("idx_webhook_reg_user", "webhook_registrations", "user_id"),
    Index("idx_webhook_log_hook", "webhook_event_log", "webhook_id"),
    Index("idx_webhook_log_created", "webhook_event_log", "created_at DESC"),
    # Unique
    Index("idx_tools_name", "tools", "name", unique=True),
    Index("idx_auth_elements_user_service_label", "auth_elements", "user_id, service, label", unique=True),
    # data sources
    Index("idx_data_sources_user", "data_sources", "user_id"),
    Index("idx_data_sources_type", "data_sources", "type"),
    Index("idx_agent_data_sources_agent", "agent_data_sources", "agent_id"),
    Index("idx_agent_data_sources_source", "agent_data_sources", "data_source_id"),
    Index("idx_tenant_key_meta_active", "tenant_key_meta", "user_id, status"),
    Index("idx_doc_chunks_source", "doc_chunks", "data_source_id"),
    Index("idx_doc_chunks_hash", "doc_chunks", "content_hash"),
    # Billing
    Index("idx_usage_events_agent_created", "usage_events", "agent_id, created_at"),
    Index("idx_usage_events_user_created", "usage_events", "user_id, created_at"),
    Index("idx_wallet_tx_wallet", "wallet_transactions", "wallet_id"),
    Index("idx_payments_user", "payments", "user_id"),
    Index("idx_payments_external", "payments", "processor, external_payment_id"),
    Index("idx_subscriptions_user", "subscriptions", "user_id"),
    Index("idx_subscriptions_agent", "subscriptions", "agent_id"),
    Index("idx_trials_user_agent", "trials", "user_id, agent_id"),
    Index("idx_exemptions_agent", "billing_exemptions", "agent_id"),
    Index("idx_exemptions_user", "billing_exemptions", "user_id"),
    Index("idx_exemptions_kind", "billing_exemptions", "kind"),
    # Automation / events
    Index("idx_agent_automations_agent", "agent_automations", "agent_id"),
    Index("idx_agent_automations_owner", "agent_automations", "owner_user_id"),
    Index("idx_agent_automations_next", "agent_automations", "next_run_at"),
    Index("idx_agent_automations_hash", "agent_automations", "agent_id, owner_user_id, source_hash", unique=True),
    Index("idx_evt_sub_agent", "agent_event_subscriptions", "agent_id"),
    Index("idx_evt_sub_owner", "agent_event_subscriptions", "owner_user_id"),
    Index("idx_evt_sub_source", "agent_event_subscriptions", "source, event_type"),
    Index("idx_evt_sub_ext", "agent_event_subscriptions", "source, external_subscription_id"),
    Index("idx_evt_sub_expiry", "agent_event_subscriptions", "external_expiration_at"),
    Index("idx_evt_sub_poll", "agent_event_subscriptions", "source, last_polled_at"),
    Index("idx_evt_sub_hash", "agent_event_subscriptions", "agent_id, owner_user_id, source_hash", unique=True),
    Index("idx_evt_del_sub", "event_deliveries", "subscription_id"),
    Index("idx_evt_del_dedup", "event_deliveries", "subscription_id, event_external_id"),
    Index("idx_evt_del_created", "event_deliveries", "created_at DESC"),
    Index("idx_pages_user", "pages", "user_id"),
]


# ── FTS tables (SQLite-only; Postgres/MySQL renderer translates to tsvector/FULLTEXT) ──

FTS_TABLES: List[FtsTable] = [
    FtsTable(
        name="memories_fts",
        content_table="memories",
        indexed_columns=["title", "compiled_truth", "timeline"],
        unindexed_columns=["slug"],
    ),
    FtsTable(
        name="doc_chunks_fts",
        content_table="doc_chunks",
        indexed_columns=["chunk_text"],
        unindexed_columns=["source_ref"],
    ),
]


# ── Triggers (SQLite-only — FTS sync) ───────────────────────────────────────

TRIGGERS: List[Trigger] = [
    Trigger("trg_memories_fts_insert", """
        AFTER INSERT ON memories BEGIN
        INSERT INTO memories_fts(rowid, slug, title, compiled_truth, timeline)
        VALUES (new.rowid, new.slug, new.title, new.compiled_truth, new.timeline);
        END
    """),
    Trigger("trg_memories_fts_delete", """
        AFTER DELETE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, slug, title, compiled_truth, timeline)
        VALUES ('delete', old.rowid, old.slug, old.title, old.compiled_truth, old.timeline);
        END
    """),
    Trigger("trg_memories_fts_update", """
        AFTER UPDATE ON memories BEGIN
        INSERT INTO memories_fts(memories_fts, rowid, slug, title, compiled_truth, timeline)
        VALUES ('delete', old.rowid, old.slug, old.title, old.compiled_truth, old.timeline);
        INSERT INTO memories_fts(rowid, slug, title, compiled_truth, timeline)
        VALUES (new.rowid, new.slug, new.title, new.compiled_truth, new.timeline);
        END
    """),
    Trigger("trg_doc_chunks_fts_insert", """
        AFTER INSERT ON doc_chunks BEGIN
        INSERT INTO doc_chunks_fts(rowid, chunk_text, source_ref)
        VALUES (new.rowid, new.chunk_text, new.source_ref);
        END
    """),
    Trigger("trg_doc_chunks_fts_delete", """
        AFTER DELETE ON doc_chunks BEGIN
        INSERT INTO doc_chunks_fts(doc_chunks_fts, rowid, chunk_text, source_ref)
        VALUES ('delete', old.rowid, old.chunk_text, old.source_ref);
        END
    """),
    Trigger("trg_doc_chunks_fts_update", """
        AFTER UPDATE ON doc_chunks BEGIN
        INSERT INTO doc_chunks_fts(doc_chunks_fts, rowid, chunk_text, source_ref)
        VALUES ('delete', old.rowid, old.chunk_text, old.source_ref);
        INSERT INTO doc_chunks_fts(rowid, chunk_text, source_ref)
        VALUES (new.rowid, new.chunk_text, new.source_ref);
        END
    """),
]


# Dependency-order list for migration / bootstrap / delete operations.
# Children before parents on delete; parents before children on insert.
TABLE_ORDER = [t.name for t in TABLES]

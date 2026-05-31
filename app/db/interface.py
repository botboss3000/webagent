"""
Abstract storage backend interface for webAgent.

Defines the contract that both Supabase and Local backends must implement.
All methods match the current SupabaseClient API surface.

Also defines `EncryptedStorageBackend` — a decorator that wraps any concrete
backend and transparently encrypts/decrypts sensitive fields on the way in
and out. The app/db factory applies this wrapper when the active encryption
level is non-trivial.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from app.models.schemas import InteractionRecord

logger = logging.getLogger(__name__)


class StorageBackend(ABC):
    """Abstract base class for database backends (Supabase, SQLite, etc.)."""

    # ---- Sessions ----

    @abstractmethod
    async def assert_session_owned(self, user_id: str, session_id: str) -> None:
        """Ensure session_id belongs to user_id. Raise PermissionError if not."""
        ...

    @abstractmethod
    async def upsert_session_summary(
        self,
        user_id: str,
        session_id: str,
        summary: str,
        message_count: int,
        title: Optional[str] = None,
    ) -> None:
        """Insert or update a session summary."""
        ...

    # ---- Interactions ----

    @abstractmethod
    async def fetch_interactions(
        self, user_id: str, session_id: str
    ) -> List[InteractionRecord]:
        """Load all interactions for a session, ordered by created_at."""
        ...

    @abstractmethod
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
        session_seq: Optional[int] = None,
        turn_id: Optional[str] = None,
        turn_seq: Optional[int] = None,
        status: str = "complete",
    ) -> str:
        """Insert an interaction row. Returns the interaction id.

        ``status`` is the message lifecycle marker — 'complete' for finished
        rows, 'streaming' for an assistant answer still being generated (later
        flipped to 'complete' / 'interrupted' / 'error')."""
        ...

    async def insert_interactions_batch(self, rows: List[Dict[str, Any]]) -> List[str]:
        """Bulk-insert interaction rows in a single round-trip.

        Each dict may contain: id, session_id, parent_id, role, content,
        tool_name, tool_call_id, channel, metadata, input, output, source,
        from_id, to_id, session_seq, turn_id, turn_seq, created_at.

        Caller is responsible for session ownership. Default impl falls back
        to a loop of insert_interaction — subclasses should override for
        true batching (PostgREST array body / SQLite executemany).
        """
        ids: List[str] = []
        for r in rows:
            rid = await self.insert_interaction(
                user_id=r.get("user_id", ""),
                session_id=r["session_id"],
                role=r.get("role", "tool"),
                content=r.get("content", ""),
                parent_id=r.get("parent_id"),
                tool_name=r.get("tool_name"),
                tool_call_id=r.get("tool_call_id"),
                channel=r.get("channel"),
                metadata=r.get("metadata"),
                input_data=r.get("input"),
                output_data=r.get("output"),
                sender_id=r.get("from_id"),
                receiver_id=r.get("to_id"),
                source=r.get("source"),
                session_seq=r.get("session_seq"),
                turn_id=r.get("turn_id"),
                turn_seq=r.get("turn_seq"),
            )
            ids.append(rid)
        return ids

    async def next_session_seq(self, session_id: str, count: int = 1) -> int:
        """Return the next session_seq value to assign for this session.

        Default impl returns 1 (forces caller to rely on its own counter).
        Subclasses should query MAX(session_seq) on bootstrap or restart.
        """
        return 1

    # ---- Default-template seeding ----

    @abstractmethod
    async def copy_defaults_to_agent(
        self, agent_id: str, template_id: Optional[str] = None
    ) -> int:
        """
        Copy admin-base prompt slots from the default template into this agent.
        Returns the number of slot rows copied.
        """
        ...

    # ---- Memory System (knowledge brain) ----
    # Four tables: memories, memory_chunks, memory_links, memory_timeline

    @abstractmethod
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
        """Create or update a memory page. Returns the page dict."""
        ...

    @abstractmethod
    async def memory_get(self, user_id: str, slug: str) -> Optional[dict]:
        """Get a single memory page by slug."""
        ...

    @abstractmethod
    async def memory_delete(self, user_id: str, slug: str) -> bool:
        """Delete a memory page and its chunks/links/timeline."""
        ...

    @abstractmethod
    async def memory_list(
        self, user_id: str, page_type: Optional[str] = None
    ) -> List[dict]:
        """List all memory pages, optionally filtered by type."""
        ...

    @abstractmethod
    async def memory_search(
        self, user_id: str, query: str, limit: int = 10
    ) -> List[dict]:
        """Keyword search across memory pages using FTS."""
        ...

    @abstractmethod
    async def memory_add_link(
        self,
        user_id: str,
        from_slug: str,
        to_slug: str,
        link_type: str,
        context: Optional[str] = None,
    ) -> dict:
        """Add a typed edge to the knowledge graph."""
        ...

    @abstractmethod
    async def memory_graph_query(
        self,
        user_id: str,
        node_slug: str,
        link_type: Optional[str] = None,
        direction: str = "both",
        depth: int = 2,
    ) -> List[dict]:
        """Traverse the knowledge graph from a starting node."""
        ...

    @abstractmethod
    async def memory_add_timeline_entry(
        self,
        user_id: str,
        page_slug: str,
        event_date: str,
        source: str,
        summary: str,
        detail: Optional[str] = None,
    ) -> dict:
        """Append a new entry to a page's timeline."""
        ...

    # ---- Session Search ----

    @abstractmethod
    async def search_sessions(
        self, user_id: str, query: str, limit: int = 5
    ) -> List[dict]:
        """Search across sessions and messages. Returns enriched results."""
        ...

    # ---- Skills ----

    @abstractmethod
    async def list_skills(
        self, user_id: str, limit: int = 50
    ) -> List[dict]:
        """List all active skills for a user (with execution stats)."""
        ...

    @abstractmethod
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
        ...

    @abstractmethod
    async def skill_get_rating(
        self, skill_id: str, user_id: Optional[str] = None
    ) -> dict:
        """
        Compute composite rating for a skill.
        Returns dict with score (0-100), success_rate, efficiency, feedback_score, execution_count.
        """
        ...

    @abstractmethod
    async def skill_add_feedback(
        self,
        skill_id: str,
        user_id: str,
        feedback_type: str,
        execution_id: Optional[str] = None,
        message: Optional[str] = None,
    ) -> str:
        """Record user feedback on a skill execution. Returns the feedback id."""
        ...

    @abstractmethod
    async def skill_get_id_by_name(self, user_id: str, name: str) -> Optional[str]:
        """Look up a skill's id by name for a user."""
        ...

    # ---- Raw client access (for code that uses the Supabase query builder directly) ----

    @abstractmethod
    def get_raw_client(self) -> Any:
        """
        Return the underlying database client.
        For Supabase, this is the supabase.Client (used by ToolLoader,
        ToolExecutionTracker, admin/review, registry for direct table queries).
        For local mode, this is the aiosqlite connection or a proxy.
        """
        ...

    # ---- Agent Assignment ----

    @abstractmethod
    async def resolve_agent(
        self, user_id: str, template_id: str
    ) -> dict:
        """
        Resolve an agent for a user + template combo.

        1. Look for existing active agent row where user_id == template_id + user_id suffix
           AND template_id matches.
        2. If found as 'active', return it.
        3. If found as 'template' or 'filesystem' (virtual), create a real agent row
           from the template and return it.
        4. If not found, return a virtual dict with status='template' and all fields
           from the template, ready for the caller to materialize.

        Returns a dict with at minimum:
          id, user_id, template_id, system_prompt, max_turn_count, model, provider,
          temperature, max_tokens, metadata, status
        """
        ...

    @abstractmethod
    async def get_session_agent_id(self, session_id: str) -> Optional[str]:
        """Return the agent_id bound to this session, or None."""
        ...

    @abstractmethod
    async def add_session_participant(
        self, session_id: str, participant_id: str, role: str
    ) -> None:
        """Add a participant to a session. role is 'user' or 'agent'."""
        ...

    @abstractmethod
    async def remove_session_participant(
        self, session_id: str, participant_id: str
    ) -> None:
        """Remove a participant from a session by id."""
        ...

    @abstractmethod
    async def is_session_participant(
        self, session_id: str, participant_id: str, role: Optional[str] = None
    ) -> bool:
        """Check if participant_id is in a session. If role specified, also checks role matches."""
        ...

    @abstractmethod
    async def get_session_participants(
        self, session_id: str
    ) -> List[dict]:
        """Return the full participants array for a session."""
        ...

    @abstractmethod
    async def bind_session_to_agent(self, session_id: str, agent_id: str) -> None:
        """Bind a session to an agent (insert or update the binding row)."""
        ...

    @abstractmethod
    async def get_agent_for_user(self, user_id: str) -> Optional[dict]:
        """Get the assigned agent for a user. Returns None if not assigned yet."""
        ...

    @abstractmethod
    async def get_agent_by_id(self, agent_id: str) -> Optional[dict]:
        """Load one agent row by primary key ``id`` (includes ``system_prompt``)."""
        ...

    @abstractmethod
    async def fetch_agent_with_context(
        self,
        user_id: str,
        context_types: Optional[List[str]] = None,
    ) -> Optional[dict]:
        """
        Fetch agent + all context documents in one query.
        Returns agent dict with additional key ``context_documents`` (list of dicts).
        If no agent exists for user, returns None.
        If ``context_types`` is None or empty, loads all types.
        Caller should fall back to seeding + re-fetch if ``context_documents`` is empty.
        """
        ...

    @abstractmethod
    async def fetch_agent_by_id_with_context(
        self,
        agent_id: str,
        context_types: Optional[List[str]] = None,
        user_id: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Same as ``fetch_agent_with_context`` but queries by agent ``id`` (PK) instead of ``user_id``.
        ``user_id`` is used to resolve per-caller prompt slot overrides; when omitted, returns admin-base content.
        Returns None if agent_id not found.
        """
        ...

    @abstractmethod
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

        Returns None only if resolution fails entirely (should raise instead).
        """
        ...

    @abstractmethod
    async def create_agent_for_user(self, user_id: str) -> dict:
        """Create a new agent for a user by cloning the default template. Returns the new agent row."""
        ...

    @abstractmethod
    async def increment_agent_turn_count(self, agent_id: str) -> int:
        """Increment the turn_count for an agent. Returns the new turn count."""
        ...

    @abstractmethod
    async def get_default_template(self) -> dict:
        """Get the default agent template (used as blueprint for new agents)."""
        ...

    @abstractmethod
    async def get_max_turn_count(self, agent_id: str = "default_agent") -> int:
        """Get the max_turn_count for a given agent_id. DEPRECATED — use get_agent_for_user instead."""
        ...

    @abstractmethod
    async def seed_agent_templates(self, force: bool = False) -> dict:
        """
        Re-seed agent_templates + agent_prompt_templates from app/context/agents/*.json.

        Behavior:
          - Computes a manifest hash over the JSON files.
          - If hash matches app_meta['last_agent_manifest_hash'] AND force is False,
            returns immediately with cached=True (no DB writes).
          - Otherwise per-template per-slot upserts respecting the source guard:
            existing rows with source='admin' are skipped unless force=True.

        Returns a summary dict:
            {
              "changed": int,         # slot rows inserted/updated
              "skipped_admin": int,   # admin-sourced rows left alone
              "templates": int,       # config rows upserted
              "cached": bool,         # whether the manifest short-circuit fired
              "manifest_hash": str,
            }
        """
        ...

    # ---- Attachments ----

    @abstractmethod
    async def insert_attachment(
        self,
        user_id: str,
        session_id: str,
        original_name: str,
        mime_type: str,
        size_bytes: int,
        storage_path: str,
        metadata: Optional[dict] = None,
        storage_provider: str = "local",
    ) -> str:
        """Insert an attachment record. Returns the attachment id."""
        ...

    @abstractmethod
    async def get_attachment(self, attachment_id: str) -> Optional[dict]:
        """Get a single attachment by id."""
        ...

    @abstractmethod
    async def get_session_attachments(self, session_id: str) -> List[dict]:
        """Get all attachments for a session."""
        ...

    @abstractmethod
    async def delete_attachment(self, attachment_id: str) -> bool:
        """Delete an attachment record by id."""
        ...

    @abstractmethod
    async def update_attachment_metadata(self, attachment_id: str, patch: dict) -> bool:
        """Merge ``patch`` into an attachment's metadata JSON. Returns True if updated."""
        ...

    @abstractmethod
    async def update_interaction_content(self, interaction_id: str, content: str) -> bool:
        """Replace an interaction row's ``content``. Returns True if updated."""
        ...

    # ---- Streaming-answer persistence + run state ----
    # These are NON-abstract with safe defaults so backends that haven't
    # implemented durable run tracking (e.g. Supabase) keep working — they
    # simply fall back to the in-memory RunBuffer for live replay, exactly as
    # before. LocalBackend overrides all of them with real implementations.

    async def update_interaction(
        self,
        interaction_id: str,
        *,
        content: Optional[str] = None,
        status: Optional[str] = None,
        output_data: Optional[str] = None,
        metadata: Optional[str] = None,
    ) -> bool:
        """Patch selected interaction fields. Default: fall back to content-only."""
        if content is not None:
            return await self.update_interaction_content(interaction_id, content)
        return False

    async def run_state_begin(
        self, session_id: str, user_id: str, agent_id: Optional[str], turn_id: Optional[str],
        origin: Optional[str] = None, relaunch_ctx: Optional[str] = None,
        max_resume_attempts: Optional[int] = None,
    ) -> None:
        """Mark a session as having a fresh run in progress. Default: no-op."""
        return None

    async def run_state_set_assistant(self, session_id: str, assistant_interaction_id: str) -> None:
        """Record the in-progress assistant row id. Default: no-op."""
        return None

    async def run_state_update_seq(self, session_id: str, latest_session_seq: int) -> None:
        """Advance the highest emitted session_seq. Default: no-op."""
        return None

    async def run_state_finish(
        self, session_id: str, status: str = "complete", error: Optional[str] = None,
        stop_cause: Optional[str] = None,
    ) -> None:
        """Mark the run finished. Default: no-op."""
        return None

    async def run_state_get(self, session_id: str):
        """Return the run-state row for a session, or None. Default: None."""
        return None

    async def run_state_list_active(self, user_id: str):
        """Sessions for a user with a running turn. Default: empty list."""
        return []

    async def cleanup_orphaned_runs(self) -> int:
        """Reset runs left mid-flight by a prior process. Default: 0."""
        return 0

    # ── Self-healing / auto-resume (default no-ops; implemented by LocalBackend) ──

    async def run_state_set_cause(self, session_id: str, stop_cause: str) -> None:
        """Tag why a live run is stopping (user_stop / replaced). Default: no-op."""
        return None

    async def run_state_session_tool_names(self, session_id: str):
        """Distinct tool names used in a session (resume opt-out). Default: []."""
        return []

    async def run_state_heartbeat(
        self, session_id: str, owner_token: Optional[str] = None, lease_seconds: float = 120.0
    ) -> None:
        """Liveness ping + lease refresh from the running loop. Default: no-op."""
        return None

    # ── Diagnostics flight-recorder (default no-ops → RAM-only on this backend) ──

    async def insert_diagnostics_batch(self, rows):
        """Persist diagnostic records. Default: no-op (recorder keeps RAM ring)."""
        return 0

    async def query_diagnostics(self, **kwargs):
        """Query durable diagnostic rows. Default: empty (RAM ring still served)."""
        return []

    async def prune_diagnostics(self, **kwargs):
        """Trim the durable diagnostics table. Default: no-op."""
        return 0

    async def run_state_list_active_all(self):
        """Every session with a 'running' turn (all users). Default: empty list."""
        return []

    async def run_state_list_resumable(self, now_iso: Optional[str] = None):
        """Runs eligible for auto-resume. Default: empty list."""
        return []

    async def run_state_claim_for_resume(
        self, session_id: str, owner_token: str, lease_seconds: float,
        backoff_seconds: float, effective_max: int,
    ) -> bool:
        """Atomically claim a stopped run for resume. Default: False (no claim)."""
        return False

    async def run_state_mark_failed(self, session_id: str, error: Optional[str] = None) -> None:
        """Terminal: retry budget exhausted. Default: no-op."""
        return None

    async def run_state_mark_manual(self, session_id: str, error: Optional[str] = None) -> None:
        """Mark a run as awaiting a human one-click resume. Default: no-op."""
        return None

    async def mark_orphans_for_resume(self) -> int:
        """Boot recovery that leaves orphans resumable. Default: 0."""
        return 0

    # ---- Interrupt Handling ----

    @abstractmethod
    async def set_interrupt(self, session_id: str) -> None:
        """Set the interrupt flag for a session."""
        ...

    @abstractmethod
    async def clear_interrupt(self, session_id: str) -> None:
        """Clear the interrupt flag for a session."""
        ...

    @abstractmethod
    async def check_interrupt(self, session_id: str) -> bool:
        """Check if an interrupt was requested for a session."""
        ...

    # ---- Webhooks (generic inbound) ----

    @abstractmethod
    async def register_webhook(
        self,
        user_id: str,
        name: str,
        instructions: str = "",
    ) -> dict:
        """
        Create a generic inbound webhook registration.
        Returns the registration dict with:
          id, name, instructions, url, active, created_at
        """
        ...

    @abstractmethod
    async def get_webhook(self, webhook_id: str) -> Optional[dict]:
        """Get a webhook registration by id."""
        ...

    @abstractmethod
    async def list_webhooks(self, user_id: str) -> List[dict]:
        """List all webhook registrations for a user."""
        ...

    @abstractmethod
    async def delete_webhook(self, webhook_id: str, user_id: str) -> bool:
        """Delete a webhook registration by id (scoped to user_id)."""
        ...

    @abstractmethod
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
        ...

    @abstractmethod
    async def get_webhook_logs(
        self, webhook_id: str, limit: int = 20
    ) -> List[dict]:
        """Get recent webhook events for a registration."""
        ...

    # ---- Auth Elements (per-user service credentials) ----

    @abstractmethod
    async def auth_element_get(
        self, user_id: str, service: str, label: str = "default"
    ) -> Optional[dict]:
        """Get one auth element by user + service + label.
        Returns dict with keys: id, user_id, service, label, config (JSON str),
        secret_ref, is_active, created_at, updated_at.
        Returns None if not found."""
        ...

    @abstractmethod
    async def auth_element_set(
        self,
        user_id: str,
        service: str,
        config: dict,
        secret_ref: str = "",
        label: str = "default",
    ) -> dict:
        """Upsert an auth element for a user+service+label.
        config is a dict of non-sensitive settings (provider, model, base_url, etc.)
        secret_ref is the secret value (API key, token, etc.) — will move to vault later.
        Returns the saved row dict."""
        ...

    @abstractmethod
    async def auth_element_list(
        self, user_id: str, service: Optional[str] = None
    ) -> List[dict]:
        """List auth elements for a user, optionally filtered by service."""
        ...

    @abstractmethod
    async def auth_element_delete(
        self, user_id: str, service: str, label: str = "default"
    ) -> bool:
        """Delete an auth element. Returns True if deleted."""
        ...


    # ---- Provider Ratings ----

    @abstractmethod
    async def get_provider_ratings(self, user_id: str) -> dict:
        """Get all provider ratings for a user. Returns dict: {(provider, model): rating}"""
        pass

    @abstractmethod
    async def update_provider_rating(self, user_id: str, provider: str, model: str, delta: int) -> int:
        """Increment/decrement a provider rating. Returns the new rating."""
        pass


    # ---- User Profiles ----

    @abstractmethod
    async def get_user_profile(self, user_id: str) -> Optional[dict]:
        """Return the user_profiles row for user_id, or None if not found."""
        ...

    @abstractmethod
    async def is_user_admin(self, user_id: str) -> bool:
        """Return True if the user has is_admin=True in user_profiles."""
        ...

    # ---- Multi-Agent Management ----

    @abstractmethod
    async def list_agent_templates(self, include_admin: bool = False) -> List[dict]:
        """
        Return agent_templates that are user-visible (is_pipeline=0).
        If include_admin=False, excludes access_level='admin_only' templates.
        """
        ...

    @abstractmethod
    async def list_agents_for_user(self, user_id: str, include_admin: bool = False) -> List[dict]:
        """
        Return all agents visible to a user: system templates + user's custom agents.
        Each item includes a 'source' key: 'template' or 'custom'.
        """
        ...

    @abstractmethod
    async def create_custom_agent(self, user_id: str, name: str, description: str = "") -> dict:
        """
        Create a new custom agent for a user, cloned from the default template.
        Returns the new agents row as a dict (with source='custom').
        """
        ...

    @abstractmethod
    async def save_agent_as_template(
        self,
        agent_id: str,
        template_id: str,
        name: str,
        description: str = "",
        icon: str = "",
        discoverable: bool = False,
        access_level: str = "all",
        updated_by: str = "admin",
    ) -> dict:
        """
        Snapshot a custom agent's config + admin-base prompt slots into a new
        agent_templates row plus matching agent_prompt_templates rows
        (source='admin' so JSON re-seed won't clobber them).
        Raises ValueError if the agent is missing or template_id already exists.
        """
        ...

    @abstractmethod
    async def delete_custom_agent(self, agent_id: str, user_id: str) -> bool:
        """
        Delete a custom agent. Caller must be in admin_users.
        Returns True if a row was deleted, False if not found or not owned.
        """
        ...

    @abstractmethod
    async def get_user_default_agent_id(self, user_id: str) -> Optional[str]:
        """Return the user's preferred default_agent_id, or None if not set."""
        ...

    @abstractmethod
    async def set_user_default_agent(self, user_id: str, agent_id: str) -> None:
        """Set the user's preferred default agent in user_profiles."""
        ...

    @abstractmethod
    async def update_agent_fields(
        self,
        agent_id: str,
        user_id: str,
        updates: dict,
    ) -> Optional[dict]:
        """
        Update editable fields on a custom agent. Caller must be in admin_users.
        Returns the updated agent row dict, or None if not found/not owned.
        """
        ...

    @abstractmethod
    async def get_agent_roles(self, agent_id: str) -> dict:
        """Return {'admin_users': [...], 'member_users': [...]} for an agent."""
        ...

    @abstractmethod
    async def add_agent_member(self, agent_id: str, user_id: str) -> bool:
        """
        Add user_id to the agent's member_users list if not already present.
        Returns True if newly added, False if already a member or admin.
        """
        ...

    @abstractmethod
    async def is_agent_member(self, agent_id: str, user_id: str) -> bool:
        """Return True if user_id is a member or admin of the agent."""
        ...

    # ---- AutoAgent pages (page-builder workspace) ----

    @abstractmethod
    async def pages_list(self, user_id: str) -> List[dict]:
        """Return all page rows for user_id, ordered with 'home' first then by updated_at desc.
        Each row: id, user_id, slug, title, agent_context, html, created_at, updated_at.
        `html` may be None when the body lives on disk (hybrid mode)."""
        ...

    @abstractmethod
    async def pages_get(self, user_id: str, slug: str) -> Optional[dict]:
        """Get one page row by (user_id, slug). Returns None if not found."""
        ...

    @abstractmethod
    async def pages_upsert(
        self,
        user_id: str,
        slug: str,
        title: str,
        agent_context: str = "",
        html: Optional[str] = None,
    ) -> dict:
        """Insert or update a page. Returns the saved row. `html=None` leaves the
        column NULL on insert and untouched on update (so hybrid mode can store
        metadata-only rows without disturbing existing bodies)."""
        ...

    @abstractmethod
    async def pages_delete(self, user_id: str, slug: str) -> bool:
        """Delete one page. Returns True if a row was removed."""
        ...

    # ---- Per-Agent External Data Sources ----

    @abstractmethod
    async def data_source_create(
        self,
        user_id: str,
        name: str,
        type: str,
        config: Optional[dict] = None,
        auth_element_id: Optional[str] = None,
        safety_policy: Optional[dict] = None,
    ) -> dict:
        """Create a new external data source. Returns the inserted row."""
        ...

    @abstractmethod
    async def data_source_update(self, ds_id: str, user_id: str, **fields) -> Optional[dict]:
        """Update fields on a data source (owner-scoped). Returns updated row or None."""
        ...

    @abstractmethod
    async def data_source_get(self, ds_id: str, user_id: Optional[str] = None) -> Optional[dict]:
        """Fetch a data source by id; if user_id given, scope by ownership."""
        ...

    @abstractmethod
    async def data_source_list(self, user_id: str) -> List[dict]:
        """List all data sources owned by user_id."""
        ...

    @abstractmethod
    async def data_source_delete(self, ds_id: str, user_id: str) -> bool:
        """Delete a data source (owner-scoped). Cascades to agent_data_sources."""
        ...

    @abstractmethod
    async def agent_data_source_list(self, agent_id: str, enabled_only: bool = False) -> List[dict]:
        """Return attachments for an agent, joined with the source row."""
        ...

    @abstractmethod
    async def agent_data_source_attach(
        self,
        agent_id: str,
        data_source_id: str,
        tool_alias: Optional[str] = None,
        inject_schema_in_prompt: bool = True,
    ) -> dict:
        """Attach a data source to an agent. Idempotent on (agent_id, data_source_id)."""
        ...

    @abstractmethod
    async def agent_data_source_update(
        self, agent_id: str, data_source_id: str, **fields
    ) -> Optional[dict]:
        """Update an attachment's settings (tool_alias, enabled, inject_schema_in_prompt)."""
        ...

    @abstractmethod
    async def agent_data_source_detach(self, agent_id: str, data_source_id: str) -> bool:
        """Detach a data source from an agent."""
        ...

    @abstractmethod
    async def doc_chunk_upsert(
        self,
        data_source_id: str,
        source_ref: str,
        chunk_index: int,
        chunk_text: str,
        content_hash: Optional[str] = None,
        embedding: Optional[List[float]] = None,
        metadata: Optional[dict] = None,
    ) -> str:
        """Insert or replace a doc chunk. Returns the chunk id."""
        ...

    @abstractmethod
    async def doc_chunk_delete_by_source_ref(self, data_source_id: str, source_ref: str) -> int:
        """Delete all chunks tied to a (data_source_id, source_ref). Returns count deleted."""
        ...

    @abstractmethod
    async def doc_chunk_count(self, data_source_id: str) -> int:
        """Return the total chunk count for a data source."""
        ...

    @abstractmethod
    async def doc_chunk_search(
        self, data_source_id: str, query: str, limit: int = 5
    ) -> List[dict]:
        """Hybrid keyword + vector search within a single data source. Returns ranked chunks."""
        ...


# ──────────────────────────────────────────────────────────────────────────
# EncryptedStorageBackend
# ──────────────────────────────────────────────────────────────────────────


# Sentinel identifiers used by InlineDBSecrets (app/secrets/inline_db_secrets.py).
# Rows with this (user_id, service) pair store wrapped DEKs and MUST bypass the
# encryption shim — otherwise we'd try to fetch a DEK to encrypt the DEK itself.
_VAULT_SENTINEL_USER = "_vault"
_VAULT_SENTINEL_SERVICE = "_secrets_vault"


class EncryptedStorageBackend:
    """
    Decorator over any StorageBackend that transparently encrypts/decrypts
    sensitive fields (currently `auth_elements.secret_ref`) using an
    EncryptionBackend.

    Intentionally does NOT inherit from StorageBackend (the ABC would
    require us to override every abstract method). It duck-types: callers
    typed as ``StorageBackend`` are unaffected because every method that
    isn't overridden here is delegated to the inner backend via
    ``__getattr__``.

    Rows whose (user_id, service) match the inline-DB vault sentinel are
    passed through untouched. The vault itself must never be encrypted by
    the shim that depends on it.
    """

    def __init__(self, inner: StorageBackend, enc) -> None:
        # `enc` is an app.encryption.interface.EncryptionBackend instance.
        # Imported in the factory, not here, to avoid a circular import.
        self._inner = inner
        self._enc = enc

    # ── Sensitive field interceptors ──────────────────────────────────────

    @staticmethod
    def _is_vault_call(user_id: str, service: str) -> bool:
        return user_id == _VAULT_SENTINEL_USER and service == _VAULT_SENTINEL_SERVICE

    async def auth_element_get(
        self, user_id: str, service: str, label: str = "default"
    ) -> Optional[dict]:
        row = await self._inner.auth_element_get(user_id, service, label)
        if not row:
            return row
        if self._is_vault_call(user_id, service):
            return row
        ref = row.get("secret_ref")
        if ref:
            try:
                row["secret_ref"] = await self._enc.decrypt(user_id, ref)
            except Exception as e:
                logger.warning(
                    "decrypt failed for user=%s service=%s label=%s: %s",
                    user_id, service, label, e,
                )
        return row

    async def auth_element_set(
        self,
        user_id: str,
        service: str,
        config: dict,
        secret_ref: str = "",
        label: str = "default",
    ) -> dict:
        wrapped_secret = secret_ref
        if secret_ref and not self._is_vault_call(user_id, service):
            try:
                wrapped_secret = await self._enc.encrypt(user_id, secret_ref)
            except Exception as e:
                logger.error(
                    "encrypt failed for user=%s service=%s label=%s: %s — storing plaintext",
                    user_id, service, label, e,
                )
        return await self._inner.auth_element_set(
            user_id, service, config, wrapped_secret, label,
        )

    async def auth_element_list(
        self, user_id: str, service: Optional[str] = None
    ) -> List[dict]:
        rows = await self._inner.auth_element_list(user_id, service)
        out: List[dict] = []
        for row in rows or []:
            r = dict(row)
            if not self._is_vault_call(r.get("user_id", ""), r.get("service", "")):
                ref = r.get("secret_ref")
                if ref:
                    try:
                        r["secret_ref"] = await self._enc.decrypt(r.get("user_id", ""), ref)
                    except Exception as e:
                        logger.warning("decrypt failed in list: %s", e)
            out.append(r)
        return out

    # ── Pass-through ──────────────────────────────────────────────────────

    def __getattr__(self, name):
        # Only invoked for attrs not found on self (so the overridden methods
        # above shadow this). Delegates everything else to the inner backend.
        return getattr(self._inner, name)

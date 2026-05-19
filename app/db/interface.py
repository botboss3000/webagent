"""
Abstract storage backend interface for webAgent.

Defines the contract that both Supabase and Local backends must implement.
All methods match the current SupabaseClient API surface.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from app.models.schemas import InteractionRecord


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
    ) -> str:
        """Insert an interaction row. Returns the interaction id."""
        ...

    # ---- Context Defaults ----

    @abstractmethod
    async def fetch_context_defaults(
        self, context_types: List[str]
    ) -> List[dict]:
        """Load default context rows where context_type is in the list."""
        ...

    @abstractmethod
    async def copy_defaults_to_agent(
        self, agent_id: str, template_id: Optional[str] = None
    ) -> int:
        """
        Copy template rows into context storage for this agent.
        Only copies types not already present for this agent.
        Returns the number of rows copied.
        """
        ...

    # ---- Context Documents ----

    @abstractmethod
    async def fetch_context_documents(
        self, agent_id: str, context_types: Optional[List[str]] = None,
    ) -> List[dict]:
        """Load context rows for this agent; if context_types is None or empty, load all types."""
        ...

    @abstractmethod
    async def get_context_document(
        self, agent_id: str, context_id: str,
    ) -> Optional[dict]:
        """Return one context row by id if it belongs to this agent."""
        ...

    @abstractmethod
    async def update_context_document_content(
        self, agent_id: str, context_id: str, content: str,
    ) -> None:
        """Update the ``content`` column for a row owned by this agent."""
        ...

    @abstractmethod
    async def insert_document(
        self,
        agent_id: str,
        context_type: str,
        title: str,
        content: str,
        tags: Optional[List[str]] = None,
    ) -> str:
        """Insert a context document. Returns the id."""
        ...

    @abstractmethod
    async def delete_context_row(self, agent_id: str, context_id: str) -> None:
        """Delete a context row by id (scoped to agent)."""
        ...

    @abstractmethod
    async def delete_all_documents_for_agent(self, agent_id: str) -> int:
        """Delete all context rows for an agent. Returns count of deleted rows."""
        ...

    @abstractmethod
    async def fetch_context_documents_for_agent(
        self,
        agent_id: str,
        context_types: Optional[List[str]] = None,
    ) -> List[dict]:
        """
        Load context documents for the user assigned to this agent.
        If ``context_types`` is None or empty, load all types for that user.
        """
        ...

    @abstractmethod
    async def get_context_document_for_agent(
        self, agent_id: str, context_id: str
    ) -> Optional[dict]:
        """Return one context row by id if it belongs to this agent's user."""
        ...

    @abstractmethod
    async def update_context_document_content_for_agent(
        self, agent_id: str, context_id: str, content: str
    ) -> None:
        """Update the ``content`` column for a row owned by this agent's user."""
        ...

    @abstractmethod
    async def insert_context_document_for_agent(
        self,
        agent_id: str,
        context_type: str,
        title: str,
        content: str,
        tags: Optional[List[str]] = None,
    ) -> str:
        """Insert a context document for this agent's user. Returns the new id."""
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
    ) -> Optional[dict]:
        """
        Same as ``fetch_agent_with_context`` but queries by agent ``id`` (PK) instead of ``user_id``.
        Direct FK lookup — no naming convention, no inference chain, no fallback.
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
    async def seed_agent_templates(self) -> int:
        """
        Re-seed agent_templates from app/context/agents/*.json.
        Upserts all template rows so JSON changes take effect.
        Returns the number of templates seeded.
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
    async def delete_custom_agent(self, agent_id: str, owner_user_id: str) -> bool:
        """
        Delete a custom agent owned by owner_user_id.
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
        owner_user_id: str,
        updates: dict,
    ) -> Optional[dict]:
        """
        Update editable fields on a custom agent owned by owner_user_id.
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

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
        sender_id: Optional[str] = None,
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
        self, agent_id: str
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
    async def get_agent_for_user(self, user_id: str) -> Optional[dict]:
        """Get the assigned agent for a user. Returns None if not assigned yet."""
        ...

    @abstractmethod
    async def get_agent_by_id(self, agent_id: str) -> Optional[dict]:
        """Load one agent row by primary key ``id`` (includes ``system_prompt``)."""
        ...

    @abstractmethod
    async def create_agent_for_user(self, user_id: str) -> dict:
        """Create a new agent for a user by cloning the default template. Returns the new agent row."""
        ...

    @abstractmethod
    async def get_default_template(self) -> dict:
        """Get the default agent template (used as blueprint for new agents)."""
        ...

    @abstractmethod
    async def get_max_turn_count(self, agent_id: str = "default_agent") -> int:
        """Get the max_turn_count for a given agent_id. DEPRECATED — use get_agent_for_user instead."""
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


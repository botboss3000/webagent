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
        title: str | None = None,
    ) -> None:
        """Insert or update a session summary."""
        ...

    # ---- Interactions ----

    @abstractmethod
    async def fetch_interactions(
        self, user_id: str, session_id: str
    ) -> list[InteractionRecord]:
        """Load all interactions for a session, ordered by created_at."""
        ...

    @abstractmethod
    async def insert_interaction(
        self,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
        parent_id: str | None = None,
        tool_name: str | None = None,
        tool_call_id: str | None = None,
        metadata: str | None = None,
        input_data: str | None = None,
    ) -> str:
        """Insert an interaction row. Returns the interaction id."""
        ...

    # ---- Context Defaults ----

    @abstractmethod
    async def fetch_context_defaults(
        self, context_types: list[str]
    ) -> list[dict]:
        """Load default context rows where context_type is in the list."""
        ...

    @abstractmethod
    async def copy_defaults_to_user(
        self, user_id: str
    ) -> int:
        """
        Copy all context_default rows into context_documents for a user.
        Only copies rows that don't already exist for that user (by context_type).
        Returns the number of rows copied.
        """
        ...

    # ---- Context Documents ----

    @abstractmethod
    async def fetch_context_documents(
        self, user_id: str, context_types: list[str]
    ) -> list[dict]:
        """Load context rows where context_type is in the list."""
        ...

    @abstractmethod
    async def insert_document(
        self,
        user_id: str,
        context_type: str,
        title: str,
        content: str,
        tags: Optional[list[str]] = None,
    ) -> str:
        """Insert a context document. Returns the id."""
        ...

    @abstractmethod
    async def update_context_row(self, context_id: str, content: str) -> None:
        """Update a context row's content."""
        ...

    @abstractmethod
    async def delete_context_row(self, context_id: str) -> None:
        """Delete a context row by id."""
        ...

    @abstractmethod
    async def delete_all_documents_for_user(self, user_id: str) -> int:
        """Delete all context rows for a user. Returns count of deleted rows."""
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
        frontmatter: dict | None = None,
    ) -> dict:
        """Create or update a memory page. Returns the page dict."""
        ...

    @abstractmethod
    async def memory_get(self, user_id: str, slug: str) -> dict | None:
        """Get a single memory page by slug."""
        ...

    @abstractmethod
    async def memory_delete(self, user_id: str, slug: str) -> bool:
        """Delete a memory page and its chunks/links/timeline."""
        ...

    @abstractmethod
    async def memory_list(
        self, user_id: str, page_type: str | None = None
    ) -> list[dict]:
        """List all memory pages, optionally filtered by type."""
        ...

    @abstractmethod
    async def memory_search(
        self, user_id: str, query: str, limit: int = 10
    ) -> list[dict]:
        """Keyword search across memory pages using FTS."""
        ...

    @abstractmethod
    async def memory_add_link(
        self,
        user_id: str,
        from_slug: str,
        to_slug: str,
        link_type: str,
        context: str | None = None,
    ) -> dict:
        """Add a typed edge to the knowledge graph."""
        ...

    @abstractmethod
    async def memory_graph_query(
        self,
        user_id: str,
        node_slug: str,
        link_type: str | None = None,
        direction: str = "both",
        depth: int = 2,
    ) -> list[dict]:
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
        detail: str | None = None,
    ) -> dict:
        """Append a new entry to a page's timeline."""
        ...

    # ---- Session Search ----

    @abstractmethod
    async def search_sessions(
        self, user_id: str, query: str, limit: int = 5
    ) -> list[dict]:
        """Search across sessions and messages. Returns enriched results."""
        ...

    # ---- Skills ----

    @abstractmethod
    async def list_skills(
        self, user_id: str, limit: int = 50
    ) -> list[dict]:
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
        interaction_id: str | None = None,
        error_message: str | None = None,
        input_params: dict | None = None,
        output_summary: str | None = None,
        steps_to_complete: int = 1,
    ) -> str:
        """Record a skill execution. Returns the execution id."""
        ...

    @abstractmethod
    async def skill_get_rating(
        self, skill_id: str, user_id: str | None = None
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
        execution_id: str | None = None,
        message: str | None = None,
    ) -> str:
        """Record user feedback on a skill execution. Returns the feedback id."""
        ...

    @abstractmethod
    async def skill_get_id_by_name(self, user_id: str, name: str) -> str | None:
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
    async def get_agent_for_user(self, user_id: str) -> dict | None:
        """Get the assigned agent for a user. Returns None if not assigned yet."""
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

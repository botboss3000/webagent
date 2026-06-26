"""
Abstract page-store backend.

A CanvasStore owns the persistence of canvases — the per-user HTML
documents rendered into the Canvas tab iframe and edited via the page-builder.

Implementations:
  - FilesystemCanvasStore   one folder per canvas on disk at
                          data/user_data/{uid}/canvas/{slug}/index.html, with an
                          optional per-folder page.json descriptor (title,
                          agent_context, order, …). No central manifest — a
                          folder with an index.html IS a canvas. (default)
  - DatabaseCanvasStore     full HTML + metadata in the `pages` table
  - HybridCanvasStore       metadata in the `pages` table, body on disk
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class CanvasStore(ABC):
    """Async per-user store for canvases."""

    name: str = "unknown"

    @abstractmethod
    async def list_canvases(self, user_id: str) -> List[Dict]:
        """Return catalog entries for a user, sorted for display. Each entry has
        keys: slug, title, description, agent_context, order, created_at,
        updated_at, url. (The home canvas is seeded by the facade, not here.)"""
        ...

    @abstractmethod
    async def get_canvas_html(self, user_id: str, slug: str) -> Optional[str]:
        """Return the HTML body for a page, or None if not found."""
        ...

    @abstractmethod
    async def save_canvas_html(self, user_id: str, slug: str, html: str, agent_id: str = "") -> str:
        """Write HTML for a page and bump its updated_at. Returns the page URL.
        `agent_id` records the agent that rendered it (the canvas's owner); a
        supplied value wins, otherwise the existing owner is preserved."""
        ...

    @abstractmethod
    async def create_canvas(
        self,
        user_id: str,
        slug: str,
        title: str,
        agent_context: str = "",
        initial_html: str = "",
        agent_id: str = "",
    ) -> Dict:
        """Create a new page. Raises ValueError if slug already exists.
        Returns the manifest entry including its URL. `agent_id` is the creating
        agent (the canvas's owner)."""
        ...

    @abstractmethod
    async def delete_canvas(self, user_id: str, slug: str) -> bool:
        """Delete a page. The 'home' page cannot be deleted; returns False."""
        ...

    @abstractmethod
    async def rename_canvas(self, user_id: str, slug: str, new_title: str) -> bool:
        """Update only the display title of a page. Slug and body are preserved.
        Returns False if no page with that slug exists."""
        ...

    @abstractmethod
    async def ensure_home_canvas(self, user_id: str, seed_html: str) -> None:
        """Seed the home page for user_id if it doesn't already exist."""
        ...

    @abstractmethod
    async def get_canvas_data(self, user_id: str, slug: str) -> Optional[Dict]:
        """Return a canvas's DATA object (the content it renders), or None when it
        has no data file. Kept separate from the HTML body so the agent can update
        a canvas's content without rewriting its page markup."""
        ...

    @abstractmethod
    async def save_canvas_data(self, user_id: str, slug: str, data: Dict) -> None:
        """Write (replace) a canvas's DATA object. Bumps updated_at where the
        backend tracks it."""
        ...
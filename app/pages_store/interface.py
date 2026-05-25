"""
Abstract page-store backend.

A PageStore owns the persistence of AutoAgent pages — the per-user HTML
documents rendered into the Dashboard iframe and edited via the page-builder.

Implementations:
  - FilesystemPageStore   files on disk at visuals/users/{uid}/{slug}.html
                          plus a pages.json manifest (default)
  - DatabasePageStore     full HTML + metadata in the `pages` table
  - HybridPageStore       metadata in the `pages` table, body on disk
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class PageStore(ABC):
    """Async per-user store for AutoAgent pages."""

    name: str = "unknown"

    @abstractmethod
    async def list_pages(self, user_id: str) -> List[Dict]:
        """Return manifest entries for a user. Seeds the home page if missing.
        Each entry has keys: slug, title, agent_context, created_at, updated_at, url."""
        ...

    @abstractmethod
    async def get_page_html(self, user_id: str, slug: str) -> Optional[str]:
        """Return the HTML body for a page, or None if not found."""
        ...

    @abstractmethod
    async def save_page_html(self, user_id: str, slug: str, html: str) -> str:
        """Write HTML for a page and bump its updated_at. Returns the page URL."""
        ...

    @abstractmethod
    async def create_page(
        self,
        user_id: str,
        slug: str,
        title: str,
        agent_context: str = "",
        initial_html: str = "",
    ) -> Dict:
        """Create a new page. Raises ValueError if slug already exists.
        Returns the manifest entry including its URL."""
        ...

    @abstractmethod
    async def delete_page(self, user_id: str, slug: str) -> bool:
        """Delete a page. The 'home' page cannot be deleted; returns False."""
        ...

    @abstractmethod
    async def ensure_home_page(self, user_id: str, seed_html: str) -> None:
        """Seed the home page for user_id if it doesn't already exist."""
        ...

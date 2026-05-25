"""Database-backed PageStore.

Stores page metadata + HTML body in the `pages` table via the active
StorageBackend (SQLite or Supabase). Survives container restarts, gets
backed up with the DB, and stays consistent with the rest of the app's
storage architecture.
"""

from typing import Dict, List, Optional

from app.db import get_db
from app.pages_store.interface import PageStore
from app.pages_store.common import (
    default_agent_context,
    home_agent_context,
    page_url,
    safe,
)


def _entry(row: Dict, user_id: str) -> Dict:
    """Project a DB row into the manifest-entry shape the UI expects."""
    return {
        "slug": row["slug"],
        "title": row["title"],
        "agent_context": row.get("agent_context") or "",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "url": page_url(user_id, row["slug"]),
    }


class DatabasePageStore(PageStore):
    name = "database"

    async def list_pages(self, user_id: str) -> List[Dict]:
        rows = await get_db().pages_list(user_id)
        return [_entry(r, user_id) for r in rows]

    async def get_page_html(self, user_id: str, slug: str) -> Optional[str]:
        row = await get_db().pages_get(user_id, slug)
        return row.get("html") if row else None

    async def save_page_html(self, user_id: str, slug: str, html: str) -> str:
        existing = await get_db().pages_get(user_id, slug)
        title = (existing or {}).get("title") or slug.capitalize()
        ctx = (existing or {}).get("agent_context") or ""
        await get_db().pages_upsert(
            user_id=user_id,
            slug=slug,
            title=title,
            agent_context=ctx,
            html=html,
        )
        return page_url(user_id, slug)

    async def create_page(
        self,
        user_id: str,
        slug: str,
        title: str,
        agent_context: str = "",
        initial_html: str = "",
    ) -> Dict:
        from app.pages_store.common import blank_page_html
        safe_slug = safe(slug)
        if await get_db().pages_get(user_id, safe_slug):
            raise ValueError(f"Page '{safe_slug}' already exists")
        row = await get_db().pages_upsert(
            user_id=user_id,
            slug=safe_slug,
            title=title,
            agent_context=agent_context or default_agent_context(title),
            html=initial_html or blank_page_html(title),
        )
        return _entry(row, user_id)

    async def delete_page(self, user_id: str, slug: str) -> bool:
        if safe(slug) == "home":
            return False
        return await get_db().pages_delete(user_id, safe(slug))

    async def ensure_home_page(self, user_id: str, seed_html: str) -> None:
        existing = await get_db().pages_get(user_id, "home")
        if existing:
            return
        await get_db().pages_upsert(
            user_id=user_id,
            slug="home",
            title="Home",
            agent_context=home_agent_context(),
            html=seed_html,
        )

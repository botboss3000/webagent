"""Database-backed CanvasStore.

Stores page metadata + HTML body in the `pages` table via the active
StorageBackend (SQLite or Supabase). Survives container restarts, gets
backed up with the DB, and stays consistent with the rest of the app's
storage architecture.
"""

import json
from typing import Dict, List, Optional

from app.db import get_db
from app.canvas_store.interface import CanvasStore
from app.canvas_store.common import (
    default_agent_context,
    home_agent_context,
    canvas_url,
    safe,
)


def _entry(row: Dict, user_id: str) -> Dict:
    """Project a DB row into the manifest-entry shape the UI expects."""
    return {
        "slug": row["slug"],
        "title": row["title"],
        "agent_context": row.get("agent_context") or "",
        "agent_id": row.get("agent_id") or "",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "url": canvas_url(user_id, row["slug"]),
    }


class DatabaseCanvasStore(CanvasStore):
    name = "database"

    async def list_canvases(self, user_id: str) -> List[Dict]:
        rows = await get_db().canvas_list(user_id)
        return [_entry(r, user_id) for r in rows]

    async def get_canvas_html(self, user_id: str, slug: str) -> Optional[str]:
        row = await get_db().canvas_get(user_id, slug)
        return row.get("html") if row else None

    async def save_canvas_html(self, user_id: str, slug: str, html: str, agent_id: str = "") -> str:
        existing = await get_db().canvas_get(user_id, slug)
        title = (existing or {}).get("title") or slug.capitalize()
        ctx = (existing or {}).get("agent_context") or ""
        await get_db().canvas_upsert(
            user_id=user_id,
            slug=slug,
            title=title,
            agent_context=ctx,
            html=html,
            agent_id=agent_id,
        )
        return canvas_url(user_id, slug)

    async def create_canvas(
        self,
        user_id: str,
        slug: str,
        title: str,
        agent_context: str = "",
        initial_html: str = "",
        agent_id: str = "",
    ) -> Dict:
        from app.canvas_store.common import blank_canvas_html
        safe_slug = safe(slug)
        if await get_db().canvas_get(user_id, safe_slug):
            raise ValueError(f"Canvas '{safe_slug}' already exists")
        row = await get_db().canvas_upsert(
            user_id=user_id,
            slug=safe_slug,
            title=title,
            agent_context=agent_context or default_agent_context(title),
            html=initial_html or blank_canvas_html(title),
            agent_id=agent_id,
        )
        return _entry(row, user_id)

    async def delete_canvas(self, user_id: str, slug: str) -> bool:
        if safe(slug) == "home":
            return False
        return await get_db().canvas_delete(user_id, safe(slug))

    async def rename_canvas(self, user_id: str, slug: str, new_title: str) -> bool:
        safe_slug = safe(slug)
        existing = await get_db().canvas_get(user_id, safe_slug)
        if not existing:
            return False
        # html=None on upsert preserves the existing body and only updates
        # the metadata columns (title + agent_context + updated_at).
        await get_db().canvas_upsert(
            user_id=user_id,
            slug=safe_slug,
            title=new_title,
            agent_context=existing.get("agent_context") or "",
            html=None,
        )
        return True

    async def get_canvas_data(self, user_id: str, slug: str) -> Optional[Dict]:
        raw = await get_db().canvas_get_data(user_id, safe(slug))
        if not raw:
            return None
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    async def save_canvas_data(self, user_id: str, slug: str, data: Dict) -> None:
        await get_db().canvas_set_data(user_id, safe(slug), json.dumps(data))

    async def ensure_home_canvas(self, user_id: str, seed_html: str) -> None:
        existing = await get_db().canvas_get(user_id, "home")
        if existing:
            return
        await get_db().canvas_upsert(
            user_id=user_id,
            slug="home",
            title="Home",
            agent_context=home_agent_context(),
            html=seed_html,
        )
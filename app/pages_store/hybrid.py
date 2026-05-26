"""Hybrid PageStore.

Metadata (slug, title, agent_context, timestamps) lives in the `pages`
table via the active StorageBackend; the HTML body lives on disk at
visuals/users/{uid}/{slug}.html.

Trade-off: page index survives container restarts and is queryable across
backends, but HTML bodies remain hand-editable on disk and serve from a
single fast read. Best of both for setups that want DB-backed catalog +
direct file access. Container restarts will still wipe the bodies unless
the disk is persistent.
"""

import os
from typing import Dict, List, Optional

from app.db import get_db
from app.pages_store.interface import PageStore
from app.pages_store.common import (
    USERS_DIR,
    blank_page_html,
    default_agent_context,
    home_agent_context,
    page_url,
    safe,
)


def _entry(row: Dict, user_id: str) -> Dict:
    return {
        "slug": row["slug"],
        "title": row["title"],
        "agent_context": row.get("agent_context") or "",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "url": page_url(user_id, row["slug"]),
    }


class HybridPageStore(PageStore):
    name = "hybrid"

    def _page_path(self, user_id: str, slug: str) -> str:
        return os.path.join(USERS_DIR, safe(user_id), f"{safe(slug)}.html")

    def _ensure_user_dir(self, user_id: str) -> str:
        d = os.path.join(USERS_DIR, safe(user_id))
        os.makedirs(d, exist_ok=True)
        return d

    def _write_body(self, user_id: str, slug: str, html: str) -> None:
        self._ensure_user_dir(user_id)
        with open(self._page_path(user_id, slug), "w", encoding="utf-8") as f:
            f.write(html)

    def _read_body(self, user_id: str, slug: str) -> Optional[str]:
        path = self._page_path(user_id, slug)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    async def list_pages(self, user_id: str) -> List[Dict]:
        rows = await get_db().pages_list(user_id)
        return [_entry(r, user_id) for r in rows]

    async def get_page_html(self, user_id: str, slug: str) -> Optional[str]:
        return self._read_body(user_id, slug)

    async def save_page_html(self, user_id: str, slug: str, html: str) -> str:
        existing = await get_db().pages_get(user_id, slug)
        title = (existing or {}).get("title") or slug.capitalize()
        ctx = (existing or {}).get("agent_context") or ""
        # html=None on upsert leaves the column NULL (body lives on disk)
        await get_db().pages_upsert(
            user_id=user_id,
            slug=slug,
            title=title,
            agent_context=ctx,
            html=None,
        )
        self._write_body(user_id, slug, html)
        return page_url(user_id, slug)

    async def create_page(
        self,
        user_id: str,
        slug: str,
        title: str,
        agent_context: str = "",
        initial_html: str = "",
    ) -> Dict:
        safe_slug = safe(slug)
        if await get_db().pages_get(user_id, safe_slug):
            raise ValueError(f"Page '{safe_slug}' already exists")
        row = await get_db().pages_upsert(
            user_id=user_id,
            slug=safe_slug,
            title=title,
            agent_context=agent_context or default_agent_context(title),
            html=None,
        )
        self._write_body(user_id, safe_slug, initial_html or blank_page_html(title))
        return _entry(row, user_id)

    async def delete_page(self, user_id: str, slug: str) -> bool:
        safe_slug = safe(slug)
        if safe_slug == "home":
            return False
        deleted = await get_db().pages_delete(user_id, safe_slug)
        path = self._page_path(user_id, safe_slug)
        if os.path.exists(path):
            os.remove(path)
        return deleted

    async def rename_page(self, user_id: str, slug: str, new_title: str) -> bool:
        safe_slug = safe(slug)
        existing = await get_db().pages_get(user_id, safe_slug)
        if not existing:
            return False
        # Body lives on disk — only the DB metadata row needs updating.
        await get_db().pages_upsert(
            user_id=user_id,
            slug=safe_slug,
            title=new_title,
            agent_context=existing.get("agent_context") or "",
            html=None,
        )
        return True

    async def ensure_home_page(self, user_id: str, seed_html: str) -> None:
        existing = await get_db().pages_get(user_id, "home")
        if not existing:
            await get_db().pages_upsert(
                user_id=user_id,
                slug="home",
                title="Home",
                agent_context=home_agent_context(),
                html=None,
            )
        if not os.path.exists(self._page_path(user_id, "home")):
            self._write_body(user_id, "home", seed_html)

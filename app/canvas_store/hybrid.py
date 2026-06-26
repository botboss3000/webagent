"""Hybrid CanvasStore.

Metadata (slug, title, agent_context, timestamps) lives in the `pages`
table via the active StorageBackend; the HTML body lives on disk at
data/user_data/{uid}/canvas/{slug}/index.html (one folder per canvas).

Trade-off: canvas index survives container restarts and is queryable across
backends, but HTML bodies remain hand-editable on disk and serve from a
single fast read. Best of both for setups that want DB-backed catalog +
direct file access. Container restarts will still wipe the bodies unless
the disk is persistent.
"""

import os
import shutil
from typing import Dict, List, Optional

from app.db import get_db
from app.canvas_store.interface import CanvasStore
from app.canvas_store.common import (
    canvas_dir,
    canvas_body_path,
    read_canvas_data,
    write_canvas_data,
    blank_canvas_html,
    default_agent_context,
    home_agent_context,
    canvas_url,
    safe,
)


def _entry(row: Dict, user_id: str) -> Dict:
    return {
        "slug": row["slug"],
        "title": row["title"],
        "agent_context": row.get("agent_context") or "",
        "agent_id": row.get("agent_id") or "",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "url": canvas_url(user_id, row["slug"]),
    }


class HybridCanvasStore(CanvasStore):
    name = "hybrid"

    def _canvas_path(self, user_id: str, slug: str) -> str:
        return canvas_body_path(user_id, slug)

    def _ensure_canvas_dir(self, user_id: str, slug: str) -> str:
        d = canvas_dir(user_id, slug)
        os.makedirs(d, exist_ok=True)
        return d

    def _write_body(self, user_id: str, slug: str, html: str) -> None:
        self._ensure_canvas_dir(user_id, slug)
        with open(self._canvas_path(user_id, slug), "w", encoding="utf-8") as f:
            f.write(html)

    def _read_body(self, user_id: str, slug: str) -> Optional[str]:
        path = self._canvas_path(user_id, slug)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    async def list_canvases(self, user_id: str) -> List[Dict]:
        rows = await get_db().canvas_list(user_id)
        return [_entry(r, user_id) for r in rows]

    async def get_canvas_html(self, user_id: str, slug: str) -> Optional[str]:
        return self._read_body(user_id, slug)

    async def save_canvas_html(self, user_id: str, slug: str, html: str, agent_id: str = "") -> str:
        existing = await get_db().canvas_get(user_id, slug)
        title = (existing or {}).get("title") or slug.capitalize()
        ctx = (existing or {}).get("agent_context") or ""
        # html=None on upsert leaves the column NULL (body lives on disk)
        await get_db().canvas_upsert(
            user_id=user_id,
            slug=slug,
            title=title,
            agent_context=ctx,
            html=None,
            agent_id=agent_id,
        )
        self._write_body(user_id, slug, html)
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
        safe_slug = safe(slug)
        if await get_db().canvas_get(user_id, safe_slug):
            raise ValueError(f"Canvas '{safe_slug}' already exists")
        row = await get_db().canvas_upsert(
            user_id=user_id,
            slug=safe_slug,
            title=title,
            agent_context=agent_context or default_agent_context(title),
            html=None,
            agent_id=agent_id,
        )
        self._write_body(user_id, safe_slug, initial_html or blank_canvas_html(title))
        return _entry(row, user_id)

    async def delete_canvas(self, user_id: str, slug: str) -> bool:
        safe_slug = safe(slug)
        if safe_slug == "home":
            return False
        deleted = await get_db().canvas_delete(user_id, safe_slug)
        folder = canvas_dir(user_id, safe_slug)
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
        return deleted

    async def rename_canvas(self, user_id: str, slug: str, new_title: str) -> bool:
        safe_slug = safe(slug)
        existing = await get_db().canvas_get(user_id, safe_slug)
        if not existing:
            return False
        # Body lives on disk — only the DB metadata row needs updating.
        await get_db().canvas_upsert(
            user_id=user_id,
            slug=safe_slug,
            title=new_title,
            agent_context=existing.get("agent_context") or "",
            html=None,
        )
        return True

    async def get_canvas_data(self, user_id: str, slug: str) -> Optional[Dict]:
        # Like the HTML body, canvas data lives on disk in hybrid mode.
        return read_canvas_data(user_id, safe(slug))

    async def save_canvas_data(self, user_id: str, slug: str, data: Dict) -> None:
        safe_slug = safe(slug)
        self._ensure_canvas_dir(user_id, safe_slug)
        write_canvas_data(user_id, safe_slug, data)

    async def ensure_home_canvas(self, user_id: str, seed_html: str) -> None:
        existing = await get_db().canvas_get(user_id, "home")
        if not existing:
            await get_db().canvas_upsert(
                user_id=user_id,
                slug="home",
                title="Home",
                agent_context=home_agent_context(),
                html=None,
            )
        if not os.path.exists(self._canvas_path(user_id, "home")):
            self._write_body(user_id, "home", seed_html)
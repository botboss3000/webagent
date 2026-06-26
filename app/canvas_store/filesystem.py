"""Filesystem-backed CanvasStore — default.

Stores each canvas per-user at data/user_data/{user_id}/canvas/{slug}/index.html
— one folder per canvas. Following the same drop-in philosophy as abilities and
main-panel pages, a folder containing an ``index.html`` IS a canvas: no central
manifest. Each folder MAY carry an optional ``page.json`` descriptor holding the
canvas's title, agent_context, description and display ``order``; when it's
absent the title defaults to the slug (the folder name) and the canvas sorts
alphabetically after the explicitly-ordered ones. Drop a folder in → it appears;
delete it → it's gone.

Simple, fast, hand-editable on disk. Loses canvases on container restart in
stateless deploys (e.g. Cloud Run) — for those use the database or hybrid
backends.
"""

import os
import shutil
from typing import Dict, List, Optional

from app.canvas_store.interface import CanvasStore
from app.canvas_store.common import (
    canvas_dir,
    canvas_body_path,
    canvas_entry,
    discover_canvas_slugs,
    read_canvas_meta,
    write_canvas_meta,
    read_canvas_data,
    write_canvas_data,
    sort_canvas_entries,
    blank_canvas_html,
    default_agent_context,
    home_agent_context,
    canvas_url,
    now_iso,
    safe,
)


class FilesystemCanvasStore(CanvasStore):
    name = "filesystem"

    # ── Path helpers ─────────────────────────────────────────────────────

    def _canvas_path(self, user_id: str, slug: str) -> str:
        return canvas_body_path(user_id, slug)

    def _ensure_canvas_dir(self, user_id: str, slug: str) -> str:
        d = canvas_dir(user_id, slug)
        os.makedirs(d, exist_ok=True)
        return d

    # ── CanvasStore impl ───────────────────────────────────────────────────

    async def list_canvases(self, user_id: str) -> List[Dict]:
        entries = [canvas_entry(user_id, slug) for slug in discover_canvas_slugs(user_id)]
        return sort_canvas_entries(entries)

    async def get_canvas_html(self, user_id: str, slug: str) -> Optional[str]:
        path = self._canvas_path(user_id, safe(slug))
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    async def save_canvas_html(self, user_id: str, slug: str, html: str, agent_id: str = "") -> str:
        safe_slug = safe(slug)
        self._ensure_canvas_dir(user_id, safe_slug)
        with open(self._canvas_path(user_id, safe_slug), "w", encoding="utf-8") as f:
            f.write(html)
        # Bump updated_at when a descriptor already exists; ALSO write one when an
        # agent rendered this canvas (agent_id given) so its owning-agent link
        # persists — a supplied agent_id wins, otherwise the existing owner is
        # kept. A descriptor-less, agent-less render stays descriptor-free.
        meta = read_canvas_meta(user_id, safe_slug)
        if meta or agent_id:
            if agent_id:
                meta["agent_id"] = agent_id
            meta["updated_at"] = now_iso()
            write_canvas_meta(user_id, safe_slug, meta)
        return canvas_url(user_id, safe_slug)

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
        if os.path.exists(self._canvas_path(user_id, safe_slug)):
            raise ValueError(f"Canvas '{safe_slug}' already exists")
        now = now_iso()
        meta = {
            "title": title,
            "agent_context": agent_context or default_agent_context(title),
            "agent_id": agent_id or "",
            "description": "",
            "created_at": now,
            "updated_at": now,
        }
        self._ensure_canvas_dir(user_id, safe_slug)
        with open(self._canvas_path(user_id, safe_slug), "w", encoding="utf-8") as f:
            f.write(initial_html or blank_canvas_html(title))
        write_canvas_meta(user_id, safe_slug, meta)
        return canvas_entry(user_id, safe_slug)

    async def delete_canvas(self, user_id: str, slug: str) -> bool:
        safe_slug = safe(slug)
        if safe_slug == "home":
            return False
        folder = canvas_dir(user_id, safe_slug)
        if not os.path.isdir(folder):
            return False
        shutil.rmtree(folder, ignore_errors=True)
        return True

    async def rename_canvas(self, user_id: str, slug: str, new_title: str) -> bool:
        safe_slug = safe(slug)
        if not os.path.exists(self._canvas_path(user_id, safe_slug)):
            return False
        # Persisting a title requires a descriptor — create one if the canvas
        # didn't have a page.json yet.
        meta = read_canvas_meta(user_id, safe_slug)
        meta["title"] = new_title
        meta["updated_at"] = now_iso()
        if not meta.get("agent_context"):
            meta["agent_context"] = default_agent_context(new_title)
        write_canvas_meta(user_id, safe_slug, meta)
        return True

    async def get_canvas_data(self, user_id: str, slug: str) -> Optional[Dict]:
        return read_canvas_data(user_id, safe(slug))

    async def save_canvas_data(self, user_id: str, slug: str, data: Dict) -> None:
        safe_slug = safe(slug)
        self._ensure_canvas_dir(user_id, safe_slug)
        write_canvas_data(user_id, safe_slug, data)
        # Mirror save_canvas_html: bump updated_at only when a descriptor exists.
        meta = read_canvas_meta(user_id, safe_slug)
        if meta:
            meta["updated_at"] = now_iso()
            write_canvas_meta(user_id, safe_slug, meta)

    async def ensure_home_canvas(self, user_id: str, seed_html: str) -> None:
        if not os.path.exists(self._canvas_path(user_id, "home")):
            self._ensure_canvas_dir(user_id, "home")
            with open(self._canvas_path(user_id, "home"), "w", encoding="utf-8") as f:
                f.write(seed_html)
        if not read_canvas_meta(user_id, "home"):
            now = now_iso()
            write_canvas_meta(user_id, "home", {
                "title": "Home",
                "agent_context": home_agent_context(),
                "description": "",
                "order": 0,  # Home always sorts first.
                "created_at": now,
                "updated_at": now,
            })

"""Filesystem-backed PageStore — default.

Stores pages per-user at visuals/users/{user_id}/{slug}.html, with a
pages.json manifest in the same directory. Simple, fast, hand-editable on
disk, and easy to git-commit. Loses pages on container restart in stateless
deploys (e.g. Cloud Run) — for those use the database or hybrid backends.
"""

import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.pages_store.interface import PageStore
from app.pages_store.common import (
    USERS_DIR,
    blank_page_html,
    default_agent_context,
    home_agent_context,
    page_url,
    safe,
)


class FilesystemPageStore(PageStore):
    name = "filesystem"

    # ── Path helpers ─────────────────────────────────────────────────────

    def _user_dir(self, user_id: str) -> str:
        return os.path.join(USERS_DIR, safe(user_id))

    def _manifest_path(self, user_id: str) -> str:
        return os.path.join(self._user_dir(user_id), "pages.json")

    def _page_path(self, user_id: str, slug: str) -> str:
        return os.path.join(self._user_dir(user_id), f"{safe(slug)}.html")

    def _ensure_user_dir(self, user_id: str) -> str:
        d = self._user_dir(user_id)
        os.makedirs(d, exist_ok=True)
        return d

    def _load_manifest(self, user_id: str) -> List[Dict]:
        path = self._manifest_path(user_id)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_manifest(self, user_id: str, pages: List[Dict]) -> None:
        self._ensure_user_dir(user_id)
        with open(self._manifest_path(user_id), "w", encoding="utf-8") as f:
            json.dump(pages, f, indent=2)

    # ── PageStore impl ───────────────────────────────────────────────────

    async def list_pages(self, user_id: str) -> List[Dict]:
        pages = self._load_manifest(user_id)
        return [dict(p, url=page_url(user_id, p["slug"])) for p in pages]

    async def get_page_html(self, user_id: str, slug: str) -> Optional[str]:
        path = self._page_path(user_id, slug)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    async def save_page_html(self, user_id: str, slug: str, html: str) -> str:
        self._ensure_user_dir(user_id)
        with open(self._page_path(user_id, slug), "w", encoding="utf-8") as f:
            f.write(html)
        pages = self._load_manifest(user_id)
        now = datetime.now(timezone.utc).isoformat()
        for page in pages:
            if page["slug"] == slug:
                page["updated_at"] = now
                break
        self._save_manifest(user_id, pages)
        return page_url(user_id, slug)

    async def create_page(
        self,
        user_id: str,
        slug: str,
        title: str,
        agent_context: str = "",
        initial_html: str = "",
    ) -> Dict:
        pages = self._load_manifest(user_id)
        safe_slug = safe(slug)
        if any(p["slug"] == safe_slug for p in pages):
            raise ValueError(f"Page '{safe_slug}' already exists")
        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "slug": safe_slug,
            "title": title,
            "agent_context": agent_context or default_agent_context(title),
            "created_at": now,
            "updated_at": now,
        }
        pages.append(entry)
        self._save_manifest(user_id, pages)
        await self.save_page_html(user_id, safe_slug, initial_html or blank_page_html(title))
        return dict(entry, url=page_url(user_id, safe_slug))

    async def delete_page(self, user_id: str, slug: str) -> bool:
        safe_slug = safe(slug)
        if safe_slug == "home":
            return False
        pages = self._load_manifest(user_id)
        new_pages = [p for p in pages if p["slug"] != safe_slug]
        if len(new_pages) == len(pages):
            return False
        self._save_manifest(user_id, new_pages)
        path = self._page_path(user_id, safe_slug)
        if os.path.exists(path):
            os.remove(path)
        return True

    async def ensure_home_page(self, user_id: str, seed_html: str) -> None:
        pages = self._load_manifest(user_id)
        if not any(p["slug"] == "home" for p in pages):
            now = datetime.now(timezone.utc).isoformat()
            entry = {
                "slug": "home",
                "title": "Home",
                "agent_context": home_agent_context(),
                "created_at": now,
                "updated_at": now,
            }
            # Home page always goes first
            self._save_manifest(user_id, [entry] + [p for p in pages if p["slug"] != "home"])
        if not os.path.exists(self._page_path(user_id, "home")):
            self._ensure_user_dir(user_id)
            with open(self._page_path(user_id, "home"), "w", encoding="utf-8") as f:
                f.write(seed_html)

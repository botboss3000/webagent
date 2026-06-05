"""Async facade over the wiki storage layer.

Thin wrapper around the ``wiki_*`` methods on the active StorageBackend
(``app/db/local.py``), plus slug derivation and a couple of validation rules.
Used by both the HTTP API (``app/api/wiki.py``) and the agent tools
(``app/tools/wiki_tools.py``) so the two stay in lock-step.
"""
from __future__ import annotations

import re
from typing import List, Optional

from app.wiki.db import get_wiki_store

MAX_TITLE = 120


def slugify(text: str) -> str:
    """Derive a url-safe, globally-unique-ish handle from a title.

    Lowercase, spaces/punctuation collapse to single hyphens, trimmed. Empty
    input falls back to ``"untitled"``.
    """
    s = (text or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)        # drop punctuation
    s = re.sub(r"[\s_-]+", "-", s)         # whitespace/underscores → hyphen
    s = s.strip("-")
    return s or "untitled"


def _norm_tags(tags) -> List[str]:
    """Coerce tags into a clean list of non-empty strings."""
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = re.split(r"[,\n]", tags)
    out = []
    for t in tags:
        t = str(t).strip()
        if t and t not in out:
            out.append(t)
    return out


async def list_articles() -> List[dict]:
    """All articles (metadata + short snippet), newest first."""
    return await get_wiki_store().list()


async def get_article(slug: str) -> Optional[dict]:
    """Full article by slug, or None."""
    return await get_wiki_store().get(slug)


async def search_articles(query: str, limit: int = 10) -> List[dict]:
    """Hybrid (semantic + keyword) search. Returns ranked results with snippets."""
    query = (query or "").strip()
    if not query:
        return []
    return await get_wiki_store().search(query, limit=limit)


async def create_article(
    title: str,
    body: str = "",
    tags=None,
    category: str = "",
    user_id: str = "",
    slug: Optional[str] = None,
) -> dict:
    """Create a new article. Slug is derived from the title unless given; a
    numeric suffix is appended if the slug is already taken."""
    title = (title or "").strip()
    if not title:
        raise ValueError("Title is required.")
    store = get_wiki_store()
    base = slugify(slug or title)
    final = base
    n = 2
    while await store.get(final) is not None:
        final = f"{base}-{n}"
        n += 1
    return await store.upsert(
        slug=final,
        title=title[:MAX_TITLE],
        body=body or "",
        tags=_norm_tags(tags),
        category=(category or "").strip(),
        user_id=user_id,
    )


async def update_article(
    slug: str,
    title: Optional[str] = None,
    body: Optional[str] = None,
    tags=None,
    category: Optional[str] = None,
    user_id: str = "",
) -> Optional[dict]:
    """Update fields on an existing article. Only provided fields change; the
    slug (URL) is preserved. Returns the updated article, or None if missing."""
    store = get_wiki_store()
    existing = await store.get(slug)
    if existing is None:
        return None
    new_title = (title if title is not None else existing["title"]).strip()
    if not new_title:
        raise ValueError("Title cannot be empty.")
    new_tags = _norm_tags(tags) if tags is not None else existing.get("tags", [])
    return await store.upsert(
        slug=slug,
        title=new_title[:MAX_TITLE],
        body=body if body is not None else existing.get("body", ""),
        tags=new_tags,
        category=(category if category is not None else existing.get("category", "")).strip(),
        user_id=user_id,
    )


async def delete_article(slug: str) -> bool:
    """Delete an article. Returns False if it didn't exist."""
    return await get_wiki_store().delete(slug)

"""
REST API for the company-wide Wiki — a shared, searchable knowledge base.

Unlike Pages, the Wiki is NOT per-user: every article is visible to everyone and
searched together. ``user_id`` is recorded for attribution only.

Endpoints:
  GET    /api/v1/wiki                 — list all articles (metadata + snippet)
  GET    /api/v1/wiki/search?q=…      — hybrid (semantic + keyword) search
  GET    /api/v1/wiki/{slug}          — one full article
  POST   /api/v1/wiki                 — create (slug derived from title)
  PATCH  /api/v1/wiki/{slug}          — update title/body/tags/category
  DELETE /api/v1/wiki/{slug}          — delete
"""
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.wiki.store import (
    list_articles,
    get_article,
    search_articles,
    create_article,
    update_article,
    delete_article,
)

router = APIRouter(prefix="/api/v1/wiki", tags=["wiki"])


class CreateArticleRequest(BaseModel):
    title: str
    body: Optional[str] = ""
    tags: Optional[List[str]] = None
    category: Optional[str] = ""
    user_id: Optional[str] = ""


class UpdateArticleRequest(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    tags: Optional[List[str]] = None
    category: Optional[str] = None
    user_id: Optional[str] = ""


@router.get("")
async def api_list_wiki():
    """List every article (metadata + a short body snippet), newest first."""
    articles = await list_articles()
    return {"status": "ok", "articles": articles, "count": len(articles)}


@router.get("/search")
async def api_search_wiki(
    q: str = Query("", description="Search query"),
    limit: int = Query(10, ge=1, le=50),
):
    """Hybrid semantic + keyword search. Returns ranked results with snippets."""
    results = await search_articles(q, limit=limit)
    return {"status": "ok", "results": results, "count": len(results), "query": q}


@router.get("/{slug}")
async def api_get_wiki(slug: str):
    """Return one full article by slug."""
    article = await get_article(slug)
    if article is None:
        raise HTTPException(status_code=404, detail=f"Article '{slug}' not found.")
    return {"status": "ok", "article": article}


@router.post("")
async def api_create_wiki(body: CreateArticleRequest):
    """Create a new article. The slug is derived from the title."""
    try:
        article = await create_article(
            title=body.title,
            body=body.body or "",
            tags=body.tags,
            category=body.category or "",
            user_id=body.user_id or "",
        )
        return {"status": "ok", "article": article}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{slug}")
async def api_update_wiki(slug: str, body: UpdateArticleRequest):
    """Update an existing article's fields. The slug (URL) is preserved."""
    try:
        article = await update_article(
            slug=slug,
            title=body.title,
            body=body.body,
            tags=body.tags,
            category=body.category,
            user_id=body.user_id or "",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if article is None:
        raise HTTPException(status_code=404, detail=f"Article '{slug}' not found.")
    return {"status": "ok", "article": article}


@router.delete("/{slug}")
async def api_delete_wiki(slug: str):
    """Delete an article."""
    ok = await delete_article(slug)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Article '{slug}' not found.")
    return {"status": "ok", "message": f"Article '{slug}' deleted."}

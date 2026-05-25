"""
REST API for the AutoAgent multi-page workspace.

Endpoints:
  GET    /api/v1/pages                          — list pages for a user
  POST   /api/v1/pages                          — create a new page
  DELETE /api/v1/pages/{slug}                   — delete a page
  GET    /api/v1/pages/{user_id}/{slug}/html    — render HTML for the iframe
"""
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional

from app.visualizer.pages import (
    list_pages,
    create_page,
    delete_page,
    get_page_html,
)

router = APIRouter(prefix="/api/v1/pages", tags=["pages"])


class CreatePageRequest(BaseModel):
    user_id: str
    slug: str
    title: str
    agent_context: Optional[str] = ""
    initial_html: Optional[str] = ""


@router.get("")
async def api_list_pages(user_id: str = Query(..., description="User ID")):
    """Return all pages for the given user. Seeds the home page if missing."""
    pages = await list_pages(user_id)
    return {"status": "ok", "pages": pages, "count": len(pages)}


@router.post("")
async def api_create_page(body: CreatePageRequest):
    """Create a new page for the user."""
    try:
        entry = await create_page(
            user_id=body.user_id,
            slug=body.slug,
            title=body.title,
            agent_context=body.agent_context or "",
            initial_html=body.initial_html or "",
        )
        return {"status": "ok", "page": entry}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.delete("/{slug}")
async def api_delete_page(slug: str, user_id: str = Query(..., description="User ID")):
    """Delete a page. The home page cannot be deleted."""
    if slug == "home":
        raise HTTPException(status_code=403, detail="The home page cannot be deleted.")
    ok = await delete_page(user_id=user_id, slug=slug)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Page '{slug}' not found.")
    return {"status": "ok", "message": f"Page '{slug}' deleted."}


@router.get("/{user_id}/{slug}/html", response_class=HTMLResponse)
async def api_get_page_html(user_id: str, slug: str):
    """Serve a page's HTML body for the iframe. Works the same across all
    PageStore backends — filesystem, database, or hybrid."""
    # Seed the home page on first request through this endpoint, mirroring
    # the behavior of /api/v1/pages so a direct deep-link to /home/html
    # doesn't 404 a new user.
    if slug == "home":
        await list_pages(user_id)
    html = await get_page_html(user_id, slug)
    if html is None:
        raise HTTPException(status_code=404, detail=f"Page '{slug}' not found.")
    return HTMLResponse(content=html)

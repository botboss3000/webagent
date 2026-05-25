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


# Slim themed scrollbar injected into iframe pages. The parent app's global
# scrollbar styles can't reach the sandboxed iframe, so without this the OS
# default scrollbar would show. Uses prefers-color-scheme so it adapts to the
# theme the agent renders the page with (orange in light docs, blue in dark).
_SCROLLBAR_STYLE = """<style id="webagent-scrollbar-inject">
*{scrollbar-width:thin;scrollbar-color:rgba(125,207,255,0.32) transparent;}
*::-webkit-scrollbar{width:6px;height:6px;}
*::-webkit-scrollbar-track{background:transparent;}
*::-webkit-scrollbar-thumb{background:rgba(125,207,255,0.38);border-radius:999px;}
*::-webkit-scrollbar-thumb:hover{background:rgba(125,207,255,0.62);}
*::-webkit-scrollbar-corner{background:transparent;}
@media (prefers-color-scheme: light){
*{scrollbar-color:rgba(255,140,66,0.40) transparent;}
*::-webkit-scrollbar-thumb{background:rgba(255,140,66,0.48);}
*::-webkit-scrollbar-thumb:hover{background:rgba(255,140,66,0.72);}
}
</style>"""


def _inject_scrollbar_style(html: str) -> str:
    """Inject a slim themed scrollbar stylesheet into the iframe page so the
    inner document doesn't render the OS default scrollbar. Inserts right
    after <head> when present, otherwise prepends."""
    if not html:
        return html
    lower = html.lower()
    idx = lower.find("<head>")
    if idx != -1:
        insert_at = idx + len("<head>")
        return html[:insert_at] + _SCROLLBAR_STYLE + html[insert_at:]
    return _SCROLLBAR_STYLE + html


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
    return HTMLResponse(content=_inject_scrollbar_style(html))

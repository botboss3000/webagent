"""
Visualizer module -- AutoAgent multi-page workspace.

Injects the following tools into the agent's tool registry:
  render_visual   -- write HTML to a named page
  list_pages      -- list all pages for the current user
  create_page     -- create a new named page
  delete_page     -- delete a page (home page is protected)

Call register_tools(tools, user_id) from app/tools/loader.py.
"""
import json
from typing import Dict
from app.tools.loader import ToolInfo


def register_tools(tools: Dict[str, ToolInfo], user_id: str) -> None:
    """Inject visualizer tools into the tools dict."""

    from app.visualizer.tool import render_visual as _render_visual
    from app.visualizer.pages import (
        list_pages as _list_pages,
        create_page as _create_page,
        delete_page as _delete_page,
    )

    # -- render_visual ---------------------------------------------------------
    async def _render_visual_wrapper(html: str, title: str = "", page_name: str = "home"):
        return await _render_visual(
            html=html,
            title=title,
            page_name=page_name,
            user_id=user_id,
        )

    tools["render_visual"] = ToolInfo(
        name="render_visual",
        handler=_render_visual_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "html": {
                    "type": "string",
                    "description": (
                        "Full HTML document string to render. "
                        "For p5.js sketches, include the p5.js CDN script tag. "
                        "For regular pages, standard HTML/CSS/JS is fine."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "Human-readable title for the page (shown in the UI).",
                },
                "page_name": {
                    "type": "string",
                    "description": (
                        "Slug of the page to write to (e.g. 'home', 'dashboard', 'notes'). "
                        "Must match the page the user is currently viewing. Defaults to 'home'."
                    ),
                },
            },
            "required": ["html"],
        },
    )

    # -- list_pages ------------------------------------------------------------
    async def _list_pages_wrapper():
        pages = await _list_pages(user_id)
        return json.dumps({"status": "ok", "pages": pages, "count": len(pages)})

    tools["list_pages"] = ToolInfo(
        name="list_pages",
        handler=_list_pages_wrapper,
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
    )

    # -- create_page -----------------------------------------------------------
    async def _create_page_wrapper(
        slug: str,
        title: str,
        agent_context: str = "",
        initial_html: str = "",
    ):
        try:
            entry = await _create_page(
                user_id=user_id,
                slug=slug,
                title=title,
                agent_context=agent_context,
                initial_html=initial_html,
            )
            return json.dumps({"status": "ok", "page": entry})
        except ValueError as e:
            return json.dumps({"status": "error", "message": str(e)})

    tools["create_page"] = ToolInfo(
        name="create_page",
        handler=_create_page_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "URL-safe identifier for the page (e.g. 'dashboard', 'notes'). Lowercase, no spaces.",
                },
                "title": {
                    "type": "string",
                    "description": "Human-readable display name for the page (e.g. 'My Dashboard').",
                },
                "agent_context": {
                    "type": "string",
                    "description": (
                        "Optional system prompt / persona for this page's agent. "
                        "Describes the agent's role and the page's purpose. "
                        "If omitted, a default context is generated from the title."
                    ),
                },
                "initial_html": {
                    "type": "string",
                    "description": "Optional initial HTML to seed the page with. If omitted, a blank placeholder is used.",
                },
            },
            "required": ["slug", "title"],
        },
    )

    # -- delete_page -----------------------------------------------------------
    async def _delete_page_wrapper(slug: str):
        ok = await _delete_page(user_id=user_id, slug=slug)
        if ok:
            return json.dumps({"status": "ok", "message": "Page '{}' deleted.".format(slug)})
        if slug == "home":
            return json.dumps({"status": "error", "message": "The home page cannot be deleted."})
        return json.dumps({"status": "error", "message": "Page '{}' not found.".format(slug)})

    tools["delete_page"] = ToolInfo(
        name="delete_page",
        handler=_delete_page_wrapper,
        parameters={
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Slug of the page to delete. The 'home' page cannot be deleted.",
                },
            },
            "required": ["slug"],
        },
    )

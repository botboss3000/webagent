"""
Page management for the Canvas multi-page workspace.

Thin async facade over the configurable PageStore (filesystem / database /
hybrid). Pick the backend from the App Config → Storage panel or via the
WEBAGENT_CANVAS_STORE env var. See app/canvas_store/__init__.py for details.
"""
from typing import Dict, List, Optional

from app.canvas_store import get_canvases_store
from app.canvas_store.seed import home_canvas_html
from app.canvas_store import common as _canvas_files


async def list_canvases(user_id: str) -> List[Dict]:
    """Return all pages for a user, seeding home if missing."""
    store = get_canvases_store()
    await store.ensure_home_canvas(user_id, home_canvas_html())
    return await store.list_canvases(user_id)


async def get_canvas_html(user_id: str, slug: str) -> Optional[str]:
    return await get_canvases_store().get_canvas_html(user_id, slug)


async def get_canvas_data(user_id: str, slug: str) -> Optional[Dict]:
    """Return a canvas's data bag (the content it renders), or None when unset."""
    return await get_canvases_store().get_canvas_data(user_id, slug)


async def save_canvas_data(user_id: str, slug: str, data: Dict) -> None:
    """Replace a canvas's data bag. The canvas reads this instead of hardcoding
    its content into the page, so updating data never touches index.html."""
    await get_canvases_store().save_canvas_data(user_id, slug, data)


async def save_canvas_html(user_id: str, slug: str, html: str, title: str = "", agent_id: str = "") -> str:
    """Write HTML to a canvas, auto-registering it in the manifest if it's new.

    The store's own ``save_canvas_html`` only updates the timestamp of an
    EXISTING manifest entry — a brand-new slug written straight through
    ``render_visual`` (without a prior ``create_canvas``) would save its HTML but
    never appear in the page selector. So when the slug isn't listed yet we
    create it (which registers it + writes the HTML in one step); the agent can
    therefore add a new canvas simply by rendering to it.

    ``agent_id`` records the agent doing the render as the canvas's owner — set on
    the auto-create and carried into the plain save (last writer wins; an existing
    owner is preserved when no agent_id is supplied).
    """
    store = get_canvases_store()
    try:
        existing = await store.list_canvases(user_id)
        if not any(p.get("slug") == slug for p in (existing or [])):
            nice = title or slug.replace("-", " ").replace("_", " ").strip().title() or slug
            try:
                created = await store.create_canvas(
                    user_id=user_id, slug=slug, title=nice, initial_html=html,
                    agent_id=agent_id,
                )
                if isinstance(created, dict) and created.get("url"):
                    return created["url"]
            except ValueError:
                pass  # already exists (race) — fall through to a plain save
    except Exception:
        pass  # never let registration failure block the actual save
    saved = await store.save_canvas_html(user_id, slug, html, agent_id=agent_id)
    # A re-render is a NEW version of the page — wipe its console log so the agent
    # only ever reads errors from the build that's now running (the browser will
    # re-capture as the new page mounts). Best-effort; never block the save.
    try:
        _canvas_files.clear_canvas_logs(user_id, slug)
    except Exception:
        pass
    return saved


# ── Per-canvas console log (filesystem-local; see common.py) ───────────────────
# The page's own console output, captured in the browser and POSTed back, kept in
# a file beside index.html — a page-scoped debug log the design agent reads WITHOUT
# any logs.db / codebase-admin access. These thin wrappers keep the canvas API
# routes + the get_canvas_logs tool importing from one place.

def append_canvas_logs(user_id: str, slug: str, entries, replace_source: str = None) -> int:
    """Append console entries for a canvas (returns lines kept). `replace_source`
    first drops existing lines from that source (e.g. 'headless') before appending."""
    return _canvas_files.append_canvas_logs(user_id, slug, entries, replace_source=replace_source)


def read_canvas_logs(user_id: str, slug: str, limit: int = 100, level: str = None):
    """Return a canvas's most recent console entries (newest last), optional level filter."""
    return _canvas_files.read_canvas_logs(user_id, slug, limit=limit, level=level)


def clear_canvas_logs(user_id: str, slug: str) -> None:
    """Wipe a canvas's console log (also done automatically on each re-render)."""
    _canvas_files.clear_canvas_logs(user_id, slug)


async def create_canvas(
    user_id: str,
    slug: str,
    title: str,
    agent_context: str = "",
    initial_html: str = "",
    agent_id: str = "",
) -> Dict:
    return await get_canvases_store().create_canvas(
        user_id=user_id,
        slug=slug,
        title=title,
        agent_context=agent_context,
        initial_html=initial_html,
        agent_id=agent_id,
    )


async def delete_canvas(user_id: str, slug: str) -> bool:
    return await get_canvases_store().delete_canvas(user_id, slug)


async def rename_canvas(user_id: str, slug: str, new_title: str) -> bool:
    return await get_canvases_store().rename_canvas(user_id, slug, new_title)


async def ensure_home_canvas(user_id: str) -> None:
    await get_canvases_store().ensure_home_canvas(user_id, home_canvas_html())

"""
render_visual tool -- writes HTML to a per-user named canvas and returns the path for the frontend iframe.
"""
import json
from app.visualizer.canvas import save_canvas_html, ensure_home_canvas


def _looks_complete(html: str) -> bool:
    """True when the string looks like a whole HTML document, not a truncated stream.

    The recurring failure this guards against: the model streams a large canvas in
    one tool call, the call is cut short (user interrupt, length cap, a dropped
    chunk), and a half-written document arrives here. Saving it would let the agent
    'succeed' on a broken canvas and then claim it shipped. A complete document
    always ends with a closing </html>; a truncated one almost never does.
    """
    low = html.lower()
    return ("<html" in low or "<!doctype" in low) and "</html>" in low


async def render_visual(
    html: str,
    title: str = "",
    slug: str = "home",
    user_id: str = "default",
    canvas_name: str = None,
    agent_id: str = "",
) -> str:
    """
    Save HTML to the named canvas for this user and return metadata.

    Args:
        html:     Full HTML document string
        title:    Human-readable title for the canvas
        slug:     Canvas slug to write to (e.g. "home", "dashboard")
        user_id:  User identifier for per-user canvas isolation
        canvas_name: Alias for ``slug`` — a model that names the canvas with
                   ``canvas_name`` instead of ``slug`` still writes to the right
                   canvas. If given and ``slug`` is the default, it wins, so a
                   stray name never silently dumps onto ``home``.
        agent_id: The agent doing the render — recorded as the canvas's owning
                   agent so the Canvas footer can name it and route its chat.

    Returns:
        JSON string with path, title, slug, size_bytes, complete
    """
    # Accept ``canvas_name`` as an alias for ``slug`` so a call that names the
    # canvas the other way still lands on the intended canvas rather than
    # defaulting to home.
    if canvas_name and (not slug or slug == "home"):
        slug = canvas_name

    if not html or not html.strip():
        return json.dumps({
            "status": "error",
            "message": "Empty HTML rejected. Put the COMPLETE HTML document string in the `html` parameter. The canvas was NOT updated.",
        })

    if not _looks_complete(html):
        return json.dumps({
            "status": "error",
            "message": (
                "The HTML looks truncated — it has no closing </html>, so it is "
                "almost certainly a partial/cut-off document. NOTHING was saved; "
                "the canvas is unchanged. Re-send the ENTIRE document from "
                "<!DOCTYPE html> through </html> in a single render_visual call."
            ),
            "saved": False,
            "slug": slug,
        })

    await ensure_home_canvas(user_id)
    url_path = await save_canvas_html(user_id, slug, html, title=title, agent_id=agent_id)
    size = len(html.encode("utf-8"))
    result = {
        "status": "ok",
        "saved": True,
        "complete": True,
        "path": url_path,
        "title": title or slug.capitalize(),
        "slug": slug,
        "size_bytes": size,
        "note": (
            "Saved to canvas '{}'. The canvas is updated ONLY because this call "
            "returned status ok — never tell the user it is done unless you saw "
            "this result. To double-check, read it back with get_canvas('{}')."
        ).format(slug, slug),
    }
    # A finished dashboard/page is rarely this small; flag likely stubs so the
    # agent doesn't mistake a placeholder for a delivered design.
    if size < 900:
        result["warning"] = (
            "This document is very small ({} bytes) — that is a stub, not a "
            "finished design. If you meant to ship a full canvas, it was likely "
            "truncated; re-render the complete document."
        ).format(size)
    return json.dumps(result)

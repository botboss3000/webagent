"""
render_visual tool -- writes HTML to a per-user named page and returns the path for the frontend iframe.
"""
import json
from app.visualizer.pages import save_page_html, ensure_home_page


async def render_visual(
    html: str,
    title: str = "",
    page_name: str = "home",
    user_id: str = "default",
) -> str:
    """
    Save HTML to the named page for this user and return metadata.

    Args:
        html:       Full HTML document string
        title:      Human-readable title for the page
        page_name:  Page slug to write to (e.g. "home", "dashboard")
        user_id:    User identifier for per-user page isolation

    Returns:
        JSON string with path, title, page_name, size_bytes
    """
    ensure_home_page(user_id)
    url_path = save_page_html(user_id, page_name, html)
    result = {
        "status": "ok",
        "path": url_path,
        "title": title or page_name.capitalize(),
        "page_name": page_name,
        "size_bytes": len(html.encode("utf-8")),
    }
    return json.dumps(result)

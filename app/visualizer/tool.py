"""
render_visual tool — writes p5.js HTML to disk, returns path for frontend iframe.
"""
import json
import os
import time
from datetime import datetime, timezone


VISUALS_DIR = "visuals"


def _ensure_visuals_dir(session_id: str) -> str:
    """Create visuals/<session_id>/ directory, return absolute path."""
    session_dir = os.path.join(VISUALS_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)
    return session_dir


async def render_visual(html: str, title: str = "", session_id: str = "") -> str:
    """
    Save p5.js HTML to visuals/<session_id>/render.html and return metadata.

    Args:
        html: Full HTML document string with embedded p5.js sketch
        title: Human-readable title for the visualization
        session_id: Session identifier for isolation

    Returns:
        JSON string with path, title, timestamp
    """
    sid = session_id or "default"
    session_dir = _ensure_visuals_dir(sid)

    filepath = os.path.join(session_dir, "render.html")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    timestamp = datetime.now(timezone.utc).isoformat()

    result = {
        "status": "ok",
        "path": f"/visuals/{sid}/render.html",
        "title": title or "Untitled Sketch",
        "timestamp": timestamp,
        "size_bytes": len(html.encode("utf-8")),
    }

    return json.dumps(result)

"""
Plugin Pages API — serves the page prompt catalog and accepts page creation
requests that create a header tab + start a chat with the default agent.

The prompt catalog lives in plugins/page-prompts.json.
"""
import json
import logging
import os
import re
from typing import List, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PROMPTS_FILE = os.path.join(_APP_DIR, "plugins", "page-prompts.json")
_PLUGIN_PAGES_DIR = os.path.join(_APP_DIR, "plugins", "pages")


def _load_prompts() -> List[Dict]:
    """Load the page prompt catalog."""
    if not os.path.exists(_PROMPTS_FILE):
        return []
    try:
        with open(_PROMPTS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        items = []
        for key, entry in raw.items():
            items.append({
                "id": key,
                "label": entry.get("label", key),
                "icon": entry.get("icon", "file-text"),
                "default_title": entry.get("default_title", key),
                "prompt": entry.get("prompt", ""),
            })
        return items
    except Exception as e:
        logger.warning("Failed to load page-prompts.json: %s", e)
        return []


router = APIRouter(prefix="/api/v1/plugin-pages")


@router.get("/prompts")
async def list_prompts():
    """Return the catalog of available plugin page prompts."""
    return {"status": "ok", "prompts": _load_prompts()}


class CreatePluginPageRequest(BaseModel):
    prompt_id: str
    title: str


@router.post("/create")
async def create_plugin_page(body: CreatePluginPageRequest):
    """Create an HTML placeholder file for a plugin page and return its info.

    The frontend then creates the actual page via the existing /api/v1/pages
    endpoint (or the agent does via render_visual), and injects the header tab.
    This just ensures the slug is available and the file can be served.
    """
    prompts = _load_prompts()
    match = next((p for p in prompts if p["id"] == body.prompt_id), None)
    if not match:
        raise HTTPException(status_code=404, detail=f"Prompt '{body.prompt_id}' not found")

    # Generate a URL-safe slug
    slug = body.title.lower().strip()
    slug = re.sub(r"[^a-z0-9-]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        slug = body.prompt_id

    # Write a minimal placeholder HTML so /plugins/pages/{slug}.html can serve it
    os.makedirs(_PLUGIN_PAGES_DIR, exist_ok=True)
    placeholder_path = os.path.join(_PLUGIN_PAGES_DIR, f"{slug}.html")
    if not os.path.exists(placeholder_path):
        placeholder_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{body.title}</title>
<style>
  body {{ font-family: 'Inter', sans-serif; background: #0d0d1a; color: #c0caf5; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
  .msg {{ text-align: center; }}
  .msg h1 {{ color: #a9b1d6; font-size: 24px; }}
  .msg p {{ color: #565f89; }}
</style>
</head>
<body>
<div class="msg">
  <h1>{body.title}</h1>
  <p>Building… tell the agent what to create in the chat panel.</p>
</div>
</body>
</html>"""
        with open(placeholder_path, "w", encoding="utf-8") as f:
            f.write(placeholder_html)

    return {
        "status": "ok",
        "page": {
            "slug": slug,
            "title": body.title,
            "prompt_id": body.prompt_id,
            "prompt": match["prompt"],
            "icon": match["icon"],
        },
    }


@router.get("/pages")
async def list_plugin_pages():
    """List available plugin page HTML files (for the UI to discover)."""
    os.makedirs(_PLUGIN_PAGES_DIR, exist_ok=True)
    files = []
    for fname in os.listdir(_PLUGIN_PAGES_DIR):
        if fname.endswith(".html"):
            slug = fname[:-5]
            files.append({
                "slug": slug,
                "url": f"/plugins/pages/{fname}",
            })
    return {"status": "ok", "files": files}
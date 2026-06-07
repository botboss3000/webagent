"""Helpers shared by all PageStore implementations."""

import re

from app import user_workspace as _ws


def safe(name: str) -> str:
    """Sanitize a string for safe use as a directory / file / slug segment."""
    return re.sub(r"[^\w\-]", "_", name)


def user_pages_dir(user_id: str) -> str:
    """On-disk dir holding a user's AutoAgent pages.

    Lives in the user's data home: ``data/user_data/<user_id>/pages/`` (see
    app/user_workspace.py). Created if missing."""
    return str(_ws.user_dir(user_id, "pages"))


def page_url(user_id: str, slug: str) -> str:
    """The URL the iframe loads to render a page.

    Backend-agnostic: served by app/api/pages.py:GET /api/v1/pages/{uid}/{slug}/html
    which dispatches through the active PageStore."""
    return f"/api/v1/pages/{safe(user_id)}/{safe(slug)}/html"


def default_agent_context(title: str) -> str:
    return (
        f"You are the {title} page agent. Your role is to build and maintain this "
        f"page called '{title}'. When asked to create or update content, produce clean, "
        f"well-designed HTML that matches the dark webAgent aesthetic "
        f"(#0a0a0f background, #c0caf5 text, #7aa2f7 accents). Render functional, "
        f"interactive pages tailored to the purpose of '{title}'."
    )


def home_agent_context() -> str:
    return (
        "You are the webAgent home page agent. Your role is to maintain this "
        "informational page about webAgent — its features, getting started guide, "
        "and use cases. When asked to update or modify this page, produce clean, "
        "well-structured HTML that matches the dark webAgent aesthetic. The page "
        "serves as the main welcome and onboarding resource for users of the app."
    )


def blank_page_html(title: str) -> str:
    escaped = title.replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escaped}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; height: 100%; background: #0a0a0f; color: #c0caf5;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }}
  .empty-state {{ display: flex; flex-direction: column; align-items: center;
    justify-content: center; height: 100vh; gap: 12px; color: #565f89; }}
  .empty-state h2 {{ color: #7aa2f7; font-size: 22px; margin: 0; }}
  .empty-state p {{ font-size: 14px; margin: 0; }}
</style>
</head>
<body>
<div class="empty-state">
  <h2>{escaped}</h2>
  <p>This page is empty — send a prompt to build it.</p>
</div>
</body>
</html>"""

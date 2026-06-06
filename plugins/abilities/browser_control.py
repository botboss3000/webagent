"""Browser Control ability — SELF-CONTAINED drop-in.

Gates two tools, both with handlers in core:
  • browser_action — drive a headless Chromium (Playwright) browser. Wrapped to
    close over the run's user_id, with the browser session keyed to user_id (one
    persistent browser per user). This exact quirk (session_id == user_id) is
    preserved from the old loader wiring.
  • http_request — arbitrary outbound HTTP. Returned as-is from core.

Discovered generically by core via three optional module hooks:
  • FEATURE       — catalog + UI + which tool names it gates
  • build_tools() — its tool handlers (app/tools/loader.py injects them)
  • TOOL_SCHEMAS / DESTRUCTIVE — read AFTER build_tools by the loader

Imports stay LAZY (inside build_tools) so scanning FEATURE stays cheap.
"""

from __future__ import annotations

from typing import Optional

FEATURE = {
    "id": "browser_control",
    "display_name": "Browser Control",
    "category": "ability",
    "status": "beta",
    "summary": "headless Playwright browser + arbitrary HTTP.",
    "tools": ["browser_action", "http_request"],
    "group": "web",
    "icon": "mouse-pointer-2",
    "color": "#9ece6a",
    "description": "Lets the agent drive a headless Chromium browser and make HTTP requests. No credentials.",
    "simple": True,
    # Bundled skill: a load-on-demand how-to for driving the browser (the action
    # loop + the shared/private browser-session model). Body lives in the sibling
    # file browser_control.skill.md (found by convention). Handle is minted once
    # and frozen here so a loaded ability-skill keeps matching across restarts.
    "skill_mode": "selectable",
    "skill_handle": "browser_control_bz9k3p",
    "skill_summary": "How to drive a browser: the navigate/read/click/type/"
                     "screenshot loop, and the shared-vs-private browser-session "
                     "(tab) sharing gate. Load this before using browser_action.",
}


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: Optional[str] = None, enabled_providers=None,
                **_ctx):
    """Return {tool_name: handler} for browser_action + http_request.

    browser_action is wrapped to resolve the browser SESSION (a persistent,
    shareable tab) this agent may drive — the sharing gate. Without an explicit
    browser_session_id the agent uses (or auto-creates) its own shared tab;
    private/unlinked tabs are unreachable. http_request is returned straight from
    core.
    """
    from app.tools.browser import (
        browser_action as _core_browser_action,
        resolve_agent_session as _resolve_browser_session,
    )
    from app.tools.core_tools import http_request as _core_http_request

    async def _browser_action_wrapper(
        action: str,
        selector: Optional[str] = None,
        text: Optional[str] = None,
        url: Optional[str] = None,
        js: Optional[str] = None,
        timeout_ms: int = 5000,
        full_page: bool = True,
        browser_session_id: Optional[str] = None,
    ):
        try:
            bs_id = _resolve_browser_session(user_id, agent_id or "", browser_session_id)
        except PermissionError as _pe:
            return {"success": False, "error": str(_pe), "url": "", "title": ""}
        return await _core_browser_action(
            bs_id=bs_id,
            action=action,
            selector=selector,
            text=text,
            url=url,
            js=js,
            timeout_ms=timeout_ms,
            full_page=full_page,
        )

    return {
        "browser_action": _browser_action_wrapper,
        "http_request": _core_http_request,
    }


# Populated as literals (handlers come straight from core / a thin wrapper, so
# there are no core-exported schema constants to mirror here).
DESTRUCTIVE = set()

TOOL_SCHEMAS = {
    "browser_action": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["navigate", "click", "type", "get_text", "get_html", "screenshot", "wait", "evaluate", "title", "url", "close"],
                "description": "Browser action to perform",
            },
            "selector": {"type": "string", "description": "CSS selector (for click, type, get_text)"},
            "text": {"type": "string", "description": "Text to type (for type action)"},
            "url": {"type": "string", "description": "URL to navigate to (for navigate action)"},
            "js": {"type": "string", "description": "JavaScript code (for evaluate action)"},
            "timeout_ms": {"type": "integer", "description": "Wait timeout in ms (default 5000)", "default": 5000},
            "full_page": {"type": "boolean", "description": "Full page screenshot", "default": True},
            "browser_session_id": {"type": "string", "description": "Optional: the browser tab (browser session) to act on. Must be one shared with this agent. Omit to use the agent's own shared tab."},
        },
        "required": ["action"],
    },
    "http_request": {
        "type": "object",
        "properties": {
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
                "description": "HTTP method",
                "default": "GET",
            },
            "url": {"type": "string", "description": "Full URL including scheme (e.g. https://api.example.com/data)"},
            "headers": {
                "type": "object",
                "description": "Optional dict of HTTP headers",
                "additionalProperties": {"type": "string"},
                "default": {},
            },
            "body": {
                "type": "object",
                "description": "Request body (dict for JSON/form, string for text)",
                "default": {},
            },
            "body_type": {
                "type": "string",
                "enum": ["json", "form", "text"],
                "description": "How to encode body",
                "default": "json",
            },
            "timeout": {
                "type": "integer",
                "description": "Request timeout in seconds",
                "default": 30,
            },
        },
        "required": ["url"],
    },
}

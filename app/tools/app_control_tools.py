"""App-control tools — let an agent change what the user is looking at.

Gated by the ``app_control`` ability (Basic category). The single tool,
``set_app_view``, pushes a ``ui_command`` event down the session's live
WebSocket (via ``_emit_to_visualizers``) so the front-end rearranges the
viewer's own screen:

  * switch the main view (e.g. bring the live Browser to the front),
  * show or hide the chat panel,
  * resize the chat panel.

Non-destructive: it only moves panels around for the viewer and writes no
data, which is why it lives in the Basic group rather than Administrator.

The front-end handler for ``ui_command`` lives in ``ui/js/agentWs.js``; the
view names map to the main-tab ids defined in ``ui/js/tabs.js``.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Friendly view name (what the agent passes) → internal main-tab id used by
# ui/js/tabs.js. Both the friendly name and the raw tab id are accepted so the
# agent can't trip on the internal spelling.
_VIEW_ALIASES: Dict[str, str] = {
    "browser":     "web",
    "web":         "web",
    "pages":       "autoagent",
    "autoagent":   "autoagent",
    "agents":      "agents",
    "terminal":    "terminal",
    "account":     "account",
    "admin":       "admin-tools",
    "admin-tools": "admin-tools",
}

# The values offered to the agent in the tool schema (friendly names only).
_VIEW_CHOICES = ["browser", "pages", "agents", "terminal", "account", "admin"]

SET_APP_VIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "view": {
            "type": "string",
            "enum": _VIEW_CHOICES,
            "description": (
                "Which main view to bring to the front of the user's screen. "
                "'browser' = the live in-app browser (mirrors your headless "
                "Chromium), 'pages' = the Pages workspace, 'agents' = the agent "
                "manager, 'terminal' = the terminal, 'account' = the account "
                "page, 'admin' = the admin tools. Omit to leave the current "
                "view unchanged."
            ),
        },
        "show_chat": {
            "type": "boolean",
            "description": (
                "Show (true) or hide (false) the chat panel. Hiding it gives "
                "the main view the full width — e.g. hide chat while showing "
                "the browser. Omit to leave chat visibility unchanged."
            ),
        },
        "chat_width": {
            "type": "integer",
            "description": (
                "Width of the chat panel in pixels (minimum 280). Use to widen "
                "or narrow chat, e.g. to give the browser more room. Omit to "
                "leave the width unchanged."
            ),
        },
    },
    "required": [],
}


def build_app_control_tools(user_id: str, session_id: str) -> Dict[str, Callable]:
    """Return the app-control tool handlers, closed over the caller's session.

    `session_id` targets the right screen — the front-end filters ui_command
    events to the session the viewer is currently chatting with.
    """

    async def set_app_view(
        view: Optional[str] = None,
        show_chat: Optional[bool] = None,
        chat_width: Optional[int] = None,
    ) -> str:
        """Change what the user is looking at in the app window.

        Switch the main view (browser, pages, agents, terminal, account,
        admin), show or hide the chat panel, and/or resize the chat panel —
        all on the user's live screen. Use this to follow your narration with
        the matching view: e.g. when you start browsing, bring the browser to
        the front and hide chat so the user watches the page. All arguments
        are optional; pass only what you want to change. The change is applied
        silently.
        """
        tab: Optional[str] = None
        friendly: Optional[str] = None
        if view is not None:
            friendly = str(view).strip().lower()
            tab = _VIEW_ALIASES.get(friendly)
            if tab is None:
                return (
                    f"Unknown view '{view}'. Choose one of: "
                    + ", ".join(_VIEW_CHOICES)
                )

        if tab is None and show_chat is None and chat_width is None:
            return "Nothing to change — pass a view, show_chat, and/or chat_width."

        if not session_id:
            return (
                "Can't change the view: this run has no live UI session attached "
                "(it may be an event-triggered or background run)."
            )

        width: Optional[int] = None
        if chat_width is not None:
            try:
                width = max(280, int(chat_width))
            except (TypeError, ValueError):
                return "chat_width must be a whole number of pixels (minimum 280)."

        event = {
            "type": "ui_command",
            "action": "set_view",
            "view": tab,            # internal tab id, or None to leave as-is
            "show_chat": show_chat,  # bool or None
            "chat_width": width,     # int or None
        }
        try:
            from app.api.chat import _emit_to_visualizers
            await _emit_to_visualizers(session_id, event, user_id=user_id)
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("set_app_view emit failed: %s", e)
            return f"Couldn't reach the user's screen to change the view: {e}"

        parts = []
        if friendly is not None:
            parts.append(f"showing the {friendly} view")
        if show_chat is not None:
            parts.append("showing the chat panel" if show_chat else "hiding the chat panel")
        if width is not None:
            parts.append(f"setting the chat panel width to {width}px")
        return "Done — " + ", ".join(parts) + "."

    return {"set_app_view": set_app_view}

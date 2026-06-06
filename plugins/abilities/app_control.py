"""App Control ability — SELF-CONTAINED drop-in. See app/abilities/__init__.py
for the contract.

TIER-1 ability: the single ``set_app_view`` handler still lives in core
(``app/tools/app_control_tools.py``). This file only declares the ability and,
via ``build_tools``, LAZILY imports that core factory and returns its handler so
the generic loader block can register it. The tool's JSON schema is sourced
from the factory's ``SET_APP_VIEW_SCHEMA`` constant inside ``build_tools`` (so
it can never drift from core), mirrored into the module-level ``TOOL_SCHEMAS``
that the loader reads AFTER calling ``build_tools``.

Non-destructive: it only moves panels around for the viewer and writes no data,
so ``DESTRUCTIVE`` is empty.
"""

from __future__ import annotations

FEATURE = {
    "id": "app_control",
    "display_name": "App Control",
    "category": "ability",
    "status": "stable",
    "summary": "switch the main view + show/hide/resize the chat panel.",
    "tools": ["set_app_view"],
    "group": "core",
    "icon": "app-window",
    "color": "#73daca",
    "description": "Lets the agent change what you're looking at — switch the main view (Browser, Pages, Agents...), show or hide the chat panel, and resize it. On by default; switch off to remove it platform-wide.",
    "simple": True,
}

# Populated inside build_tools() from the core factory's schema constant so the
# loader (which reads these AFTER build_tools) always sees current values.
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()  # set_app_view only rearranges panels — nothing destructive.


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the app-control tools.

    Lazily imports the core factory + schema constant, closes the handler over
    the caller's user_id/session_id (the session id targets the right live
    screen via the visualizer WebSocket), and refreshes the module-level
    TOOL_SCHEMAS from the core constant so it can't drift.
    """
    from app.tools.app_control_tools import (
        build_app_control_tools,
        SET_APP_VIEW_SCHEMA,
    )

    handlers = build_app_control_tools(user_id, session_id)

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({name: SET_APP_VIEW_SCHEMA for name in handlers})

    return handlers

"""Automation ability — SELF-CONTAINED drop-in.

Gates the event-subscription tools: when an external source fires (Gmail
Pub/Sub, Graph subscription, Slack mention, Drive change, etc.), run a prompt —
the same kind of real-time trigger the Automation tab creates (a row in
``agent_event_subscriptions`` plus a provider-side watch).

The tool handlers live in the core factory ``app.tools.automation_tools``
(``build_automation_tools`` + ``AUTOMATION_TOOL_SCHEMAS`` + ``AUTOMATION_DESTRUCTIVE``).
``build_tools`` below wraps that factory and mirrors its schema/destructive
constants into the module-level ``TOOL_SCHEMAS`` / ``DESTRUCTIVE`` that the loader
reads AFTER the call, so they never drift. Imports stay lazy (inside
``build_tools``) so scanning FEATURE stays cheap.

Discovered generically by core (see app/abilities/__init__.py
"Self-contained abilities"): FEATURE for the catalog/UI/gated tool names,
build_tools for the handlers. Delete this file and the capability is gone.
"""

from __future__ import annotations

FEATURE = {
    "id": "automation",
    "display_name": "Automation",
    "category": "ability",
    "status": "beta",
    "summary": "scheduled tasks + event subscriptions.",
    # All five event-subscription tools ship via build_tools() below; each seeds
    # to "discoverable" on enable.
    "tools": [
        "list_event_sources", "list_delivery_channels", "event_subscribe",
        "list_event_subscriptions", "event_unsubscribe",
    ],
    "group": "core",
    "icon": "clock",
    "color": "#e0af68",
    "description": "Scheduled tasks and event-triggered jobs that can call integrations.",
    "simple": False,
}


# Populated from the core factory's constants inside build_tools(), so the loader
# (which reads these AFTER build_tools) always sees the authoritative schemas.
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx) -> dict:
    """Return {tool_name: handler} for the event-subscription tools, each closed
    over the caller's user_id / agent_id / session_id. Mirrors the core factory's
    schema/destructive constants into the module-level TOOL_SCHEMAS / DESTRUCTIVE."""
    from app.tools.automation_tools import (
        build_automation_tools,
        AUTOMATION_TOOL_SCHEMAS,
        AUTOMATION_DESTRUCTIVE,
    )

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update(AUTOMATION_TOOL_SCHEMAS)
    DESTRUCTIVE.clear()
    DESTRUCTIVE.update(AUTOMATION_DESTRUCTIVE)

    return build_automation_tools(user_id=user_id, agent_id=agent_id, session_id=session_id)

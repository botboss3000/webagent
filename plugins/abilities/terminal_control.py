"""Terminal Control ability — SELF-CONTAINED drop-in.

See app/abilities/__init__.py for the contract. This file declares the ability
(FEATURE) and exposes its tools via build_tools(...), which lazily wraps the
core factory app.tools.terminal_tools.build_terminal_tools and mirrors that
factory's schema/destructive constants into module-level TOOL_SCHEMAS /
DESTRUCTIVE so they can never drift.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


FEATURE = {
    "id": "terminal_control",
    "display_name": "Terminal Control",
    "category": "ability",
    "status": "experimental",
    "summary": "drive interactive terminal programs (effectively shell access).",
    "tools": ["terminal_open", "terminal_read", "terminal_send",
              "terminal_wait", "terminal_list", "terminal_close"],
    # NOTE: build_tools also returns `terminal_tunnel` (hands the terminal to the
    # USER). It is intentionally NOT listed here so it is NOT seeded as a
    # "discoverable" tool — it stays always-on when this ability is enabled.
    "group": "administrator",
    "icon": "terminal",
    "color": "#f7768e",
    "description": "Lets the agent open and drive interactive terminal programs (REPLs, CLIs) — effectively shell access, so grant per agent with care.",
    "simple": True,
    # Bundled skill: a load-on-demand how-to for driving interactive programs.
    # Body lives in the sibling file terminal_control.skill.md (found by
    # convention). Handle is minted once and frozen here.
    "skill_mode": "selectable",
    "skill_handle": "terminal_control_9x4k2p",
}


# Populated inside build_tools() from the core factory's constants (the loader
# reads these AFTER calling build_tools), so they never drift from the factory.
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the terminal-control tools.

    Lazily imports the core factory (keeps FEATURE scanning cheap), returns its
    handler dict verbatim, and refreshes module-level TOOL_SCHEMAS / DESTRUCTIVE
    from the factory's own constants. The factory also returns `terminal_tunnel`
    (hands the terminal to the user); it is returned here but left out of
    FEATURE["tools"] so it stays always-on rather than seeded discoverable.
    """
    from app.tools.terminal_tools import (
        build_terminal_tools,
        TERMINAL_TOOL_SCHEMAS,
        TERMINAL_DESTRUCTIVE,
    )

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update(TERMINAL_TOOL_SCHEMAS)
    DESTRUCTIVE.clear()
    DESTRUCTIVE.update(TERMINAL_DESTRUCTIVE)

    return build_terminal_tools(user_id, session_id)

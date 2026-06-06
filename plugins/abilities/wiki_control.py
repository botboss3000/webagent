"""Wiki Control ability — SELF-CONTAINED drop-in.

Gates the company-wide Wiki tools (read, search, create, edit, set-status,
delete, history, restore, backlinks). The handlers + their schemas live in the
core factory ``app.tools.wiki_tools`` (``build_wiki_tools`` /
``WIKI_TOOL_SCHEMAS`` / ``WIKI_DESTRUCTIVE``); this file is the discovery surface
that core's generic drop-in block calls.

It is discovered generically by core via two optional module hooks (see
app/abilities/__init__.py "Self-contained abilities"):
  • FEATURE                    — catalog + UI + which tool names it gates
  • build_tools(...)           — its tool handlers (app/tools/loader.py injects them)

build_tools imports the factory LAZILY (so scanning FEATURE stays cheap),
returns its handler dict, and populates module-level TOOL_SCHEMAS / DESTRUCTIVE
from the factory's constants so they never drift.
"""

from __future__ import annotations

FEATURE = {
    "id": "wiki_control",
    "display_name": "Wiki Control",
    "category": "ability",
    "status": "beta",
    "summary": "read/search/create/edit/delete company-wide Wiki entries.",
    # Exactly the names build_wiki_tools() returns (seeds each to "discoverable").
    "tools": ["wiki_search", "wiki_list", "wiki_get", "wiki_create",
              "wiki_update", "wiki_set_status", "wiki_delete", "wiki_history",
              "wiki_get_revision", "wiki_restore", "wiki_backlinks"],
    "group": "productivity",
    "icon": "book-open",
    "color": "#7dcfff",
    "description": "Lets the agent read, search, create, edit, and delete entries in the company-wide Wiki. Writes change shared data everyone sees, so grant per agent with care.",
    "simple": True,
}


# Populated by build_tools() from the factory constants (see below). The loader
# reads these AFTER calling build_tools, so filling them there avoids drift.
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **ctx):
    """Return {tool_name: handler} for the Wiki tools.

    Lazily imports the core factory, mirrors its schema/destructive constants
    into this module's TOOL_SCHEMAS / DESTRUCTIVE, and returns the handler dict.
    """
    from app.tools.wiki_tools import (
        build_wiki_tools,
        WIKI_TOOL_SCHEMAS,
        WIKI_DESTRUCTIVE,
    )

    handlers = build_wiki_tools(user_id)

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update(WIKI_TOOL_SCHEMAS)
    DESTRUCTIVE.clear()
    DESTRUCTIVE.update(WIKI_DESTRUCTIVE)

    return handlers

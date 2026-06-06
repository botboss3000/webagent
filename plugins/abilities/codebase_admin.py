"""Codebase Admin ability — SELF-CONTAINED drop-in.

The full privileged source suite (read/write/edit/delete files, run commands,
restart, git, search) plus a ``db_query`` context tool. Handlers live in the
shared admin library (``plugins/admin/source_tools.py`` via ``inject_source_tools``);
``db_query`` is lazily wrapped from ``app.tools.core_tools.db_query``. The shared
``plugins/admin/adapter.extract_injected`` runs the injector against a throwaway
dict and adapts its ToolInfo objects into the (handlers, schemas, destructive)
triple the generic drop-in loader contract wants, so schemas/flags never drift.

Discovered generically by core (see app/tools/loader.py "Self-contained ability
tools"): build_tools() returns its handlers and the loader reads the
module-level TOOL_SCHEMAS / DESTRUCTIVE populated below AFTER the call.
"""

from __future__ import annotations

FEATURE = {
    "id": "codebase_admin",
    "display_name": "Codebase Admin",
    "category": "ability",
    "status": "stable",
    "summary": "read/write/edit/delete source, run commands, restart — privileged.",
    # Exactly the names build_tools returns: the injected source suite + db_query.
    "tools": ["db_query", "read_source", "write_source", "edit_source",
              "patch_source", "delete_source", "search_source", "search_comments",
              "read_directory", "run_command", "run_python", "restart_server",
              "git_tool", "resolve_conflict", "commit_and_push"],
    "group": "administrator",
    "icon": "folder-cog",
    "color": "#bb9af7",
    "description": "Lets the agent read, write, edit, delete files, and run shell commands on the host. No credentials.",
    "simple": True,
    # Bundled skill: a load-on-demand "how to work on this codebase" guide. It
    # teaches the agent to read CLAUDE.md first, explore via the search_comments
    # tool, and mirror changes across the grep-able consistency markers. Body
    # lives in the sibling file codebase_admin.skill.md (found by convention).
    # Handle is minted once and frozen here. skill_summary is the catalog
    # "when to use it" line (kept separate from the tool `summary` above) — it
    # points the agent at CLAUDE.md before any codebase work.
    "skill_mode": "selectable",
    "skill_handle": "codebase_admin_src7",
    "skill_summary": ("Load this BEFORE any task that reads, edits, or extends this "
                      "repo's code. First step it gives you: read CLAUDE.md (the index), "
                      "then the matching docs/claude guide. Also: explore by comments with "
                      "search_comments, and mirror changes across the consistency markers."),
}


# Populated inside build_tools() from the injected ToolInfo objects + the
# db_query schema (kept here so the loader, which reads these AFTER calling
# build_tools, sees the right schemas/flags without them drifting).
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()


# db_query's schema, copied VERBATIM from app/tools/loader.py (the codebase_admin
# block that adds db_query). db_query is NOT destructive.
_DB_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "get", "insert", "update", "delete"],
            "description": "Action to perform: list, get, insert, update, or delete (delete clears content)",
        },
        "context_type": {"type": "string", "description": "Document type (agent, user, skills, tools, tasks, memory, project, jobs) — for list/insert actions"},
        "context_id": {"type": "string", "description": "Document ID — for get/update/delete actions"},
        "title": {"type": "string", "description": "Title — for insert action"},
        "content": {"type": "string", "description": "Content body — for insert/update actions"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags — for insert action"},
    },
    "required": ["action"],
}


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the full privileged source suite + db_query.

    The source suite comes from the shared admin library via the adapter; db_query
    is lazily wrapped from core. Imports stay LAZY so scanning FEATURE is cheap.
    """
    from plugins.admin.adapter import extract_injected
    from plugins.admin.source_tools import inject_source_tools

    handlers, schemas, destructive = extract_injected(inject_source_tools, user_id)

    # ── db_query: lazily wrapped from app.tools.core_tools.db_query. The wrapper
    # closure + schema are preserved VERBATIM from the loader's codebase_admin
    # block (it forwards context_type/context_id/title/tags + user_id into core).
    from typing import List, Optional
    from app.tools.core_tools import db_query as _core_db_query

    async def _db_query_wrapper(
        action: str,
        context_type: "Optional[str]" = None,
        context_id: "Optional[str]" = None,
        title: "Optional[str]" = None,
        content: "Optional[str]" = None,
        tags: "Optional[List[str]]" = None,
    ):
        return await _core_db_query(
            action=action,
            context_type=context_type,
            context_id=context_id,
            title=title,
            content=content,
            tags=tags,
            user_id=user_id,
        )

    handlers["db_query"] = _db_query_wrapper
    schemas["db_query"] = dict(_DB_QUERY_SCHEMA)
    # db_query is NOT destructive (per the card), so it is left out of `destructive`.

    # Publish schemas/flags for the loader to read AFTER this call.
    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update(schemas)
    DESTRUCTIVE.clear()
    DESTRUCTIVE.update(destructive)
    return handlers

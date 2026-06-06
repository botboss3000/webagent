"""Git Control ability — SELF-CONTAINED drop-in.

Structured git operations only (no shell access). Handlers live in the shared
admin library (``plugins/admin/source_tools.py`` via ``inject_git_tools``); the
shared ``plugins/admin/adapter.extract_injected`` runs the injector against a
throwaway dict and adapts its ToolInfo objects into the (handlers, schemas,
destructive) triple the generic drop-in loader contract wants, so schemas/flags
never drift.

Discovered generically by core (see app/tools/loader.py "Self-contained ability
tools"): build_tools() returns its handlers and the loader reads the
module-level TOOL_SCHEMAS / DESTRUCTIVE populated below AFTER the call.
"""

from __future__ import annotations

FEATURE = {
    "id": "git_control",
    "display_name": "Git Control",
    "category": "ability",
    "status": "stable",
    "summary": "GitHub source-control operations.",
    # Exactly what build_tools returns (the injected git subset).
    "tools": ["git_tool", "resolve_conflict", "commit_and_push"],
    "group": "administrator",
    "icon": "git-branch",
    "color": "#9ece6a",
    "description": "Version control only — status, diff, commit, push, branch, pull — without shell access. On by default.",
    "simple": True,
}


# Populated inside build_tools() from the injected ToolInfo objects (the loader
# reads these AFTER calling build_tools, so populating them there keeps them
# from drifting).
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the structured git tools.

    Imports stay LAZY so scanning FEATURE stays cheap.
    """
    from plugins.admin.adapter import extract_injected
    from plugins.admin.source_tools import inject_git_tools

    handlers, schemas, destructive = extract_injected(inject_git_tools, user_id)

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update(schemas)
    DESTRUCTIVE.clear()
    DESTRUCTIVE.update(destructive)
    return handlers

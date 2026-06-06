"""Codebase Admin ability — drop-in. See app/abilities/__init__.py for the contract."""

FEATURE = {
    "id": "codebase_admin",
    "display_name": "Codebase Admin",
    "category": "ability",
    "status": "stable",
    "summary": "read/write/edit/delete source, run commands, restart — privileged.",
    "tools": ["db_query", "read_source", "write_source", "edit_source",
              "delete_source", "resolve_conflict", "commit_and_push",
              "run_command", "restart_server", "search_comments"],
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

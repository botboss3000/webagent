"""Git Control ability — drop-in. See app/abilities/__init__.py for the contract."""

FEATURE = {
    "id": "git_control",
    "display_name": "Git Control",
    "category": "ability",
    "status": "stable",
    "summary": "GitHub source-control operations.",
    # No statically-gated tool names today (git tools resolve at runtime); the
    # ability still gates availability + renders its own row.
    "tools": [],
    "group": "administrator",
    "icon": "git-branch",
    "color": "#9ece6a",
    "description": "Version control only — status, diff, commit, push, branch, pull — without shell access. On by default.",
    "simple": True,
}

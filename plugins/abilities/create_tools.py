"""Create Tools ability — drop-in. See app/abilities/__init__.py for the contract."""

FEATURE = {
    "id": "create_tools",
    "display_name": "Create Tools",
    "category": "ability",
    "status": "beta",
    "summary": "define new DB-persisted tools at runtime.",
    "tools": ["create_tool"],
    "group": "basic",
    "icon": "wrench",
    "color": "#7dcfff",
    "description": "Lets the agent define new tools in the database. No credentials.",
    "simple": True,
}

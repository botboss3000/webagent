"""UI Admin ability — drop-in. See app/abilities/__init__.py for the contract."""

FEATURE = {
    "id": "ui_admin",
    "display_name": "UI Admin",
    "category": "ability",
    "status": "beta",
    "summary": "admin UI configuration tools.",
    "tools": [],
    "group": "core",
    "icon": "paintbrush",
    "color": "#7dcfff",
    "description": "Edits only front-end files (ui/ — CSS & HTML); never backend code or the shell. On by default.",
    "simple": True,
}

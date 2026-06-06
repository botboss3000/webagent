"""Diagnostics ability — drop-in. See app/abilities/__init__.py for the contract."""

FEATURE = {
    "id": "diagnostics",
    "display_name": "Diagnostics",
    "category": "ability",
    "status": "stable",
    "summary": "read the in-app flight-recorder.",
    "tools": ["read_diagnostics"],
    "group": "administrator",
    "icon": "activity",
    "color": "#e0af68",
    "description": "Lets the agent read the in-app flight recorder to diagnose the running app. On by default; switch off to remove it platform-wide.",
    "simple": True,
}

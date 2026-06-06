"""App Control ability — drop-in. See app/abilities/__init__.py for the contract."""

FEATURE = {
    "id": "app_control",
    "display_name": "App Control",
    "category": "ability",
    "status": "stable",
    "summary": "switch the main view + show/hide/resize the chat panel.",
    "tools": ["set_app_view"],
    "group": "basic",
    "icon": "app-window",
    "color": "#73daca",
    "description": "Lets the agent change what you're looking at — switch the main view (Browser, Pages, Agents...), show or hide the chat panel, and resize it. On by default; switch off to remove it platform-wide.",
    "simple": True,
}

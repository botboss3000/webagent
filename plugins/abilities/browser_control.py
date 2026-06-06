"""Browser Control ability — drop-in. See app/abilities/__init__.py for the contract."""

FEATURE = {
    "id": "browser_control",
    "display_name": "Browser Control",
    "category": "ability",
    "status": "beta",
    "summary": "headless Playwright browser + arbitrary HTTP.",
    "tools": ["browser_action", "http_request"],
    "group": "web",
    "icon": "mouse-pointer-2",
    "color": "#9ece6a",
    "description": "Lets the agent drive a headless Chromium browser and make HTTP requests. No credentials.",
    "simple": True,
}

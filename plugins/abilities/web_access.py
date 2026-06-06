"""Web Access ability — drop-in. See app/abilities/__init__.py for the contract."""

FEATURE = {
    "id": "web_access",
    "display_name": "Web Access",
    "category": "ability",
    "status": "stable",
    "summary": "web_search, weather, maps geocoding (free public APIs).",
    "tools": ["web_search", "maps_geocode", "get_weather"],
    "group": "web",
    "icon": "globe",
    "color": "#7aa2f7",
    "description": "Lets the agent search the web, look up weather, and geocode addresses. No API keys needed.",
    "simple": True,
}

"""Web Access ability — SELF-CONTAINED drop-in.

TIER-1 ability: all three handlers live in core. ``web_search`` and
``get_weather`` are in ``app/tools/core_tools.py``; ``maps_geocode`` is in
``app/tools/maps.py`` (its schema is the module-level ``maps.TOOL_PARAMETERS``).
This file only declares the capability (FEATURE) and exposes build_tools(...)
which lazily imports the core handlers and returns them directly — they take
their args directly, so no user_id closure is needed.

Discovered generically by core via three optional module hooks (see
app/abilities/__init__.py "Self-contained abilities"):
  • FEATURE       — catalog + UI + which tool names it gates
  • build_tools() — its tool handlers (app/tools/loader.py injects them)
  • TOOL_SCHEMAS / DESTRUCTIVE — read by the loader AFTER build_tools() runs

What it does: lets an agent search the web, look up weather, and geocode
addresses — lightweight web lookups that don't drive a real browser, using free
public APIs (no API keys needed).
"""

from __future__ import annotations

from typing import Optional

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


# Populated inside build_tools(): web_search/get_weather schemas are inlined
# below (copied VERBATIM from the loader blocks), maps_geocode's schema is pulled
# from the maps module so it never drifts. The loader reads TOOL_SCHEMAS AFTER
# calling build_tools().
TOOL_SCHEMAS: dict = {}

# All three are read-only public-API lookups (loader marked them
# destructive=False), so DESTRUCTIVE stays empty.
DESTRUCTIVE: set = set()


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: Optional[str] = None, enabled_providers=None,
                **_ctx):
    """Return {tool_name: handler} for the web-access tools.

    Lazily imports the three core handlers and returns them directly (they take
    their args directly — no user_id closure needed). Inlines the web_search /
    get_weather schemas and pulls maps_geocode's schema from the maps module,
    refreshing module-level TOOL_SCHEMAS so it can't drift.
    """
    from app.tools.core_tools import (
        web_search as _core_web_search,
        get_weather as _core_get_weather,
    )
    # Maps & geocoding (Nominatim — free, no API key)
    from app.tools.maps import (
        maps_geocode as _core_maps_geocode,
        TOOL_PARAMETERS as _MAPS_PARAMS,
    )

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({
        "web_search": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results to return (default 5, max 10)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        "maps_geocode": _MAPS_PARAMS,
        "get_weather": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "City name or 'lat,lon' coordinates (e.g. 'London', '40.71,-74.01')"},
                "units": {"type": "string", "enum": ["metric", "imperial"], "description": "metric = Celsius/km/h, imperial = Fahrenheit/mph", "default": "metric"},
            },
            "required": ["location"],
        },
    })

    return {
        "web_search": _core_web_search,
        "maps_geocode": _core_maps_geocode,
        "get_weather": _core_get_weather,
    }

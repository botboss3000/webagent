"""
Maps & geocoding tool.

Uses OpenStreetMap Nominatim (free, no API key) for forward/reverse geocoding,
and the haversine formula for great-circle distance between two points.

Gated by the "web_access" ability — same toggle that exposes web_search and
get_weather, since this is a lightweight location utility in the same family.
"""

import json
import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)

# Nominatim usage policy requires an identifying User-Agent and a rate cap of
# ~1 req/sec. The webagent backend is a single-tenant host so this is fine in
# practice; if usage grows the operator can swap in a paid provider.
_NOMINATIM_BASE = "https://nominatim.openstreetmap.org"
_USER_AGENT = "webagent/1.0 (+https://webagent.live)"
_EARTH_KM = 6371.0088


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_KM * math.asin(math.sqrt(a))


async def maps_geocode(
    action: str,
    location: Optional[str] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    lat2: Optional[float] = None,
    lon2: Optional[float] = None,
    limit: int = 5,
) -> str:
    """Forward geocode, reverse geocode, or compute distance between two points.

    action="geocode":  address/place name → list of {display_name, lat, lon, type}
    action="reverse":  (lat, lon) → {display_name, address parts}
    action="distance": (lat, lon) and (lat2, lon2) → {km, miles}
    """
    try:
        import httpx
    except Exception as e:
        return json.dumps({"status": "error", "message": f"httpx unavailable: {e}"})

    action = (action or "").lower().strip()

    if action == "distance":
        if lat is None or lon is None or lat2 is None or lon2 is None:
            return json.dumps({"status": "error", "message": "distance requires lat, lon, lat2, lon2"})
        km = _haversine_km(float(lat), float(lon), float(lat2), float(lon2))
        return json.dumps({"status": "ok", "km": round(km, 3), "miles": round(km * 0.621371, 3)})

    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if action == "geocode":
                if not location:
                    return json.dumps({"status": "error", "message": "geocode requires 'location'"})
                resp = await client.get(
                    f"{_NOMINATIM_BASE}/search",
                    params={"q": location, "format": "jsonv2", "limit": max(1, min(limit, 10))},
                    headers=headers,
                )
                if resp.status_code != 200:
                    return json.dumps({"status": "error", "message": f"Nominatim HTTP {resp.status_code}"})
                rows = resp.json() or []
                results = [
                    {
                        "display_name": r.get("display_name"),
                        "lat": float(r.get("lat")) if r.get("lat") else None,
                        "lon": float(r.get("lon")) if r.get("lon") else None,
                        "type": r.get("type"),
                        "category": r.get("category"),
                    }
                    for r in rows
                ]
                return json.dumps({"status": "ok", "query": location, "results": results, "count": len(results)})

            if action == "reverse":
                if lat is None or lon is None:
                    return json.dumps({"status": "error", "message": "reverse requires 'lat' and 'lon'"})
                resp = await client.get(
                    f"{_NOMINATIM_BASE}/reverse",
                    params={"lat": lat, "lon": lon, "format": "jsonv2"},
                    headers=headers,
                )
                if resp.status_code != 200:
                    return json.dumps({"status": "error", "message": f"Nominatim HTTP {resp.status_code}"})
                row = resp.json() or {}
                return json.dumps({
                    "status": "ok",
                    "display_name": row.get("display_name"),
                    "address": row.get("address") or {},
                    "lat": float(row.get("lat")) if row.get("lat") else None,
                    "lon": float(row.get("lon")) if row.get("lon") else None,
                })

            return json.dumps({"status": "error", "message": f"unknown action '{action}'. Use geocode, reverse, or distance."})
    except Exception as e:
        logger.exception("maps_geocode failed")
        return json.dumps({"status": "error", "message": str(e)})


TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["geocode", "reverse", "distance"],
            "description": "geocode = address→coords; reverse = coords→address; distance = great-circle km/miles between two points",
        },
        "location": {"type": "string", "description": "Address or place name (for geocode action)"},
        "lat": {"type": "number", "description": "Latitude (reverse and distance actions)"},
        "lon": {"type": "number", "description": "Longitude (reverse and distance actions)"},
        "lat2": {"type": "number", "description": "Second-point latitude (distance action)"},
        "lon2": {"type": "number", "description": "Second-point longitude (distance action)"},
        "limit": {"type": "integer", "description": "Max geocode matches (default 5, max 10)", "default": 5},
    },
    "required": ["action"],
}

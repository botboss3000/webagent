"""
Core bootstrap tools — always available from turn 1.

These are the agent's discovery and essential utilities.
All other tools are discovered via list_tools / search_tools.
"""

import json
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


# ── Tool discovery ────────────────────────────────────────────────────────────

async def list_tools(user_id: str, db=None) -> str:
    """
    List all available tools from the database.

    Returns tool names, descriptions, and parameter summaries.
    Use this to discover what tools are available beyond the core bootstrap set.
    """
    try:
        from app.db import get_db
        if db is None:
            db = get_db()
        client = db.get_raw_client()
        rows = (
            client.table("tools")
            .select("name, description, status, language, created_at")
            .in_("created_by", [user_id, "__system__"])
            .eq("status", "active")
            .execute()
        )
        data = rows.data or []
        if not data:
            return json.dumps({"status": "ok", "tools": [], "count": 0})

        tools_list = []
        for r in data:
            tools_list.append({
                "name": r["name"],
                "description": r.get("description", ""),
                "language": r.get("language", "python"),
            })
        tools_list.sort(key=lambda x: x["name"])
        return json.dumps({"status": "ok", "tools": tools_list, "count": len(tools_list)})
    except Exception as e:
        logger.error("list_tools failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


async def search_tools(query: str, user_id: str, db=None) -> str:
    """
    Search available tools by name or description keyword.

    Use this when you need a specific capability but don't know the exact tool name.
    """
    try:
        from app.db import get_db
        if db is None:
            db = get_db()
        client = db.get_raw_client()
        q = query.lower()
        rows = (
            client.table("tools")
            .select("name, description, language, status")
            .in_("created_by", [user_id, "__system__"])
            .eq("status", "active")
            .execute()
        )
        data = rows.data or []
        matches = []
        for r in data:
            name = r.get("name", "")
            desc = r.get("description", "")
            if q in name.lower() or q in desc.lower():
                matches.append({
                    "name": name,
                    "description": desc,
                    "language": r.get("language", "python"),
                })
        matches.sort(key=lambda x: x["name"])
        return json.dumps({
            "status": "ok",
            "query": query,
            "tools": matches,
            "count": len(matches),
        })
    except Exception as e:
        logger.error("search_tools failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


async def get_tool_definition(tool_name: str, user_id: str, db=None) -> str:
    """
    Get the full definition of a tool, including its JSON Schema parameters.

    Use this after discovering a tool via list_tools or search_tools
    to learn its exact parameters before calling it.
    """
    try:
        from app.db import get_db
        if db is None:
            db = get_db()
        client = db.get_raw_client()
        rows = (
            client.table("tools")
            .select("name, description, parameters, language, code")
            .in_("created_by", [user_id, "__system__"])
            .eq("name", tool_name)
            .eq("status", "active")
            .limit(1)
            .execute()
        )
        data = rows.data
        if not data:
            return json.dumps({
                "status": "error",
                "message": f"Tool '{tool_name}' not found or not active.",
            })
        tool = data[0]
        params = tool.get("parameters", {})
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except (json.JSONDecodeError, TypeError):
                params = {"type": "object", "properties": {}, "required": []}
        return json.dumps({
            "status": "ok",
            "tool": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": params,
                "language": tool.get("language", "python"),
            },
        })
    except Exception as e:
        logger.error("get_tool_definition failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


# ── On-demand skills ──────────────────────────────────────────────────────────

async def list_skills(agent_id: str = "", session_id: str = "", db=None) -> str:
    """
    List this agent's skills — named knowledge packs you can pull in on demand.

    Returns each skill's name, when-to-use description, mode (always_on or
    selectable), and whether it is already loaded in this conversation. Call
    `load_skill` with a skill's name to load its full step-by-step instructions.
    """
    try:
        from app.db import get_db
        from app.agent.skills import parse_agent_skills
        if db is None:
            db = get_db()
        agent = await db.get_agent_by_id(agent_id) if agent_id else None
        skills = parse_agent_skills(agent)
        active = set(await db.get_session_active_skills(session_id)) if session_id else set()
        out = []
        for s in skills:
            if not s.get("enabled", True):
                continue
            always = s.get("mode") == "always_on"
            out.append({
                "name": s["name"],
                "description": s.get("description", ""),
                "mode": s.get("mode", "selectable"),
                "loaded": always or s["name"] in active,
            })
        return json.dumps({"status": "ok", "count": len(out), "skills": out})
    except Exception as e:
        logger.error("list_skills failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


async def load_skill(name: str, agent_id: str = "", session_id: str = "", db=None) -> str:
    """
    Load a skill's full instructions into this conversation.

    Use this when the user's request matches a skill listed in the `[SKILLS]`
    section of your system prompt. Pass the skill's exact `name`. The returned
    instructions stay active for the rest of the conversation — load each skill
    only once.
    """
    try:
        from app.db import get_db
        from app.agent.skills import parse_agent_skills, loaded_result_text
        if db is None:
            db = get_db()
        agent = await db.get_agent_by_id(agent_id) if agent_id else None
        skills = parse_agent_skills(agent)
        target = (name or "").strip().lower()
        match = next((s for s in skills if s["name"].lower() == target), None)
        if not match:
            avail = [s["name"] for s in skills if s.get("enabled", True)]
            return json.dumps({
                "status": "error",
                "message": f"No skill named '{name}'. Available: "
                           f"{', '.join(avail) if avail else '(none)'}",
            })
        if not match.get("enabled", True):
            return json.dumps({"status": "error",
                               "message": f"Skill '{match['name']}' is disabled."})
        # Record it active for this session (drives the catalog + UI chips).
        if session_id:
            try:
                await db.set_session_active_skill(session_id, match["name"], True)
            except Exception as e:
                logger.debug("set_session_active_skill failed: %s", e)
        # The returned text IS the persisted tool result, so it replays into
        # context for the rest of the conversation. Its header line lets the
        # deactivate endpoint neutralize this row later.
        return loaded_result_text(match)
    except Exception as e:
        logger.error("load_skill failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


# ── Web search ────────────────────────────────────────────────────────────────

async def web_search(query: str, max_results: int = 5) -> str:
    """
    Search the web using DuckDuckGo.

    Returns titled search results with snippets and URLs.
    Use for finding current information, documentation, news, or any web content.
    No API key required.
    """
    import httpx
    import re
    from urllib.parse import quote_plus

    max_results = min(max_results, 10)
    url = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        response = await client.post(url, data={"q": query}, headers=headers)
        response.raise_for_status()
        html = response.text

    results = []
    link_pattern = re.compile(r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
    snippet_pattern = re.compile(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)

    links = link_pattern.findall(html)
    snippets = snippet_pattern.findall(html)

    for i in range(min(len(links), max_results)):
        href, title_html = links[i]
        title = re.sub(r'<[^>]+>', '', title_html).strip()
        snippet = ""
        if i < len(snippets):
            snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip()
        if href.startswith("//"):
            href = "https:" + href
        results.append(f"{i+1}. {title}\n   {snippet}\n   {href}")

    if not results:
        return f"No results found for '{query}'."

    return f"Search results for '{query}':\n\n" + "\n\n".join(results)


# ── DB query (prompt slots on the user's agent) ──────────────────────────────

async def db_query(
    action: str,
    slot_name: Optional[str] = None,
    content: Optional[str] = None,
    lock: Optional[bool] = None,
    merge_mode: Optional[str] = None,
    order_index: Optional[int] = None,
    user_id: str = "",
    **_legacy,
) -> str:
    """
    Read and edit the admin-base prompt slots on the user's assigned agent.

    Actions:
      list   → list all admin-base slots
      get    → get one slot by slot_name
      insert → create a new slot (slot_name + content required)
      update → replace a slot's content (slot_name + content required)
      delete → delete a slot by slot_name

    Lock + merge_mode + order_index are optional on insert/update.
    """
    try:
        from app.db import get_db
        db = get_db()
        agent = await db.get_agent_for_user(user_id)
        if not agent:
            return json.dumps({"status": "error", "message": "No agent assigned for this user."})
        agent_id = agent["id"]
        # Tolerate legacy param names from older callers.
        slot_name = slot_name or _legacy.get("context_id") or _legacy.get("context_type") or _legacy.get("title")

        if action == "list":
            slots = await db.list_slots(agent_id)
            return json.dumps({"status": "ok", "count": len(slots), "slots": slots}, default=str)

        elif action == "get":
            if not slot_name:
                return json.dumps({"status": "error", "message": "slot_name required for get action"})
            slots = await db.list_slots(agent_id)
            slot = next((s for s in slots if s["slot_name"] == slot_name), None)
            if not slot:
                return json.dumps({"status": "error", "message": f"Slot '{slot_name}' not found."})
            return json.dumps({"status": "ok", "slot": slot}, default=str)

        elif action in ("insert", "update"):
            if not slot_name or content is None:
                return json.dumps({"status": "error", "message": "slot_name and content required"})
            existing = await db.list_slots(agent_id)
            current = next((s for s in existing if s["slot_name"] == slot_name), None)
            resolved_order = order_index if order_index is not None else (
                current["order_index"] if current
                else (max((s["order_index"] or 0) for s in existing), 0)[0] + 10 if existing else 10
            )
            resolved_lock = lock if lock is not None else (current["lock"] if current else False)
            resolved_mode = merge_mode if merge_mode is not None else (current.get("merge_mode") if current else "replace")
            await db.upsert_slot(
                agent_id=agent_id,
                slot_name=slot_name,
                order_index=int(resolved_order),
                lock=bool(resolved_lock),
                merge_mode=resolved_mode,
                content=content,
                updated_by=f"tool:{user_id}",
            )
            return json.dumps({"status": "ok", "slot_name": slot_name})

        elif action == "delete":
            if not slot_name:
                return json.dumps({"status": "error", "message": "slot_name required for delete"})
            n = await db.delete_slot(agent_id, slot_name)
            return json.dumps({"status": "ok", "slot_name": slot_name, "deleted_rows": n})

        else:
            return json.dumps({"status": "error", "message": f"Unknown action '{action}'. Use: list, get, insert, update, delete."})

    except Exception as e:
        logger.error("db_query failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


# ── Memory (persistent knowledge pages) ──────────────────────────────────────

async def memory(
    action: str,
    slug: Optional[str] = None,
    query: Optional[str] = None,
    page_type: Optional[str] = None,
    title: Optional[str] = None,
    compiled_truth: Optional[str] = None,
    timeline: Optional[str] = None,
    limit: int = 10,
    user_id: str = "",
) -> str:
    """
    Manage persistent memory pages across sessions.

    Memory pages use a compiled-truth + timeline format. Each page has a
    unique slug. Pages persist across sessions and are searchable.

    Actions:
      search     → Search memory by keyword query
      list       → List all memory pages, optionally filtered by page_type
      get        → Get one memory page by slug
      upsert     → Create or update a memory page (requires slug, compiled_truth)
      delete     → Delete a memory page by slug

    Use this to remember important information about the user, project,
    preferences, and past decisions.
    """
    try:
        from app.db import get_db
        db = get_db()

        if action == "search":
            if not query:
                return json.dumps({"status": "error", "message": "query required for search action"})
            results = await db.memory_search(user_id, query, limit=limit)
            return json.dumps({
                "status": "ok",
                "query": query,
                "count": len(results or []),
                "results": results or [],
            })

        elif action == "list":
            results = await db.memory_list(user_id, page_type=page_type)
            return json.dumps({"status": "ok", "count": len(results or []), "pages": results or []})

        elif action == "get":
            if not slug:
                return json.dumps({"status": "error", "message": "slug required for get action"})
            result = await db.memory_get(user_id, slug)
            if not result:
                return json.dumps({"status": "error", "message": f"Memory page '{slug}' not found."})
            return json.dumps({"status": "ok", "page": result})

        elif action == "upsert":
            if not slug:
                return json.dumps({"status": "error", "message": "slug required for upsert action"})
            if not compiled_truth:
                return json.dumps({"status": "error", "message": "compiled_truth required for upsert action"})
            result = await db.memory_upsert(
                user_id, slug,
                page_type=page_type or "note",
                title=title or slug,
                compiled_truth=compiled_truth,
                timeline=timeline or "",
            )
            return json.dumps({"status": "ok", "slug": slug, "action": "upserted"})

        elif action == "delete":
            if not slug:
                return json.dumps({"status": "error", "message": "slug required for delete action"})
            ok = await db.memory_delete(user_id, slug)
            return json.dumps({"status": "ok" if ok else "error", "deleted": ok, "slug": slug})

        else:
            return json.dumps({
                "status": "error",
                "message": f"Unknown action '{action}'. Use: search, list, get, upsert, delete.",
            })

    except Exception as e:
        logger.error("memory tool failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


# ── Session search ───────────────────────────────────────────────────────────

async def session_search(
    query: str,
    limit: int = 10,
    user_id: str = "",
) -> str:
    """
    Search across past conversation sessions and messages.

    Finds user messages, assistant replies, and tool calls that match
    your query. Use this to recall past conversations, decisions, and
    context the user has shared.
    """
    try:
        from app.db import get_db
        db = get_db()
        q = query.lower()
        
        # Try Supabase client first
        raw = getattr(db, 'get_raw_client', None)
        if raw:
            try:
                client = raw()
                rows = (
                    client.table("interactions")
                    .select("id, session_id, role, content, tool_name, created_at")
                    .eq("user_id", user_id)
                    .limit(100)
                    .order("created_at", desc=True)
                    .execute()
                )
                data = rows.data or []
                matches = []
                for r in data:
                    content = r.get("content", "") or ""
                    tool_name = r.get("tool_name", "") or ""
                    if q in content.lower() or q in tool_name.lower():
                        matches.append({
                            "id": r.get("id"),
                            "session_id": r.get("session_id"),
                            "role": r.get("role"),
                            "tool_name": r.get("tool_name"),
                            "content": content[:500],
                            "created_at": r.get("created_at"),
                        })
                    if len(matches) >= limit:
                        break
                return json.dumps({"status": "ok", "query": query, "count": len(matches), "results": matches})
            except Exception as e:
                logger.debug("Supabase session_search failed, falling back to local: %s", e)
        
        # Fallback: the active local backend (SQLite or Postgres)
        from app.db import get_db
        conn = get_db()._get_conn()
        rows = conn.execute(
            """SELECT i.id, i.session_id, i.role, i.content, i.tool_name, i.created_at
               FROM interactions i
               JOIN sessions s ON i.session_id = s.id
               WHERE s.user_id = ? AND (LOWER(i.content) LIKE ? OR LOWER(COALESCE(i.tool_name, '')) LIKE ?)
               ORDER BY i.created_at DESC LIMIT ?""",
            (user_id, f"%{q}%", f"%{q}%", limit)
        ).fetchall()
        conn.close()
        matches = []
        for r in rows:
            matches.append({
                "id": r[0],
                "session_id": r[1],
                "role": r[2],
                "content": (r[3] or "")[:500],
                "tool_name": r[4],
                "created_at": r[5],
            })
        return json.dumps({"status": "ok", "query": query, "count": len(matches), "results": matches})
    except Exception as e:
        logger.error("session_search failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


# ── Time & Date utilities ─────────────────────────────────────────────────────

async def get_time(timezone: str = "UTC") -> str:
    """
    Get the current time in a specified timezone.

    Args:
        timezone: IANA timezone name (e.g. "America/New_York", "Europe/London",
                  "Asia/Tokyo"). Defaults to "UTC".

    Returns:
        Current time as a human-readable string.
    """
    from datetime import datetime
    if not timezone:
        timezone = "UTC"
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(timezone)
        now = datetime.now(tz)
        use_12h = now.strftime("%p") != ""
        return now.strftime("%I:%M:%S %p") if use_12h else now.strftime("%H:%M:%S")
    except Exception as e:
        return f"Error: could not resolve timezone '{timezone}'. Use IANA format e.g. 'America/New_York'. ({e})"


async def get_date(timezone: str = "UTC", format: str = "full") -> str:
    """
    Get the current date in a specified timezone.

    Args:
        timezone: IANA timezone name (e.g. "America/New_York"). Defaults to "UTC".
        format: "full" (Monday, May 8, 2026), "short" (2026-05-08), or "iso" (2026-05-08T...)

    Returns:
        Current date as a human-readable string.
    """
    from datetime import datetime
    if not timezone:
        timezone = "UTC"
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(timezone)
        now = datetime.now(tz)
        if format == "short":
            return now.strftime("%Y-%m-%d")
        elif format == "iso":
            return now.isoformat()
        else:
            return now.strftime("%A, %B %d, %Y")
    except Exception as e:
        return f"Error: could not resolve timezone '{timezone}'. Use IANA format e.g. 'America/New_York'. ({e})"


# ── Weather ───────────────────────────────────────────────────────────────────

async def get_weather(location: str, units: str = "metric") -> str:
    """
    Get current weather conditions for a location.

    Uses Open-Meteo (free, no API key required). Fetches temperature,
    conditions, wind speed, and humidity.

    Args:
        location: City name or "lat,lon" coordinates (e.g. "London", "New York", "40.71,-74.01")
        units: "metric" (Celsius, km/h) or "imperial" (Fahrenheit, mph)

    Returns:
        Formatted weather report for the location.
    """
    import httpx

    try:
        # Resolve location to coordinates
        lat, lon = await _resolve_location(location)

        # Fetch weather from Open-Meteo (free, no key)
        temp_unit = "celsius" if units == "metric" else "fahrenheit"
        wind_unit = "kmh" if units == "metric" else "mph"

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": ["temperature_2m", "relative_humidity_2m",
                                "apparent_temperature", "weather_code",
                                "wind_speed_10m", "wind_direction_10m"],
                    "temperature_unit": temp_unit,
                    "wind_speed_unit": wind_unit,
                    "timezone": "auto",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        current = data.get("current", {})
        temp = current.get("temperature_2m", "?")
        feels_like = current.get("apparent_temperature", "?")
        humidity = current.get("relative_humidity_2m", "?")
        wind_speed = current.get("wind_speed_10m", "?")
        weather_code = current.get("weather_code", 0)

        conditions = _weather_code_to_text(weather_code)
        temp_label = "C" if units == "metric" else "F"
        wind_label = "km/h" if units == "metric" else "mph"

        return (
            f"Weather in {location}: {conditions}\n"
            f"Temperature: {temp}°{temp_label} (feels like {feels_like}°{temp_label})\n"
            f"Humidity: {humidity}%\n"
            f"Wind: {wind_speed} {wind_label}"
        )

    except Exception as e:
        logger.warning("Open-Meteo failed for %s: %s", location, e)
        # Fallback: try wttr.in
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"https://wttr.in/{_encode_location(location)}?format=%C+%t+%w+%h"
                )
                resp.raise_for_status()
                text = resp.text.strip()
                if text:
                    return f"Weather in {location}: {text}"
        except Exception:
            pass
        return f"Could not fetch weather for '{location}'. Please try a different location name or use 'lat,lon' coordinates."


_WEATHER_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Foggy",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def _weather_code_to_text(code: int) -> str:
    return _WEATHER_CODES.get(code, f"Unknown ({code})")


def _encode_location(location: str) -> str:
    """Encode location for URL use."""
    import urllib.parse
    return urllib.parse.quote(location.replace(",", " "))


async def _resolve_location(location: str) -> tuple:
    """
    Resolve a location name to (lat, lon) coordinates.
    If already "lat,lon" format, parse directly.
    """
    # Check if already coordinates
    parts = location.split(",")
    if len(parts) == 2:
        try:
            lat = float(parts[0].strip())
            lon = float(parts[1].strip())
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return (lat, lon)
        except ValueError:
            pass

    # Geocode via Open-Meteo (free, no key)
    import httpx
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1, "language": "en", "format": "json"},
        )
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results")
    if not results:
        raise ValueError(f"Location '{location}' not found")

    return (results[0]["latitude"], results[0]["longitude"])


# ── Calculator ────────────────────────────────────────────────────────────────

import math as _math

_CALC_SAFE_GLOBALS = {
    "__builtins__": {},
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "pow": pow,
    "int": int,
    "float": float,
    "str": str,
    "len": len,
    "range": range,
    "True": True,
    "False": False,
    "None": None,
}

_CALC_SAFE_LOCALS = {
    "pi": _math.pi,
    "e": _math.e,
    "inf": _math.inf,
    "nan": _math.nan,
    "sin": _math.sin,
    "cos": _math.cos,
    "tan": _math.tan,
    "asin": _math.asin,
    "acos": _math.acos,
    "atan": _math.atan,
    "atan2": _math.atan2,
    "sqrt": _math.sqrt,
    "log": _math.log,
    "log10": _math.log10,
    "exp": _math.exp,
    "floor": _math.floor,
    "ceil": _math.ceil,
    "degrees": _math.degrees,
    "radians": _math.radians,
    "factorial": _math.factorial,
}


# ── HTTP Request tool ────────────────────────────────────────────────────────

async def http_request(
    method: str = "GET",
    url: str = "",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[Any] = None,
    body_type: str = "json",
    timeout: int = 30,
) -> str:
    """
    Make an outbound HTTP request to any endpoint.

    Supports GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS.
    Body can be JSON (auto-serialized), form data, or raw text.

    Args:
        method: HTTP method (GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS)
        url: Full URL including scheme (e.g. https://api.example.com/data)
        headers: Optional dict of HTTP headers
        body: Request body data (dict for JSON/form, string for raw text)
        body_type: "json", "form", "text" — how to encode body
        timeout: Request timeout in seconds (default 30)

    Returns:
        Formatted response with status code, headers, and body.
    """
    import httpx
    import json as _json
    import time

    method = method.upper()
    valid_methods = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
    if method not in valid_methods:
        return f"Error: invalid method '{method}'. Use one of: {', '.join(sorted(valid_methods))}"

    if not url:
        return "Error: url is required"

    if not url.startswith(("http://", "https://")):
        return "Error: url must start with http:// or https://"

    prepared_headers = {
        "User-Agent": "webAgent/1.0",
    }
    if headers:
        prepared_headers.update(headers)

    # Prepare body
    content = None
    if body is not None:
        if body_type == "json":
            if isinstance(body, str):
                try:
                    body = _json.loads(body)
                except _json.JSONDecodeError:
                    pass
            content = _json.dumps(body).encode()
            prepared_headers.setdefault("Content-Type", "application/json")
        elif body_type == "form":
            content = body  # httpx handles dicts directly for data= in POST
        elif body_type == "text":
            content = str(body).encode()
            prepared_headers.setdefault("Content-Type", "text/plain")

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            if method == "GET":
                resp = await client.get(url, headers=prepared_headers)
            elif method == "POST":
                if body_type == "form" and isinstance(body, dict):
                    resp = await client.post(url, headers=prepared_headers, data=body)
                else:
                    resp = await client.post(url, headers=prepared_headers, content=content)
            elif method == "PUT":
                resp = await client.put(url, headers=prepared_headers, content=content)
            elif method == "DELETE":
                resp = await client.delete(url, headers=prepared_headers, content=content)
            elif method == "PATCH":
                resp = await client.patch(url, headers=prepared_headers, content=content)
            elif method == "HEAD":
                resp = await client.head(url, headers=prepared_headers)
            elif method == "OPTIONS":
                resp = await client.options(url, headers=prepared_headers)
            else:
                return f"Error: unsupported method {method}"
    except httpx.TimeoutException:
        return f"Error: request to {url} timed out after {timeout}s"
    except httpx.ConnectError as e:
        return f"Error: could not connect to {url} — {e}"
    except Exception as e:
        return f"Error: request failed — {e}"

    duration_ms = int((time.time() - start) * 1000)

    # Truncate large response bodies
    body_text = resp.text
    if len(body_text) > 50000:
        body_text = body_text[:50000] + f"\n[... truncated, full size: {len(body_text)} bytes]"

    # Summarize headers (exclude long or binary ones)
    summary_headers = dict(resp.headers)
    for skip in ("set-cookie", "transfer-encoding", "content-encoding"):
        summary_headers.pop(skip, None)

    lines = [
        f"Status: {resp.status_code}",
        f"Duration: {duration_ms}ms",
        f"Content-Type: {resp.headers.get('content-type', '?')}",
        f"Content-Length: {len(resp.content)} bytes",
        "",
        "Body:",
        body_text,
    ]
    return "\n".join(lines)


async def calculate(expression: str) -> str:
    """
    Evaluate a mathematical expression.

    Supports: +, -, *, /, **, %, (), and functions like sin(), cos(), sqrt(),
    log(), abs(), round(), pi, e, factorial().

    Args:
        expression: A mathematical expression as a string (e.g. "2 + 2",
                   "sin(pi/4) * 100", "sqrt(144) + 3!")

    Returns:
        The result of the expression.
    """
    import traceback

    # Replace common notation: 3! → factorial(3)
    import re
    expr = re.sub(r"(\d+)!", r"factorial(\1)", expression)

    try:
        result = eval(expr, _CALC_SAFE_GLOBALS, _CALC_SAFE_LOCALS)
        # Format: round floats, avoid scientific notation for common sizes
        if isinstance(result, float):
            if abs(result) < 0.0001 or abs(result) > 1e12:
                formatted = f"{result:.6e}"
            else:
                formatted = f"{result:.6f}".rstrip("0").rstrip(".")
        else:
            formatted = str(result)
        return f"{expression} = {formatted}"
    except Exception as e:
        return f"Error evaluating '{expression}': {e}. Only basic math expressions are supported."

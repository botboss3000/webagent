"""Diagnostics ability — SELF-CONTAINED drop-in.

TIER-1 ability: the handler lives in core (app/tools/diagnostics_tool.py). This
file only declares the capability (FEATURE), and exposes build_tools(...) which
lazily imports the core handler + its parameter schema and returns the wrapped
handler closing over the injected user_id — exactly as the old loader block did.

Discovered generically by core via three optional module hooks (see
app/abilities/__init__.py "Self-contained abilities"):
  • FEATURE       — catalog + UI + which tool names it gates
  • build_tools() — its tool handlers (app/tools/loader.py injects them)
  • TOOL_SCHEMAS / DESTRUCTIVE — read by the loader AFTER build_tools() runs

What it does: lets an agent read the in-app flight recorder — recent server
errors/tracebacks, agent-loop pipeline problems, failed runs, tool metrics — so
it can diagnose the running app.
"""

from __future__ import annotations

from typing import Optional

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


# Populated inside build_tools() from the core module's TOOL_PARAMETERS so the
# schema never drifts. The loader reads TOOL_SCHEMAS AFTER calling build_tools().
TOOL_SCHEMAS: dict = {}

# read_diagnostics is a read-only flight-recorder query (loader marks it
# destructive=False), so DESTRUCTIVE stays empty.
DESTRUCTIVE: set = set()


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: Optional[str] = None, enabled_providers=None,
                **_ctx):
    """Return {tool_name: handler} for the diagnostics tool.

    Lazily imports the core handler + its parameter schema, wraps the handler to
    close over the injected user_id, and refreshes module-level TOOL_SCHEMAS from
    the core constant (so it can't drift).
    """
    from app.tools.diagnostics_tool import (
        read_diagnostics as _core_read_diagnostics,
        TOOL_PARAMETERS as _DIAG_PARAMS,
    )

    async def _read_diagnostics_wrapper(
        view: str = "diagnostics",
        levels: Optional[str] = None,
        categories: Optional[str] = None,
        tool_name: Optional[str] = None,
        session_id: Optional[str] = None,
        search: Optional[str] = None,
        since_minutes: Optional[float] = None,
        limit: int = 40,
    ):
        return await _core_read_diagnostics(
            user_id=user_id,
            view=view,
            levels=levels,
            categories=categories,
            tool_name=tool_name,
            session_id=session_id,
            search=search,
            since_minutes=since_minutes,
            limit=limit,
        )

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({"read_diagnostics": _DIAG_PARAMS})

    return {"read_diagnostics": _read_diagnostics_wrapper}

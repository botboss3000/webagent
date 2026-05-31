"""
`read_diagnostics` agent tool — lets a (suitably-privileged) agent read the
in-app flight-recorder so it can diagnose what is going wrong in the running
app: recent server errors + tracebacks, agent-loop pipeline problems, failed
runs, and tool errors.

Gated behind the pure-behavioral "diagnostics" ability (App Config → Agent
Abilities). The recorder itself lives in app/agent/diagnostics.py.
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


TOOL_PARAMETERS: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "levels": {
            "type": "string",
            "description": "Comma-separated severities to include: debug, info, warning, error, critical. "
                           "Default: warning,error,critical (the problems).",
        },
        "categories": {
            "type": "string",
            "description": "Comma-separated categories: server (log errors/tracebacks), loop "
                           "(agent-loop pipeline problems), run (run outcomes), tool (tool errors). "
                           "Default: all.",
        },
        "session_id": {
            "type": "string",
            "description": "Restrict to one chat session id (optional).",
        },
        "search": {
            "type": "string",
            "description": "Substring match against message / source / detail (optional).",
        },
        "since_minutes": {
            "type": "number",
            "description": "Only records newer than this many minutes (optional; default 120).",
        },
        "limit": {
            "type": "integer",
            "description": "Max records to return (default 40, max 200).",
        },
    },
    "required": [],
}


def _shrink_detail(detail: Any, cap: int = 900) -> Any:
    """Keep details useful but bounded — preserve tracebacks, trim the rest."""
    if detail is None:
        return None
    try:
        s = detail if isinstance(detail, str) else json.dumps(detail, default=str)
    except Exception:
        s = str(detail)
    if len(s) <= cap:
        return detail
    # For a traceback we care most about the tail (the actual exception line).
    return s[: cap - 200] + " …[truncated]… " + s[-200:]


async def read_diagnostics(
    user_id: str = "",
    levels: Optional[str] = None,
    categories: Optional[str] = None,
    session_id: Optional[str] = None,
    search: Optional[str] = None,
    since_minutes: Optional[float] = None,
    limit: int = 40,
    **_legacy,
) -> str:
    """Return recent diagnostic records as a JSON string (newest first)."""
    try:
        from app.agent.diagnostics import get_recorder
        rec = get_recorder()

        def _csv(v):
            if not v:
                return None
            parts = [p.strip().lower() for p in str(v).split(",") if p.strip()]
            return parts or None

        lvls = _csv(levels) or ["warning", "error", "critical"]
        cats = _csv(categories)
        try:
            lim = max(1, min(int(limit or 40), 200))
        except Exception:
            lim = 40
        since = since_minutes if since_minutes is not None else 120.0

        records: List[Dict[str, Any]] = await rec.query(
            levels=lvls,
            categories=cats,
            session_id=session_id,
            search=search,
            since_minutes=since,
            limit=lim,
        )

        compact = [{
            "ts": r.get("ts"),
            "level": r.get("level"),
            "category": r.get("category"),
            "source": r.get("source"),
            "message": r.get("message"),
            "session_id": r.get("session_id"),
            "agent_id": r.get("agent_id"),
            "detail": _shrink_detail(r.get("detail")),
        } for r in records]

        out = {
            "status": "ok",
            "count": len(compact),
            "filters": {
                "levels": lvls, "categories": cats, "session_id": session_id,
                "search": search, "since_minutes": since, "limit": lim,
            },
            "stats": rec.stats(),
            "records": compact,
        }
        if not compact:
            out["hint"] = ("No matching diagnostics. Widen the window (since_minutes), include more "
                           "levels (e.g. levels='info,warning,error'), or drop the category filter.")
        return json.dumps(out, default=str)
    except Exception as e:
        logger.warning("read_diagnostics failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})

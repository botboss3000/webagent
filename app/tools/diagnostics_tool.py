"""
`read_diagnostics` agent tool — lets a (suitably-privileged) agent read the
in-app flight-recorder so it can diagnose what is going wrong in the running
app: server logs + tracebacks, agent-loop pipeline problems, failed runs, tool
errors, every HTTP request (access) and WebSocket lifecycle (ws). Each record
carries correlation keys (interaction_id / session_seq / turn_id) so it can be
tied back to the exact chat interaction, plus the instance_id of the box.

`view='tools'` switches to aggregated tool-execution metrics (per-tool call
count, failure rate, avg/max duration, recent failures) from the structured
tool_executions table.

The flight-recorder writes to the dedicated, always-local logs database
(app/db/logs.db + recordings.db — see app/db/logs_store.py), separate from the
main app DB. Gated behind the pure-behavioral "diagnostics" ability (App Config
→ Agent Abilities). The recorder itself lives in app/agent/diagnostics.py.
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
        "view": {
            "type": "string",
            "description": "What to read: 'diagnostics' (default — the server log feed) or "
                           "'tools' (aggregated tool-execution metrics: per-tool call count, "
                           "failure count, success rate, avg/max duration, plus recent failures, "
                           "from the tool_executions table). Use 'tools' for 'which tool is slow / "
                           "failing'.",
        },
        "categories": {
            "type": "string",
            "description": "Comma-separated categories: server (log errors/tracebacks), http "
                           "(4xx/5xx HTTP responses + their cause), access (every HTTP request — "
                           "method/path/status/duration), ws (WebSocket connect/subscribe/"
                           "disconnect), loop (agent-loop pipeline problems), run (run outcomes), "
                           "recovery (self-recovery: stream stalls, frozen/zombie detections, "
                           "resume attempts + outcomes), tool (tool errors). Default: all.",
        },
        "tool_name": {
            "type": "string",
            "description": "With view='tools', restrict metrics to one tool (optional).",
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


def _iso_minus(minutes: float) -> Optional[str]:
    try:
        from datetime import datetime, timedelta, timezone
        return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    except Exception:
        return None


async def _tool_metrics(tool_name: Optional[str], session_id: Optional[str],
                        since_minutes: float, limit: int) -> str:
    """Aggregate the tool_executions table into per-tool stats + recent failures."""
    from app.db.logs_store import get_log_store
    store = get_log_store()
    rows = await store.query_tool_executions(
        tool_name=tool_name or None, session_id=session_id or None,
        since=_iso_minus(since_minutes), limit=max(limit, 500),
    )
    by_tool: Dict[str, Dict[str, Any]] = {}
    failures: List[Dict[str, Any]] = []
    for r in rows:
        name = r.get("tool_name") or "tool"
        s = by_tool.setdefault(name, {"tool": name, "calls": 0, "failures": 0,
                                      "_dur_sum": 0, "_dur_n": 0, "max_ms": 0})
        s["calls"] += 1
        ok = bool(r.get("success"))
        if not ok:
            s["failures"] += 1
            if len(failures) < 20:
                failures.append({"ts": r.get("ts"), "tool": name,
                                 "error": (r.get("error_message") or "")[:200],
                                 "session_id": r.get("session_id"),
                                 "interaction_id": r.get("interaction_id")})
        d = r.get("duration_ms")
        if isinstance(d, (int, float)):
            s["_dur_sum"] += d
            s["_dur_n"] += 1
            s["max_ms"] = max(s["max_ms"], int(d))
    tools = []
    for s in by_tool.values():
        n = s.pop("_dur_n") or 0
        dur_sum = s.pop("_dur_sum") or 0
        s["avg_ms"] = round(dur_sum / n) if n else None
        s["success_rate"] = round((s["calls"] - s["failures"]) / s["calls"], 3) if s["calls"] else None
        tools.append(s)
    tools.sort(key=lambda x: (x["failures"], x.get("max_ms") or 0), reverse=True)
    out = {
        "status": "ok", "view": "tools",
        "window_minutes": since_minutes, "total_calls": len(rows),
        "tools": tools, "recent_failures": failures,
    }
    if not rows:
        out["hint"] = "No tool executions in this window — widen since_minutes, or no tools have run yet."
    return json.dumps(out, default=str)


async def read_diagnostics(
    user_id: str = "",
    view: str = "diagnostics",
    levels: Optional[str] = None,
    categories: Optional[str] = None,
    tool_name: Optional[str] = None,
    session_id: Optional[str] = None,
    search: Optional[str] = None,
    since_minutes: Optional[float] = None,
    limit: int = 40,
    **_legacy,
) -> str:
    """Return recent diagnostic records (or tool metrics) as a JSON string."""
    try:
        try:
            lim = max(1, min(int(limit or 40), 200))
        except Exception:
            lim = 40
        since = since_minutes if since_minutes is not None else 120.0

        if (view or "").strip().lower() in ("tools", "tool", "tool_metrics", "metrics"):
            return await _tool_metrics(tool_name, session_id, since, lim)

        from app.agent.diagnostics import get_recorder
        rec = get_recorder()

        def _csv(v):
            if not v:
                return None
            parts = [p.strip().lower() for p in str(v).split(",") if p.strip()]
            return parts or None

        lvls = _csv(levels) or ["warning", "error", "critical"]
        cats = _csv(categories)

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
            "turn_id": r.get("turn_id"),
            "interaction_id": r.get("interaction_id"),
            "session_seq": r.get("session_seq"),
            "agent_id": r.get("agent_id"),
            "instance_id": r.get("instance_id"),
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
                           "levels (e.g. levels='info,warning,error'), drop the category filter, or "
                           "try view='tools' for tool timing/failure metrics.")
        return json.dumps(out, default=str)
    except Exception as e:
        logger.warning("read_diagnostics failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})

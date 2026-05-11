"""
Prefilter — extracts current session data AND skill state for the Judge.
No modifiable elements list (that's in the Planner's prompt).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from app.db import get_db

logger = logging.getLogger(__name__)


async def prefilter(user_id: str, session_id: str) -> Dict[str, Any]:
    """
    Pull current session transcript and tool errors.
    Skill stats, priority scoring, and modifiable elements are NOT included —
    the Planner's system prompt handles those decisions.
    """
    db = get_db()

    try:
        raw = getattr(db, "_get_conn", None)
        if not raw:
            return {"transcript": [], "opportunities": []}
        conn = raw()

        interactions = conn.execute(
            """SELECT role, tool_name, substr(content, 1, 300), created_at
               FROM interactions
               WHERE session_id = ?
               ORDER BY created_at
               LIMIT 50""",
            (session_id,),
        ).fetchall()

        transcript = []
        tools_used: Dict[str, int] = {}
        tool_errors: Dict[str, int] = {}

        for role, tool, content, ts in interactions:
            prefix = f"[{role}]"
            if tool:
                prefix += f" (tool:{tool})"
                if tool not in ("memory_search", "memory_save",
                                "list_tools", "search_tools",
                                "get_tool_definition", "create_tool",
                                "rate_skill", "run_optimizer",
                                "read_attachment"):
                    tools_used[tool] = tools_used.get(tool, 0) + 1
                    if any(w in content.lower() for w in
                           ("error", "failed", "could not", "unable", "exception")):
                        tool_errors[tool] = tool_errors.get(tool, 0) + 1
            transcript.append(f"{prefix} {content[:250]}")

        # Build simple opportunities list — just what errored in this session
        opportunities = []
        for tool_name, err_count in sorted(tool_errors.items(), key=lambda x: -x[1]):
            total = tools_used.get(tool_name, 0)
            if total > 0:
                opportunities.append({
                    "skill_name": tool_name,
                    "issue_desc": f"{err_count}/{total} calls errored in this session",
                })

        # Flag high turn count
        assistant_turns = [t for t in transcript if "[assistant]" in t and "(tool:" not in t]
        if len(assistant_turns) > 5:
            opportunities.insert(0, {
                "skill_name": "agent_behavior",
                "issue_desc": f"Session took {len(assistant_turns)} assistant turns — too many round trips",
            })

        # ALWAYS include the session — even if no tool errors, user may want to optimize
        # response quality, tone, verbosity, or any subjective concern
        if not opportunities:
            opportunities.append({
                "skill_name": "interaction_quality",
                "issue_desc": "No tool errors detected. Ask the user what they want to improve — response quality, tone, verbosity, format, or other concerns.",
            })

        conn.close()
        return {
            "transcript": transcript,
            "opportunities": opportunities[:5],
            "skill_state": _get_skill_state(user_id),
        }

    except Exception as e:
        logger.warning("Prefilter: error: %s", e)
        return {"transcript": [], "opportunities": [], "skill_state": []}


def _get_skill_state(user_id: str) -> List[Dict[str, Any]]:
    """Get current state of all skills — failure rates, execution counts."""
    from app.db import get_db
    db = get_db()
    raw = getattr(db, '_get_conn', None)
    if not raw:
        return []
    conn = raw()
    rows = conn.execute("""
        SELECT s.name, s.description,
               COUNT(se.id) as exec_count,
               SUM(CASE WHEN se.success=1 THEN 1 ELSE 0 END) as ok_count
        FROM skills s
        LEFT JOIN skill_executions se ON se.skill_id = s.id AND se.user_id = ?
        WHERE s.user_id = ? AND s.is_active = 1
        GROUP BY s.id
    """, (user_id, user_id)).fetchall()
    conn.close()
    state = []
    for name, desc, total, ok in rows:
        total, ok = total or 0, ok or 0
        fail_pct = round((1 - ok / total) * 100, 1) if total > 0 else 0
        state.append({
            "skill": name,
            "description": desc or "",
            "executions": total,
            "failures_pct": fail_pct,
            "needs_attention": fail_pct > 5 or total == 0,
        })
    return state

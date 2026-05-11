"""
Prefilter — extracts raw session stats. No skills. No decisions. Just data.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from app.db import get_db

logger = logging.getLogger(__name__)


async def prefilter(user_id: str, session_id: str) -> Dict[str, Any]:
    """Pull raw session data: transcript, turns, tokens. No skill scanning."""
    try:
        raw = getattr(get_db(), "_get_conn", None)
        if not raw:
            return {"transcript": [], "turns": 0, "tokens": 0, "time_ms": 5000}
        conn = raw()

        interactions = conn.execute(
            """SELECT role, tool_name, substr(content, 1, 300), created_at
               FROM interactions WHERE session_id = ? ORDER BY created_at LIMIT 50""",
            (session_id,),
        ).fetchall()

        transcript = []
        assistant_turns = 0
        token_estimate = 0

        for role, tool, content, ts in interactions:
            prefix = f"[{role}]"
            if tool:
                prefix += f" (tool:{tool})"
            transcript.append(f"{prefix} {content[:250]}")
            if role == "assistant" and "(tool:" not in transcript[-1]:
                assistant_turns += 1
            token_estimate += len(content) // 3

        conn.close()
        return {
            "transcript": transcript,
            "turns": max(assistant_turns, 1),
            "tokens": max(token_estimate, 50),
            "time_ms": 5000,
        }

    except Exception as e:
        logger.warning("Prefilter: error: %s", e)
        return {"transcript": [], "turns": 0, "tokens": 0, "time_ms": 5000}

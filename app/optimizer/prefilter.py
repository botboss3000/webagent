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

        # Full transcript: no content truncation
        full_interactions = conn.execute(
            """SELECT role, tool_name, content, created_at
               FROM interactions WHERE session_id = ? ORDER BY created_at""",
            (session_id,),
        ).fetchall()
        full_transcript = []
        original_message = ""
        tool_names_used = set()
        for role, tool, content, ts in full_interactions:
            prefix = f"[{role}]"
            if tool:
                prefix += f" (tool:{tool})"
                tool_names_used.add(tool)
            full_transcript.append(f"{prefix} {content}")
            if role == "user" and not original_message and tool is None:
                original_message = content[:500]

        # Look up tool definitions for tools used in this session
        tool_definitions = []
        if tool_names_used:
            placeholders = ",".join("?" for _ in tool_names_used)
            try:
                rows = conn.execute(
                    f"SELECT name, description FROM tools WHERE name IN ({placeholders})",
                    list(tool_names_used),
                ).fetchall()
                for name, desc in rows:
                    tool_definitions.append({"name": name, "description": desc[:200] if desc else "", "source": ""})
            except Exception:
                # tools table may have different schema in some DBs
                pass

        # Get context documents for this user's agent
        context_docs = []
        agent_row = conn.execute(
            "SELECT id FROM agents WHERE user_id=? LIMIT 1", (user_id,)
        ).fetchone()
        if agent_row:
            doc_rows = conn.execute(
                "SELECT context_type, title, substr(content, 1, 300) FROM context_documents WHERE agent_id=?",
                (agent_row[0],),
            ).fetchall()
            for ct, title, content_snip in doc_rows:
                context_docs.append({"type": ct, "title": title, "excerpt": content_snip})

        conn.close()
        return {
            "transcript": transcript,
            "full_transcript": full_transcript,
            "original_message": original_message,
            "tool_definitions": tool_definitions,
            "context_docs": context_docs,
            "turns": max(assistant_turns, 1),
            "tokens": max(token_estimate, 50),
            "time_ms": 5000,
        }

    except Exception as e:
        logger.warning("Prefilter: error: %s", e)
        return {"transcript": [], "turns": 0, "tokens": 0, "time_ms": 5000}

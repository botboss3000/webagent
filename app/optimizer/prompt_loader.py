"""
Shared prompt loader for optimizer agents.
Loads: DB (context_templates), then .md file, then hardcoded fallback.
"""

from __future__ import annotations

import os, logging
from typing import Dict

logger = logging.getLogger(__name__)

# ── Fallback prompts (used only if DB and .md file both unavailable) ──

FALLBACKS: Dict[str, str] = {
    "planner-prompt": "You are the Planner. You MUST output JSON with at least 1 change. Never ask questions. Do not return empty changes. Propose a trim or improvement to the greeting or system prompt. Return JSON now.",
    "worker-prompt": "You estimate the impact of a proposed change. Return ONLY JSON: {\"estimated_turns\": N, \"estimated_tokens\": N, \"estimated_time_ms\": N, \"success_likely\": true/false, \"confidence\": 0.0-1.0, \"reasoning\": \"brief\"}",
    "finalizer-prompt": "You are the Finalizer. Compare trial results against baseline and pick winners. Winner if improves >=10% on one criterion without degrading others >20%. Return JSON with winners, losers, summary.",
}


async def load_prompt(title: str) -> str:
    """Load prompt: DB → .md file → hardcoded fallback."""
    # 1. Try DB
    try:
        from app.db import get_db
        db = get_db()
        raw = getattr(db, '_get_conn', None)
        if raw:
            conn = raw()
            try:
                cur = conn.execute(
                    "SELECT content FROM context_templates WHERE context_type='optimizer' AND title=? LIMIT 1",
                    (title,),
                )
                if hasattr(cur, '__await__'):
                    cur = await cur
                row = cur.fetchone()
                if row:
                    return row[0] if isinstance(row, tuple) else row
            finally:
                if hasattr(conn, 'close'):
                    conn.close()
    except Exception as e:
        logger.debug("load_prompt DB failed for %s: %s", title, e)

    # 2. Try .md file
    name = title.replace("-prompt", "")
    md_path = os.path.join(os.path.dirname(__file__), "prompts", f"{name}.md")
    try:
        if os.path.exists(md_path):
            with open(md_path, encoding='utf-8') as f:
                return f.read()
    except Exception as e:
        logger.debug("load_prompt file failed for %s: %s", title, e)

    # 3. Hardcoded fallback
    return FALLBACKS.get(title, "")

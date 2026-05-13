"""
Shared prompt loader for optimizer agents.
Loads ONLY from DB (context_templates). No hardcoded fallbacks.
If no prompt found, returns empty string.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def load_prompt(title: str) -> str:
    """Load prompt from DB only. Returns empty string if not found."""
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
                return row[0] if row else ""
            finally:
                if hasattr(conn, 'close'):
                    conn.close()
    except Exception as e:
        logger.debug("load_prompt DB failed for %s: %s", title, e)
    return ""
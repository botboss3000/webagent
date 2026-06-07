"""Execute one scheduled automation row.

Creates a fresh session under the owner user, runs the agent loop with the
task's stored prompt, and dispatches the result via the configured channel
(or records silently). Updates ``last_run_at`` / ``last_status`` /
``next_run_at`` on the automation row.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_next_run(cron_expr: str, tz: str = "UTC") -> Optional[str]:
    try:
        from croniter import croniter
    except ImportError:
        return None
    try:
        from zoneinfo import ZoneInfo
        base = datetime.now(ZoneInfo(tz))
    except Exception:
        base = datetime.now(timezone.utc)
    try:
        nxt = croniter(cron_expr, base).get_next(datetime)
        return nxt.astimezone(timezone.utc).isoformat()
    except Exception as e:
        logger.warning("Cron recompute failed for %r tz %r: %s", cron_expr, tz, e)
        return None


async def _create_session(db, user_id: str, agent_id: str, label: str) -> str:
    session_id = f"auto-{uuid.uuid4().hex[:12]}"
    title = (label or "Scheduled task")[:60]
    try:
        raw = db.get_raw_client()
        raw.table("sessions").insert({
            "id": session_id,
            "user_id": user_id,
            "title": title,
            "agent_id": agent_id,
            "metadata": json.dumps({"source": "automation"}),
        }).execute()
    except Exception as e:
        logger.warning("Could not insert session via raw client (%s); using direct conn", e)
        try:
            conn = db._get_conn()
            try:
                conn.execute(
                    "INSERT INTO sessions (id, user_id, title, agent_id, metadata) VALUES (?, ?, ?, ?, ?)",
                    (session_id, user_id, title, agent_id, json.dumps({"source": "automation"})),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e2:
            logger.error("Session insert failed: %s", e2)
            raise
    return session_id


async def _deliver(
    channel: Optional[str],
    recipient: Optional[str],
    text: str,
) -> Optional[str]:
    if not channel or channel == "webchat" or channel == "silent":
        return None
    try:
        from app.communications.manager import get_plugin_manager
        pm = get_plugin_manager()
        plugin = pm.get_plugin(channel)
        if not plugin or not plugin.enabled:
            return f"channel {channel} not enabled"
        if not recipient:
            return f"channel {channel} has no recipient"
        await plugin.send_message(recipient, text)
        return None
    except Exception as e:
        logger.warning("Channel delivery failed (%s → %s): %s", channel, recipient, e)
        return str(e)


async def execute_automation(automation: Dict[str, Any]) -> Dict[str, Any]:
    """Run one scheduled automation row end-to-end.

    Thin wrapper over the shared run engine (``app.automation.runner.run_one``),
    which owns run-mode (inline / clone), unified delivery, guardrails, per-run
    history, retries/auto-disable, and one-shot (``schedule_kind='once'``) timers.
    Kept as the scheduler's entry point so the poll loop is unchanged.
    """
    from app.db import get_db
    from app.automation.runner import run_one

    db = get_db()
    kind = "timer" if (automation.get("schedule_kind") == "once") else "schedule"
    return await run_one(db, automation, kind=kind)

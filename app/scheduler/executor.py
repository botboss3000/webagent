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
    """Run one automation row end-to-end. Caller passes a fresh row dict.

    Returns a status dict ``{ok, session_id, error}``.
    """
    from app.db import get_db
    from app.agent.loop import run_agent_loop_buffered
    from app.agent.prompts import build_system_prompt

    db = get_db()
    automation_id = automation["id"]
    agent_id = automation["agent_id"]
    user_id = automation["owner_user_id"]
    prompt_text = automation.get("prompt") or ""
    channel = automation.get("channel")
    recipient = automation.get("channel_recipient")
    silent = bool(automation.get("silent"))
    label = automation.get("task_label") or "Scheduled task"

    await db.update_automation(automation_id, last_status="running", last_error=None)

    try:
        agent = await db.get_agent_by_id(agent_id)
        if not agent:
            raise RuntimeError(f"agent {agent_id} not found")

        session_id = await _create_session(db, user_id, agent_id, label)

        resolved_slots = await db.resolve_prompts(agent_id, user_id=user_id)
        context_docs = [
            {"id": s["slot_name"], "context_type": s["slot_name"],
             "title": s["slot_name"], "content": s["content"], "tags": []}
            for s in resolved_slots
            if (s.get("content") or "").strip() and s.get("slot_name") != "automation"
        ]
        system_prompt = await build_system_prompt(
            context_docs, brain_context=None, user_id=user_id, agent_id=agent_id,
        )

        # Persist the synthetic user message so the session has a starting turn.
        try:
            await db.insert_interaction(
                user_id, session_id, role="user", content=prompt_text,
                channel="automation",
                metadata=json.dumps({"source": "automation", "automation_id": automation_id}),
                sender_id=user_id, receiver_id=agent_id, source="automation",
            )
        except Exception as e:
            logger.debug("Could not insert synthetic user interaction: %s", e)

        raw_allowed = agent.get("allowed_tools", [])
        if isinstance(raw_allowed, str):
            try:
                raw_allowed = json.loads(raw_allowed)
            except Exception:
                raw_allowed = []

        reply = await run_agent_loop_buffered(
            user_id=user_id,
            session_id=session_id,
            user_message=prompt_text,
            system_prompt=system_prompt,
            agent_id=agent_id,
            history=None,
            channel="automation",
            timeout_seconds=600,
            db=db,
            agent_template_id=agent.get("template_id"),
            allowed_tools=raw_allowed or None,
            max_turns=agent.get("max_turn_count", 10),
        )

        delivery_err = None
        if not silent and channel and channel != "webchat":
            delivery_err = await _deliver(channel, recipient, reply or "")

        cron_expr = automation.get("schedule_cron") or ""
        tz = automation.get("timezone") or "UTC"
        next_run = _compute_next_run(cron_expr, tz)

        await db.update_automation(
            automation_id,
            last_status="ok" if not delivery_err else "error",
            last_error=delivery_err,
            last_run_at=_now_iso(),
            last_session_id=session_id,
            next_run_at=next_run,
        )
        return {"ok": True, "session_id": session_id, "error": delivery_err}

    except Exception as e:
        logger.exception("Automation %s failed: %s", automation_id, e)
        cron_expr = automation.get("schedule_cron") or ""
        tz = automation.get("timezone") or "UTC"
        next_run = _compute_next_run(cron_expr, tz)
        await db.update_automation(
            automation_id,
            last_status="error",
            last_error=str(e)[:500],
            last_run_at=_now_iso(),
            next_run_at=next_run,
        )
        return {"ok": False, "error": str(e)}

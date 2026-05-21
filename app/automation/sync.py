"""Diff parsed automation tasks against DB rows and notify the scheduler."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.automation.parser import ParsedTask, parse_automation_file

logger = logging.getLogger(__name__)


def _task_hash(t: ParsedTask) -> str:
    payload = json.dumps({
        "label": t.task_label,
        "prompt": t.prompt,
        "cron": t.schedule_cron,
        "tz": t.timezone,
        "channel": t.channel,
        "recipient": t.channel_recipient,
        "silent": t.silent,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _next_run_from_cron(cron_expr: str, tz: str = "UTC") -> Optional[str]:
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
        logger.warning("Could not compute next_run for cron %r tz %r: %s", cron_expr, tz, e)
        return None


async def sync_automations(
    db,
    agent_id: str,
    owner_user_id: str,
    slot_content: str,
    agent_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Re-parse the slot, upsert/delete rows, notify scheduler. Return result dict."""
    result = await parse_automation_file(slot_content, agent_context or {})

    keep_hashes: List[str] = []
    upserted: List[dict] = []
    for t in result.tasks:
        h = _task_hash(t)
        keep_hashes.append(h)
        next_run = _next_run_from_cron(t.schedule_cron, t.timezone or "UTC")
        row = await db.upsert_automation(
            agent_id=agent_id,
            owner_user_id=owner_user_id,
            source_hash=h,
            task_label=t.task_label,
            prompt=t.prompt,
            schedule_cron=t.schedule_cron,
            schedule_natural=t.schedule_natural,
            timezone=t.timezone,
            channel=t.channel,
            channel_recipient=t.channel_recipient,
            silent=t.silent,
            enabled=True,
            next_run_at=next_run,
        )
        upserted.append(row)

    removed = await db.delete_automations_not_in(agent_id, owner_user_id, keep_hashes)

    # Notify scheduler so it picks up new/changed rows promptly.
    try:
        from app.scheduler import get_scheduler
        sched = get_scheduler()
        await sched.sync_tasks(agent_id)
    except Exception as e:
        logger.debug("Scheduler sync_tasks failed (non-fatal): %s", e)

    tasks = await db.list_automations(agent_id=agent_id, owner_user_id=owner_user_id)
    return {
        "tasks": tasks,
        "removed": removed,
        "error": result.error,
    }

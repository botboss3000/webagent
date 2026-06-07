"""Automations dashboard API — read + manage surface for the UI.

Aggregates the two trigger tables (``agent_automations`` cron/once/timer +
``agent_event_subscriptions`` event) plus clone runners and per-run history into
one place, and exposes pause/resume/edit/delete/run-now. The agent-facing tools
live in ``app.tools.automation_tools``; this is the human/dashboard counterpart.

Auth: every route resolves the caller via ``assert_caller_is`` and only ever
returns / mutates rows the caller owns (``owner_user_id``).

Routes (prefix /api/v1/automations):
  GET    ""              — aggregated list across the caller's agents
  GET    /{id}/runs      — run history for one automation or subscription
  GET    /clones         — the caller's clone runners (+ which row owns them)
  PATCH  /{id}           — edit / pause / resume (body: fields)
  DELETE /{id}           — delete (reaps a dedicated clone runner)
  POST   /{id}/run-now   — fire a scheduled automation immediately
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from app.auth.identity import assert_caller_is

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/automations", tags=["automations"])


def _delivery_summary(row: Dict[str, Any]) -> str:
    """One-line human summary of where a row delivers."""
    raw = row.get("delivery") if isinstance(row.get("delivery"), dict) else None
    if not raw:
        dj = row.get("delivery_json")
        if isinstance(dj, str) and dj.strip() not in ("", "{}"):
            try:
                raw = json.loads(dj)
            except Exception:
                raw = None
    if raw and raw.get("targets"):
        parts = []
        for t in raw["targets"]:
            mode = t.get("mode")
            if mode == "channel":
                parts.append(t.get("channel") or "channel")
            elif mode == "here":
                parts.append("this chat")
            elif mode:
                parts.append(mode)
        if raw.get("silent"):
            parts.append("silent")
        return ", ".join(parts) or "—"
    # legacy
    ch = row.get("channel")
    if row.get("silent"):
        return "silent"
    if not ch or ch == "webchat":
        return "this chat"
    return ch


def _annotate_automation(row: Dict[str, Any], agent_names: Dict[str, str]) -> Dict[str, Any]:
    once = (row.get("schedule_kind") or "cron") == "once"
    runner_id = row.get("runner_agent_id")
    return {
        "id": row.get("id"),
        "kind": "timer" if once else "schedule",
        "table": "automation",
        "agent_id": row.get("agent_id"),
        "agent_name": agent_names.get(row.get("agent_id")),
        "task_label": row.get("task_label"),
        "prompt": row.get("prompt"),
        "trigger": (row.get("schedule_cron") if not once else "once") or "—",
        "schedule_natural": row.get("schedule_natural"),
        "timezone": row.get("timezone"),
        "run_mode": row.get("run_mode") or "inline",
        "runner_agent_id": runner_id,
        "runner_label": (f"clone of {agent_names.get(row.get('agent_id'), 'agent')}"
                         if runner_id else (row.get("run_mode") or "inline")),
        "delivery": _delivery_summary(row),
        "enabled": bool(row.get("enabled")),
        "next_run_at": row.get("next_run_at"),
        "last_run_at": row.get("last_run_at"),
        "last_status": row.get("last_status"),
        "last_error": row.get("last_error"),
        "runs_today": row.get("runs_today"),
        "max_per_day": row.get("max_per_day"),
        "fail_count": row.get("fail_count"),
        "disable_after_failures": row.get("disable_after_failures"),
        "expires_at": row.get("expires_at"),
        "origin": row.get("origin"),
        "created_at": row.get("created_at"),
    }


def _annotate_subscription(row: Dict[str, Any], agent_names: Dict[str, str]) -> Dict[str, Any]:
    runner_id = row.get("runner_agent_id")
    return {
        "id": row.get("id"),
        "kind": "event",
        "table": "subscription",
        "agent_id": row.get("agent_id"),
        "agent_name": agent_names.get(row.get("agent_id")),
        "task_label": row.get("task_label"),
        "prompt": row.get("prompt"),
        "trigger": f"{row.get('source')}/{row.get('event_type')}",
        "source": row.get("source"),
        "event_type": row.get("event_type"),
        "filter": row.get("filter") or {},
        "run_mode": row.get("run_mode") or "inline",
        "runner_agent_id": runner_id,
        "runner_label": (f"clone of {agent_names.get(row.get('agent_id'), 'agent')}"
                         if runner_id else (row.get("run_mode") or "inline")),
        "delivery": _delivery_summary(row),
        "enabled": bool(row.get("enabled")),
        "last_event_at": row.get("last_event_at"),
        "last_status": row.get("last_status"),
        "last_error": row.get("last_error"),
        "fire_count": row.get("fire_count"),
        "max_per_day": row.get("max_per_day"),
        "expires_at": row.get("expires_at"),
        "origin": row.get("origin"),
        "created_at": row.get("created_at"),
    }


async def _agent_name_map(db, rows) -> Dict[str, str]:
    names: Dict[str, str] = {}
    for aid in {r.get("agent_id") for r in rows if r.get("agent_id")}:
        try:
            ag = await db.get_agent_by_id(aid)
            if ag:
                names[aid] = ag.get("name") or aid
        except Exception:
            pass
    return names


async def _find_row(db, row_id: str):
    """Return (row, table) for an id across both trigger tables, else (None, None)."""
    row = await db.get_automation(row_id)
    if row:
        return row, "automation"
    row = await db.get_event_subscription(row_id)
    if row:
        return row, "subscription"
    return None, None


@router.get("")
async def list_automations(request: Request, user_id: str = Query(...)):
    """Aggregated list of every automation, timer, and event trigger the caller owns."""
    user_id = await assert_caller_is(request, user_id)
    from app.db import get_db
    db = get_db()
    autos = await db.list_automations(owner_user_id=user_id)
    subs = await db.list_event_subscriptions(owner_user_id=user_id)
    names = await _agent_name_map(db, list(autos) + list(subs))
    items = [_annotate_automation(r, names) for r in autos] + \
            [_annotate_subscription(r, names) for r in subs]
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"automations": items, "count": len(items)}


@router.get("/clones")
async def list_clones(request: Request, user_id: str = Query(...)):
    """Clone runner agents owned by the caller (for the dashboard's workers view)."""
    user_id = await assert_caller_is(request, user_id)
    from app.db import get_db
    db = get_db()
    clones = await db.list_clone_agents(user_id)
    out = [{
        "id": c.get("id"), "name": c.get("name"),
        "clone_of": c.get("clone_of"), "model": c.get("model"),
        "created_at": c.get("created_at"),
    } for c in clones]
    return {"clones": out, "count": len(out)}


@router.get("/{automation_id}/runs")
async def automation_runs(request: Request, automation_id: str,
                          user_id: str = Query(...), limit: int = Query(default=50)):
    """Per-run history for one automation or event subscription the caller owns."""
    user_id = await assert_caller_is(request, user_id)
    from app.db import get_db
    db = get_db()
    row, table = await _find_row(db, automation_id)
    if not row or row.get("owner_user_id") != user_id:
        raise HTTPException(status_code=404, detail="automation not found")
    if table == "subscription":
        runs = await db.list_automation_runs(subscription_id=automation_id, limit=limit)
    else:
        runs = await db.list_automation_runs(automation_id=automation_id, limit=limit)
    return {"runs": runs, "count": len(runs)}


@router.patch("/{automation_id}")
async def patch_automation(request: Request, automation_id: str, user_id: str = Query(...)):
    """Edit / pause / resume. Body is a JSON object of fields to set."""
    user_id = await assert_caller_is(request, user_id)
    try:
        body = await request.json()
    except Exception:
        body = {}
    from app.db import get_db
    from app.automation.runner import _compute_next_run
    db = get_db()
    row, table = await _find_row(db, automation_id)
    if not row or row.get("owner_user_id") != user_id:
        raise HTTPException(status_code=404, detail="automation not found")

    common = {"task_label", "prompt", "enabled", "delivery_json", "run_mode",
              "clone_abilities", "max_per_day", "disable_after_failures",
              "expires_at", "retry_max", "retry_backoff_seconds"}
    fields: Dict[str, Any] = {k: v for k, v in body.items() if k in common}
    if table == "automation":
        if "schedule_cron" in body:
            fields["schedule_cron"] = body["schedule_cron"]
            fields["next_run_at"] = _compute_next_run(
                body["schedule_cron"], body.get("timezone") or row.get("timezone") or "UTC")
        if "timezone" in body:
            fields["timezone"] = body["timezone"]
        # Re-enabling a recurring task with no next_run → recompute.
        if body.get("enabled") and (row.get("schedule_kind") or "cron") != "once" and not row.get("next_run_at"):
            fields.setdefault("next_run_at", _compute_next_run(
                fields.get("schedule_cron") or row.get("schedule_cron") or "",
                fields.get("timezone") or row.get("timezone") or "UTC"))
        updated = await db.update_automation(automation_id, **fields)
    else:
        if "filter" in body:
            fields["filter_json"] = body["filter"]
        updated = await db.update_event_subscription(automation_id, **fields)
    return {"status": "ok", "automation": updated}


@router.delete("/{automation_id}")
async def delete_automation(request: Request, automation_id: str, user_id: str = Query(...)):
    """Delete an automation/subscription; reap a dedicated clone runner if any."""
    user_id = await assert_caller_is(request, user_id)
    from app.db import get_db
    db = get_db()
    row, table = await _find_row(db, automation_id)
    if not row or row.get("owner_user_id") != user_id:
        raise HTTPException(status_code=404, detail="automation not found")

    runner_id = (row.get("runner_agent_id") or "").strip()
    if table == "automation":
        ok = await db.delete_automation(automation_id)
    else:
        try:
            from app.automation.sync import _unregister_event_sub
            await _unregister_event_sub(row)
        except Exception:
            pass
        ok = await db.delete_event_subscription(automation_id)
    if runner_id and row.get("run_mode") == "dedicated_clone":
        try:
            await db.delete_clone_agent(runner_id)
        except Exception:
            pass
    return {"status": "ok" if ok else "error"}


@router.post("/{automation_id}/run-now")
async def run_now(request: Request, automation_id: str, user_id: str = Query(...)):
    """Fire a scheduled automation immediately (not applicable to event subscriptions)."""
    user_id = await assert_caller_is(request, user_id)
    from app.db import get_db
    db = get_db()
    row, table = await _find_row(db, automation_id)
    if not row or row.get("owner_user_id") != user_id:
        raise HTTPException(status_code=404, detail="automation not found")
    if table != "automation":
        raise HTTPException(status_code=400, detail="run-now applies to scheduled tasks only")
    try:
        from app.scheduler import get_scheduler
        res = await get_scheduler().run_now(automation_id)
        return {"status": "ok", "result": res}
    except Exception as e:
        logger.exception("run-now failed for %s", automation_id)
        raise HTTPException(status_code=500, detail=str(e))

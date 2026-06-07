"""
Automations Dashboard — read-only aggregation endpoint.

Unifies four disconnected sources into one view per agent:
  • agent_automations        — scheduled cron tasks
  • agent_event_subscriptions — push/poll event triggers
  • agent_spawns             — active worker spawns (orchestrator helpers)
  • webhook_registrations    — user-level generic inbound webhooks (separate section)

No destructive actions. Follows the same auth pattern as list_event_subscriptions.
Drop-in file — not wired into any core registry.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from app.auth.identity import assert_caller_is
from app.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/automations", tags=["automations"])


def _safe_str(v: Any, fallback: str = "") -> str:
    return str(v) if v is not None else fallback


def _fmt_time(iso: Optional[str]) -> Optional[str]:
    """Normalise ISO timestamp or return None."""
    if not iso:
        return None
    try:
        # Accept both 'Z' and '+00:00' suffixes
        return iso.replace("T", " ")[:19]
    except Exception:
        return iso


def _decorate_automation(row: dict) -> dict:
    """Normalise an agent_automations row for the dashboard."""
    return {
        "id": row.get("id"),
        "type": "scheduled",
        "agent_id": row.get("agent_id"),
        "label": row.get("task_label") or "",
        "prompt": row.get("prompt") or "",
        "trigger": row.get("schedule_cron") or "",
        "timezone": row.get("timezone") or "UTC",
        "channel": row.get("channel"),
        "silent": bool(row.get("silent", False)),
        "enabled": bool(row.get("enabled", True)),
        "next_run_at": _fmt_time(row.get("next_run_at")),
        "last_run_at": _fmt_time(row.get("last_run_at")),
        "last_status": row.get("last_status"),
        "last_error": row.get("last_error"),
        "last_session_id": row.get("last_session_id"),
        # run count isn't stored directly on automations; estimate from last_status presence
        "run_count": 1 if row.get("last_run_at") else 0,
        "created_at": _fmt_time(row.get("created_at")),
    }


def _decorate_subscription(row: dict) -> dict:
    """Normalise an agent_event_subscriptions row for the dashboard."""
    filter_raw = row.get("filter_json") or row.get("filter") or {}
    if isinstance(filter_raw, str):
        try:
            filter_raw = json.loads(filter_raw)
        except Exception:
            filter_raw = {}
    return {
        "id": row.get("id"),
        "type": "event",
        "agent_id": row.get("agent_id"),
        "label": row.get("task_label") or "",
        "prompt": row.get("prompt") or "",
        "trigger": f"{row.get('source')}/{row.get('event_type')}",
        "source": row.get("source"),
        "event_type": row.get("event_type"),
        "filter": filter_raw,
        "channel": row.get("channel"),
        "silent": bool(row.get("silent", False)),
        "enabled": bool(row.get("enabled", True)),
        "last_event_at": _fmt_time(row.get("last_event_at")),
        "last_status": row.get("last_status"),
        "last_error": row.get("last_error"),
        "fire_count": row.get("fire_count", 0),
        "external_expiration_at": _fmt_time(row.get("external_expiration_at")),
        "last_polled_at": _fmt_time(row.get("last_polled_at")),
        "created_at": _fmt_time(row.get("created_at")),
    }


def _decorate_spawn(row: dict) -> dict:
    """Normalise an agent_spawns row for the dashboard."""
    return {
        "id": row.get("id"),
        "type": "worker",
        "agent_id": row.get("orchestrator_agent_id"),
        "spawn_agent_id": row.get("spawn_agent_id"),
        "spawn_session_id": row.get("spawn_session_id"),
        "name": row.get("name") or "",
        "task": (row.get("task") or "")[:120],
        "status": row.get("status"),
        "result_summary": (row.get("result_summary") or "")[:200],
        "next_check_at": _fmt_time(row.get("next_check_at")),
        "heartbeat_at": _fmt_time(row.get("heartbeat_at")),
        "created_at": _fmt_time(row.get("created_at")),
    }


@router.get("/dashboard")
async def get_automations_dashboard(
    request: Request,
    user_id: str = Query(...),
    agent_id: Optional[str] = Query(default=None),
):
    """
    Aggregate all automation-like activity for the caller's agents.

    Returns:
      - agents: list of { agent_id, agent_name, agent_description,
                  automations, event_subscriptions, spawns }
      - webhooks: user-level generic webhook registrations
      - clones: orphaned/standalone clone agents (not linked to a live spawn)
    """
    # Verify caller identity if possible (reuse the same token pattern)
    try:
        caller_id = await assert_caller_is(request, user_id)
    except Exception:
        caller_id = user_id

    db = get_db()

    # 1. Resolve which agents to query
    if agent_id:
        # Single-agent scoped view
        agent = await db.get_agent_by_id(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found.")
        # Auth: caller must own or be admin of this agent
        from app.api.agents import _is_agent_admin
        if not await _is_agent_admin(db, agent_id, caller_id):
            raise HTTPException(status_code=403, detail="Not authorized to view this agent.")
        agent_ids = [agent_id]
        agent_map = {agent_id: agent}
    else:
        # All agents the caller can see
        all_agents = await db.list_agents_for_user(caller_id, include_admin=await db.is_user_admin(caller_id))
        agent_ids = [a["id"] for a in all_agents if a.get("status") in ("active", None, "")]
        agent_map = {a["id"]: a for a in all_agents}

    # 2. Fetch automations for all agents
    all_automations: List[dict] = []
    for aid in agent_ids:
        rows = await db.list_automations(agent_id=aid, owner_user_id=caller_id)
        all_automations.extend(rows)

    # 3. Fetch event subscriptions for all agents
    all_subs: List[dict] = []
    for aid in agent_ids:
        rows = await db.list_event_subscriptions(agent_id=aid, owner_user_id=caller_id)
        all_subs.extend(rows)

    # 4. Fetch spawns for all agents
    all_spawns: List[dict] = []
    for aid in agent_ids:
        try:
            rows = await _list_spawns_by_agent(aid)
            all_spawns.extend(rows)
        except Exception as e:
            logger.warning("Failed to list spawns for agent %s: %s", aid, e)

    # 5. Fetch webhooks (user-level, not agent-level)
    webhooks: List[dict] = []
    try:
        wh_rows = await db.list_webhooks(caller_id)
        for w in wh_rows:
            webhooks.append({
                "id": w.get("id"),
                "type": "webhook",
                "name": w.get("name") or "",
                "instructions": w.get("instructions") or "",
                "active": bool(w.get("active", True)),
                "created_at": _fmt_time(w.get("created_at")),
            })
    except Exception as e:
        logger.warning("Failed to list webhooks: %s", e)

    # 6. Find clone agents (status='clone') that aren't linked to a live spawn
    clones: List[dict] = []
    try:
        spawn_agent_ids = {s.get("spawn_agent_id") for s in all_spawns if s.get("spawn_agent_id")}
        clone_rows = await _list_clone_agents(db, caller_id)
        for c in clone_rows:
            meta = c.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            clone_of = meta.get("clone_of") or ""
            cid = c.get("id", "")
            clones.append({
                "id": cid,
                "type": "clone",
                "name": c.get("name") or "",
                "clone_of": clone_of,
                "status": c.get("status"),
                "has_live_spawn": cid in spawn_agent_ids,
                "created_at": _fmt_time(c.get("created_at")),
            })
    except Exception as e:
        logger.warning("Failed to list clone agents: %s", e)

    # 7. Build per-agent output
    automations_by_agent: Dict[str, list] = {a: [] for a in agent_ids}
    for row in all_automations:
        aid = row.get("agent_id")
        if aid in automations_by_agent:
            automations_by_agent[aid].append(_decorate_automation(row))

    subs_by_agent: Dict[str, list] = {a: [] for a in agent_ids}
    for row in all_subs:
        aid = row.get("agent_id")
        if aid in subs_by_agent:
            subs_by_agent[aid].append(_decorate_subscription(row))

    spawns_by_agent: Dict[str, list] = {a: [] for a in agent_ids}
    for row in all_spawns:
        aid = row.get("orchestrator_agent_id")
        if aid in spawns_by_agent:
            spawns_by_agent[aid].append(_decorate_spawn(row))

    agents_out = []
    for aid in agent_ids:
        agent = agent_map.get(aid, {})
        agents_out.append({
            "agent_id": aid,
            "agent_name": agent.get("name") or "Unknown",
            "agent_description": (agent.get("description") or "")[:100],
            "automations": automations_by_agent.get(aid, []),
            "event_subscriptions": subs_by_agent.get(aid, []),
            "spawns": spawns_by_agent.get(aid, []),
        })

    return {
        "agents": agents_out,
        "webhooks": webhooks,
        "clones": clones,
    }


# ── Manage endpoints (run history + pause/resume/edit/delete/run-now) ──────
# The /dashboard route above is read-only aggregation. These add the
# per-run history view and the mutations the dashboard's row controls call.
# Routes use distinct paths from /dashboard, so both coexist on the same prefix.

async def _find_row(db, row_id: str):
    """Return (row, table) for an id across both trigger tables, else (None, None)."""
    row = await db.get_automation(row_id)
    if row:
        return row, "automation"
    row = await db.get_event_subscription(row_id)
    if row:
        return row, "subscription"
    return None, None


async def _owns(row: dict, caller_id: str) -> bool:
    return bool(row) and row.get("owner_user_id") == caller_id


@router.get("/{automation_id}/runs")
async def automation_runs(request: Request, automation_id: str,
                          user_id: str = Query(...), limit: int = Query(default=50)):
    """Per-run history for one automation or event subscription the caller owns."""
    caller_id = await assert_caller_is(request, user_id)
    db = get_db()
    row, table = await _find_row(db, automation_id)
    if not await _owns(row, caller_id):
        raise HTTPException(status_code=404, detail="automation not found")
    if table == "subscription":
        runs = await db.list_automation_runs(subscription_id=automation_id, limit=limit)
    else:
        runs = await db.list_automation_runs(automation_id=automation_id, limit=limit)
    return {"runs": runs, "count": len(runs)}


@router.patch("/{automation_id}")
async def patch_automation(request: Request, automation_id: str, user_id: str = Query(...)):
    """Edit / pause / resume. Body is a JSON object of fields to set."""
    caller_id = await assert_caller_is(request, user_id)
    try:
        body = await request.json()
    except Exception:
        body = {}
    from app.automation.runner import _compute_next_run
    db = get_db()
    row, table = await _find_row(db, automation_id)
    if not await _owns(row, caller_id):
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
    caller_id = await assert_caller_is(request, user_id)
    db = get_db()
    row, table = await _find_row(db, automation_id)
    if not await _owns(row, caller_id):
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
    caller_id = await assert_caller_is(request, user_id)
    db = get_db()
    row, table = await _find_row(db, automation_id)
    if not await _owns(row, caller_id):
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


# ── Helper: list spawns by orchestrator agent id ──────────────────────────
# The agent_spawns table lives in the agent_orchestration plugin, not in the
# core schema. We query it via the raw connection (same pattern as the
# plugin's own _spawns_list). No-op gracefully if the table doesn't exist.

async def _list_spawns_by_agent(orchestrator_agent_id: str) -> List[dict]:
    """Return all spawn rows for a given orchestrator agent id."""
    from app.db import get_db
    db = get_db()
    raw = getattr(db, "_get_conn", None)
    if raw is None:
        return []
    conn = raw()
    if conn is None:
        return []
    try:
        # Ensure the table exists (created by the plugin, but safe to re-run)
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS agent_spawns (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    orchestrator_session_id TEXT,
                    orchestrator_agent_id TEXT,
                    spawn_session_id TEXT,
                    spawn_agent_id TEXT,
                    name TEXT,
                    task TEXT,
                    status TEXT,
                    result_summary TEXT,
                    next_check_at TEXT,
                    check_note TEXT,
                    resume_attempts INTEGER,
                    heartbeat_at TEXT,
                    claim_token TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )"""
            )
            for idx_sql in [
                "CREATE INDEX IF NOT EXISTS idx_agent_spawns_orch ON agent_spawns(orchestrator_session_id)",
                "CREATE INDEX IF NOT EXISTS idx_agent_spawns_session ON agent_spawns(spawn_session_id)",
                "CREATE INDEX IF NOT EXISTS idx_agent_spawns_check ON agent_spawns(next_check_at)",
                "CREATE INDEX IF NOT EXISTS idx_agent_spawns_hb ON agent_spawns(status, heartbeat_at)",
            ]:
                try:
                    conn.execute(idx_sql)
                except Exception:
                    pass
        except Exception:
            pass
        rows = conn.execute(
            """SELECT * FROM agent_spawns
               WHERE orchestrator_agent_id = ?
               ORDER BY created_at DESC
               LIMIT 200""",
            (orchestrator_agent_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug("agent_spawns table not available: %s", e)
        return []
    finally:
        conn.close()


async def _list_clone_agents(db, user_id: str) -> List[dict]:
    """List agents with status='clone' that belong to this user.

    Clones are hidden from the normal agent roster and recycling bin.
    This is the dashboard's job: surface them so they're visible somewhere.
    """
    raw = getattr(db, "_get_conn", None)
    if raw is None:
        return []
    conn = raw()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """SELECT id, name, description, status, metadata, template_id, created_at, updated_at
               FROM agents
               WHERE status = 'clone'
               ORDER BY created_at DESC
               LIMIT 200"""
        ).fetchall()
        # Filter by owner in Python (portable across SQLite/Postgres)
        result = []
        for r in rows:
            d = dict(r)
            meta = d.get("metadata") or "{}"
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            owner = meta.get("owner_user_id") or meta.get("user_id")
            if not owner or owner == user_id:
                result.append(d)
        return result
    except Exception as e:
        logger.debug("Failed to list clone agents: %s", e)
        return []
    finally:
        conn.close()
"""
Agent management API.

Provides endpoints for listing, creating, updating, deleting, and configuring
user agents. Also exposes the user profile (including is_admin flag).

Endpoints
---------
GET  /api/v1/user/profile               — user profile + admin flag
GET  /api/v1/agents                     — list all agents/templates visible to the user
POST /api/v1/agents                     — create a new custom agent (cloned from default)
GET  /api/v1/agents/{agent_id}          — get a single custom agent
PUT  /api/v1/agents/{agent_id}          — update editable fields on a custom agent
DELETE /api/v1/agents/{agent_id}        — delete a custom agent
GET  /api/v1/agents/templates           — list agent templates (for tool breakdown display)
POST /api/v1/agents/test                — run a test message through an agent config
GET  /api/v1/agents/{agent_id}/members  — list agent admins + members with stats (agent admin only)
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.auth.identity import assert_caller_is
from app.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["agents"])


# ── Request / Response models ─────────────────────────────────────────────────

class CreateAgentRequest(BaseModel):
    user_id: str
    name: str
    description: Optional[str] = ""
    template_id: Optional[str] = "default"


class SlotPayload(BaseModel):
    slot_name: str
    order_index: int = 0
    lock: bool = False
    merge_mode: str = "replace"
    content: str = ""


class UpdateAgentRequest(BaseModel):
    user_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    max_turn_count: Optional[int] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    allowed_tools: Optional[List[str]] = None
    custom_tool_ids: Optional[List[str]] = None
    trigger_type: Optional[str] = None
    trigger_key: Optional[str] = None
    loop_logic: Optional[List] = None
    safety_policy: Optional[Dict[str, Any]] = None
    user_mode: Optional[str] = None
    # Per-agent LLM override (stored in metadata['llm_config'])
    llm_config: Optional[Dict[str, Any]] = None
    # Prompt slots — admin-only. Full slot set when present; reconciled against existing.
    slots: Optional[List[SlotPayload]] = None
    # Per-slot wipe of all user override rows at save time.
    reset_overrides_for: Optional[List[str]] = None


class UpdateMyPromptsItem(BaseModel):
    slot_name: str
    content: str


class UpdateMyPromptsRequest(BaseModel):
    user_id: str
    slots: List[UpdateMyPromptsItem]


class UpdateTemplateRequest(BaseModel):
    user_id: str
    template_id: str
    discoverable: Optional[bool] = None


class UpsertConnectionRequest(BaseModel):
    user_id: str
    enabled: bool
    config: Optional[Dict[str, Any]] = None


class SaveAsTemplateRequest(BaseModel):
    user_id: str
    template_id: str
    name: str
    description: Optional[str] = ""
    icon: Optional[str] = ""
    discoverable: Optional[bool] = False
    access_level: Optional[str] = "all"


class AnonSessionRequest(BaseModel):
    browser_id: Optional[str] = None


class TestAgentRequest(BaseModel):
    user_id: str
    agent_id: str          # template id (e.g. 'default') or custom agents.id UUID
    message: str
    session_id: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_agent(agent: dict) -> dict:
    """Strip locked/internal fields before returning to client.

    Prompt content lives in the agent_prompts table and is exposed via the
    dedicated /agents/{id}/slots and /agents/{id}/my-prompts endpoints, not on
    the agent row itself.
    """
    import json as _json
    HIDDEN = {"provider", "metadata"}
    result = {k: v for k, v in agent.items() if k not in HIDDEN}
    # Deserialize JSON list fields so the client receives actual arrays
    for field in ("allowed_tools", "custom_tool_ids", "loop_logic", "admin_users", "member_users", "authorized_users"):
        raw = result.get(field)
        if isinstance(raw, str):
            try:
                result[field] = _json.loads(raw)
            except Exception:
                result[field] = []
        elif raw is None:
            result[field] = []
    # Deserialize safety_policy JSON → dict
    sp_raw = result.get("safety_policy")
    if isinstance(sp_raw, str):
        try:
            result["safety_policy"] = _json.loads(sp_raw)
        except Exception:
            result["safety_policy"] = {}
    elif sp_raw is None:
        result["safety_policy"] = {}
    # Expose llm_config from metadata (defaults to use_default=True if absent)
    meta_raw = agent.get("metadata")
    meta = {}
    if isinstance(meta_raw, str):
        try:
            meta = _json.loads(meta_raw)
        except Exception:
            pass
    result["llm_config"] = meta.get("llm_config") or {"use_default": True}
    return result


async def _require_admin(db, user_id: str) -> None:
    """Raise 403 if user is not an admin."""
    if not await db.is_user_admin(user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")


async def _is_agent_admin(db, agent_id: str, user_id: str) -> bool:
    """Return True if user is a global admin OR in the agent's admin_users list."""
    if await db.is_user_admin(user_id):
        return True
    roles = await db.get_agent_roles(agent_id)
    return user_id in roles["admin_users"]


async def _require_connection_enabled(db, agent_id: str, connection_type: str) -> None:
    """Raise 403 if the connection is not enabled on this agent."""
    rows = await db.get_agent_connections(agent_id)
    conn = next((r for r in rows if r["connection_type"] == connection_type), None)
    if not conn or not conn.get("enabled"):
        raise HTTPException(status_code=403, detail="This integration is not enabled by the agent admin.")


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/user/profile")
async def get_user_profile(user_id: str = Query(...)):
    """Return user profile including is_admin flag."""
    db = get_db()
    profile = await db.get_user_profile(user_id)
    if not profile:
        # Return a safe default rather than 404 — profile is auto-created on first write
        return {"user_id": user_id, "is_admin": False, "default_agent_id": None}
    return {
        "user_id": profile["user_id"],
        "is_admin": bool(profile.get("is_admin")),
        "default_agent_id": profile.get("default_agent_id"),
    }


@router.get("/agents/templates")
async def list_agent_templates(
    user_id: str = Query(...),
    include_admin: bool = Query(False),
    discoverable_only: bool = Query(False),
):
    """
    List agent templates (system agents, is_pipeline=0).
    If include_admin=true, requires the user to be an admin (bypasses discoverable filter).
    If discoverable_only=true, only returns templates with discoverable=1.
    """
    db = get_db()
    if include_admin:
        await _require_admin(db, user_id)
    templates = await db.list_agent_templates(
        include_admin=include_admin,
        discoverable_only=discoverable_only,
    )
    return {"templates": [_safe_agent(t) for t in templates]}


@router.get("/agents")
async def list_agents(user_id: str = Query(...)):
    """
    List agents from the agents table where the user is in admin_users.
    System templates are excluded — only actual agent rows are returned.
    Each entry includes a 'source' field: 'custom'.
    """
    db = get_db()
    is_admin = await db.is_user_admin(user_id)
    all_agents = await db.list_agents_for_user(user_id, include_admin=is_admin)
    # Only return rows that came from the agents table (source='custom'),
    # not system template entries.
    user_agents = [a for a in all_agents if a.get("source") == "custom"]
    return {"agents": [_safe_agent(a) for a in user_agents]}


@router.post("/agents")
async def create_agent(req: CreateAgentRequest):
    """
    Create a new custom agent cloned from the default template.
    Returns the new agent with editable fields only.
    """
    db = get_db()
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail="Agent name is required.")
    agent = await db.create_custom_agent(
        user_id=req.user_id,
        name=req.name.strip(),
        description=req.description or "",
        template_id=req.template_id or "default",
    )
    # Platform admins' own agents are exempt from payment by default.
    # The admin can delete the exemption later via /billing/exemptions if
    # they want to charge for their own agent.
    try:
        if await db.is_user_admin(req.user_id):
            await _maybe_auto_exempt_agent(db, agent["id"], req.user_id)
    except Exception:
        pass
    return {"agent": _safe_agent(agent)}


async def _maybe_auto_exempt_agent(db, agent_id: str, granting_user_id: str) -> None:
    """Insert a kind='agent' exemption if one doesn't already exist."""
    if not hasattr(db, "_get_conn"):
        return
    import uuid as _uuid
    conn = db._get_conn()
    try:
        existing = conn.execute(
            "SELECT id FROM billing_exemptions WHERE kind='agent' AND agent_id=? LIMIT 1",
            (agent_id,),
        ).fetchone()
        if existing:
            return
        conn.execute(
            "INSERT INTO billing_exemptions (id, kind, agent_id, granted_by_user_id, reason) VALUES (?,?,?,?,?)",
            (str(_uuid.uuid4()), "agent", agent_id, granting_user_id,
             "auto: platform-admin-owned agent"),
        )
        conn.commit()
    except Exception:
        # Table may not exist yet pre-migration; safe to ignore.
        pass
    finally:
        conn.close()


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, user_id: str = Query(...)):
    """Get a single custom agent by id (must be owned by user)."""
    db = get_db()
    # Check custom agents table
    agents = await db.list_agents_for_user(user_id)
    for a in agents:
        if a.get("id") == agent_id:
            return {"agent": _safe_agent(a)}
    raise HTTPException(status_code=404, detail="Agent not found.")


@router.put("/agents/{agent_id}")
async def update_agent(agent_id: str, req: UpdateAgentRequest):
    """
    Update editable fields on a custom agent. Caller must be an agent admin.

    Two write lanes coexist on this endpoint:
      - Agent-row fields (name, model, allowed_tools, etc.) via `updates`.
      - Admin-base prompt slots via `slots` (full slot set — reconciled).
        Optional `reset_overrides_for` wipes per-user override rows for the
        listed slot_names at save time.
    """
    db = get_db()
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can edit this agent.")

    import json as _json
    payload = req.dict()
    slots_in = payload.pop("slots", None)
    reset_for = payload.pop("reset_overrides_for", None)
    llm_config_in = payload.pop("llm_config", None)
    updates = {k: v for k, v in payload.items()
               if k not in ("user_id",) and v is not None}

    # Merge llm_config into the agent's metadata blob
    if llm_config_in is not None:
        current = await db.get_agent_by_id(agent_id)
        meta = {}
        if current:
            meta_raw = current.get("metadata")
            if isinstance(meta_raw, str):
                try:
                    meta = _json.loads(meta_raw)
                except Exception:
                    pass
        meta["llm_config"] = llm_config_in
        updates["metadata"] = meta

    updated = await db.update_agent_fields(
        agent_id=agent_id,
        user_id=req.user_id,
        updates=updates,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Agent not found or not owned by this user.")

    if slots_in is not None:
        # Reconcile admin-base slot set.
        await db.replace_slots(
            agent_id=agent_id,
            slots=[s if isinstance(s, dict) else s.dict() for s in slots_in],
            reset_overrides_for=reset_for or [],
            updated_by=f"admin:{req.user_id}",
        )

    if any(k in updates for k in ("trigger_type", "trigger_key")):
        from app.agent import trigger_index
        trigger_index.build()
    return {"agent": _safe_agent(updated)}


@router.post("/agents/{agent_id}/save-as-template")
async def save_agent_as_template(agent_id: str, req: SaveAsTemplateRequest):
    """
    Snapshot a custom agent's current config + admin-base prompt slots into a
    new reusable template (rows in agent_templates + agent_prompt_templates).
    Admin-only.
    """
    db = get_db()
    await _require_admin(db, req.user_id)

    import re
    slug = (req.template_id or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", slug):
        raise HTTPException(
            status_code=400,
            detail="template_id must be 2-64 chars: lowercase letters, digits, '_' or '-'.",
        )
    if not req.name or not req.name.strip():
        raise HTTPException(status_code=400, detail="Template name is required.")

    try:
        tpl = await db.save_agent_as_template(
            agent_id=agent_id,
            template_id=slug,
            name=req.name.strip(),
            description=(req.description or "").strip(),
            icon=(req.icon or "").strip(),
            discoverable=bool(req.discoverable),
            access_level=req.access_level or "all",
            updated_by=f"admin:{req.user_id}",
        )
    except ValueError as e:
        msg = str(e)
        status = 409 if "already exists" in msg else 400
        if "not found" in msg:
            status = 404
        raise HTTPException(status_code=status, detail=msg)

    return {"template": _safe_agent(tpl)}


@router.get("/agents/{agent_id}/slots")
async def get_agent_slots(request: Request, agent_id: str, user_id: str = Query(...)):
    user_id = await assert_caller_is(request, user_id)
    """Return admin-base slot definitions for an agent or template.

    Custom agents return the rows scoped to their agent_id. System templates
    return the rows scoped to template_id (agent_prompts.agent_id = template_id).
    """
    db = get_db()
    agent = await db.get_agent_by_id(agent_id)
    is_template = False
    if not agent:
        # Try as a template id (system templates have no agents row).
        templates = await db.list_agent_templates(include_admin=True)
        tpl = next((t for t in templates if t.get("id") == agent_id), None)
        if not tpl:
            raise HTTPException(status_code=404, detail="Agent not found.")
        is_template = True
    slots = await db.list_slots(agent_id, user_id=user_id)
    if is_template:
        # Templates have no per-agent admin role — only global admins may edit.
        is_admin = await db.is_user_admin(user_id)
    else:
        is_admin = await _is_agent_admin(db, agent_id, user_id)
    return {"slots": slots, "user_role": "admin" if is_admin else "member"}


@router.put("/agents/{agent_id}/my-prompts")
async def update_my_prompts(request: Request, agent_id: str, req: UpdateMyPromptsRequest):
    """Write the caller's per-user override rows for one or more unlocked slots.

    Locked slots and unknown slot_names are rejected per slot (the rest still
    write). Returns the resolved slot list for this caller.

    If the ``automation`` slot is among the writes, also re-parse it into
    structured ``agent_automations`` rows and return them under
    ``automation_tasks``.
    """
    req.user_id = await assert_caller_is(request, req.user_id)
    db = get_db()
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")
    rejected: List[Dict[str, str]] = []
    written: List[str] = []
    automation_content: Optional[str] = None
    for item in req.slots:
        res = await db.upsert_override(
            agent_id=agent_id,
            slot_name=item.slot_name,
            user_id=req.user_id,
            content=item.content or "",
            updated_by=f"user:{req.user_id}",
        )
        if res is None:
            rejected.append({"slot_name": item.slot_name, "reason": "locked_or_unknown"})
        else:
            written.append(item.slot_name)
            if item.slot_name == "automation":
                automation_content = item.content or ""

    slots = await db.list_slots(agent_id, user_id=req.user_id)

    automation_payload: Optional[Dict[str, Any]] = None
    if automation_content is not None:
        try:
            from app.automation.sync import sync_automations
            automation_payload = await sync_automations(
                db=db,
                agent_id=agent_id,
                owner_user_id=req.user_id,
                slot_content=automation_content,
                agent_context={
                    "name": agent.get("name"),
                    "description": agent.get("description"),
                },
            )
        except Exception as e:
            logger.warning("Automation sync failed: %s", e)
            automation_payload = {"tasks": [], "removed": 0, "error": str(e)}

    response = {"written": written, "rejected": rejected, "slots": slots}
    if automation_payload is not None:
        response["automation_tasks"] = automation_payload.get("tasks", [])
        response["automation_error"] = automation_payload.get("error")
        # Surface the event-trigger half of the parse result too, with
        # the same health badge the UI's event-subscriptions panel uses.
        evt_subs = automation_payload.get("event_subscriptions") or []
        response["automation_event_subscriptions"] = [
            _decorate_subscription_health(s) for s in evt_subs
        ]
        response["automation_removed_event_subscriptions"] = (
            automation_payload.get("removed_event_subscriptions") or []
        )
    return response


@router.get("/agents/{agent_id}/automations")
async def list_agent_automations(request: Request, agent_id: str, user_id: str = Query(...)):
    """List parsed scheduled task rows for this user/agent."""
    user_id = await assert_caller_is(request, user_id)
    db = get_db()
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")
    rows = await db.list_automations(agent_id=agent_id, owner_user_id=user_id)
    return {"tasks": rows}


@router.post("/agents/{agent_id}/automations/parse")
async def reparse_agent_automations(request: Request, agent_id: str, user_id: str = Query(...)):
    """Re-run the LLM parser against the caller's resolved ``automation`` slot."""
    user_id = await assert_caller_is(request, user_id)
    db = get_db()
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")

    slots = await db.list_slots(agent_id, user_id=user_id)
    content = ""
    for s in slots:
        if s.get("slot_name") == "automation":
            content = s.get("override_content") or s.get("content") or ""
            break

    from app.automation.sync import sync_automations
    try:
        result = await sync_automations(
            db=db, agent_id=agent_id, owner_user_id=user_id,
            slot_content=content,
            agent_context={
                "name": agent.get("name"),
                "description": agent.get("description"),
            },
        )
    except Exception as e:
        logger.warning("Re-parse failed: %s", e)
        result = {
            "tasks": [], "removed": 0,
            "event_subscriptions": [], "removed_event_subscriptions": [],
            "error": str(e),
        }
    evt_subs = result.get("event_subscriptions") or []
    return {
        "tasks": result.get("tasks", []),
        "removed": result.get("removed", 0),
        "event_subscriptions": [_decorate_subscription_health(s) for s in evt_subs],
        "removed_event_subscriptions": result.get("removed_event_subscriptions") or [],
        "error": result.get("error"),
    }


class UpdateAutomationBody(BaseModel):
    user_id: str
    enabled: Optional[bool] = None
    silent: Optional[bool] = None
    channel: Optional[str] = None
    channel_recipient: Optional[str] = None


@router.patch("/agents/{agent_id}/automations/{automation_id}")
async def patch_agent_automation(request: Request, agent_id: str, automation_id: str, body: UpdateAutomationBody):
    """Toggle enabled / channel / silent for a single task without re-parsing."""
    body.user_id = await assert_caller_is(request, body.user_id)
    db = get_db()
    row = await db.get_automation(automation_id)
    if not row or row.get("agent_id") != agent_id:
        raise HTTPException(status_code=404, detail="Automation not found.")
    if row.get("owner_user_id") != body.user_id and not await db.is_user_admin(body.user_id):
        raise HTTPException(status_code=403, detail="Not the owner of this automation.")
    fields: Dict[str, Any] = {}
    if body.enabled is not None:
        fields["enabled"] = body.enabled
    if body.silent is not None:
        fields["silent"] = body.silent
    if body.channel is not None:
        fields["channel"] = body.channel or None
    if body.channel_recipient is not None:
        fields["channel_recipient"] = body.channel_recipient or None
    updated = await db.update_automation(automation_id, **fields)
    return {"task": updated}


@router.post("/agents/{agent_id}/automations/{automation_id}/run-now")
async def run_agent_automation_now(request: Request, agent_id: str, automation_id: str, user_id: str = Query(...)):
    """Fire an automation immediately. Does not affect ``next_run_at``."""
    user_id = await assert_caller_is(request, user_id)
    db = get_db()
    row = await db.get_automation(automation_id)
    if not row or row.get("agent_id") != agent_id:
        raise HTTPException(status_code=404, detail="Automation not found.")
    if row.get("owner_user_id") != user_id and not await db.is_user_admin(user_id):
        raise HTTPException(status_code=403, detail="Not the owner of this automation.")
    from app.scheduler import get_scheduler
    try:
        result = await get_scheduler().run_now(automation_id)
    except Exception as e:
        logger.warning("run_now failed: %s", e)
        result = {"ok": False, "error": str(e)}
    fresh = await db.get_automation(automation_id)
    return {"result": result, "task": fresh}


@router.post("/automations/fire/{automation_id}")
async def fire_remote_automation(automation_id: str, token: str = Query(...)):
    """Webhook entry point used by remote scheduler providers (Google Cloud
    Scheduler, cron-job.org, generic webhooks). Validates the per-automation
    ``fire_token`` and runs the executor.
    """
    db = get_db()
    row = await db.get_automation_by_fire_token(automation_id, token)
    if not row:
        raise HTTPException(status_code=404, detail="Unknown automation or bad token.")
    if not row.get("enabled"):
        return {"ok": False, "skipped": "disabled"}
    from app.scheduler.executor import execute_automation
    result = await execute_automation(row)
    return {"ok": bool(result.get("ok")), "session_id": result.get("session_id"), "error": result.get("error")}


@router.delete("/agents/{agent_id}/automations/{automation_id}")
async def delete_agent_automation(request: Request, agent_id: str, automation_id: str, user_id: str = Query(...)):
    """Delete one automation row (the file is left untouched)."""
    user_id = await assert_caller_is(request, user_id)
    db = get_db()
    row = await db.get_automation(automation_id)
    if not row or row.get("agent_id") != agent_id:
        raise HTTPException(status_code=404, detail="Automation not found.")
    if row.get("owner_user_id") != user_id and not await db.is_user_admin(user_id):
        raise HTTPException(status_code=403, detail="Not the owner of this automation.")
    deleted = await db.delete_automation(automation_id)
    return {"deleted": bool(deleted)}


# ============================================================
# Per-agent event subscriptions (push + poll triggers)
#
# The Automation slot can produce both cron rows (agent_automations) and
# event-trigger rows (agent_event_subscriptions). These endpoints expose
# the event-trigger rows to the Automation tab UI so the user can see
# provider health, toggle, edit, test-fire, re-register, or delete them
# directly instead of editing the English file.
#
# Source-of-truth model (option 3): the English file is the user's
# **intent**; the DB rows are the **active** state. UI edits write the DB
# only, leaving the file alone. Re-parsing the file always merges new
# triggers in (existing rows with matching source_hash are no-ops).
# ============================================================


@router.get("/agents/{agent_id}/event-subscriptions")
async def list_agent_event_subscriptions(request: Request, agent_id: str, user_id: str = Query(...)):
    user_id = await assert_caller_is(request, user_id)
    """List the caller's event-trigger rows for this agent.

    Each row carries the same observability fields the renewer/poller write:
    last_status, last_error, last_event_at, fire_count, external_expiration_at.
    """
    db = get_db()
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")
    rows = await db.list_event_subscriptions(agent_id=agent_id, owner_user_id=user_id)
    # Decorate each row with a "health" badge derived from its state.
    decorated = [_decorate_subscription_health(r) for r in rows]
    return {"subscriptions": decorated}


def _decorate_subscription_health(row: Dict[str, Any]) -> Dict[str, Any]:
    """Compute a health string the UI can render directly."""
    from datetime import datetime, timezone, timedelta
    out = dict(row)
    if not row.get("enabled"):
        out["health"] = "disabled"
        return out
    last_status = row.get("last_status") or ""
    if last_status == "error":
        out["health"] = "error"
        return out
    exp = row.get("external_expiration_at")
    if exp:
        try:
            t = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if t <= now:
                out["health"] = "expired"
            elif (t - now) < timedelta(hours=24):
                out["health"] = "expiring_soon"
            else:
                out["health"] = "ok"
        except Exception:
            out["health"] = "ok"
    else:
        # No TTL (poll source, or comms bridge). OK as long as no error.
        out["health"] = "ok"
    return out


class UpdateEventSubscriptionBody(BaseModel):
    user_id: str
    enabled: Optional[bool] = None
    silent: Optional[bool] = None
    channel: Optional[str] = None
    channel_recipient: Optional[str] = None
    prompt: Optional[str] = None
    task_label: Optional[str] = None
    filter: Optional[Dict[str, Any]] = None


@router.patch("/agents/{agent_id}/event-subscriptions/{sub_id}")
async def patch_agent_event_subscription(
    request: Request, agent_id: str, sub_id: str, body: UpdateEventSubscriptionBody,
):
    body.user_id = await assert_caller_is(request, body.user_id)
    """Edit a single event subscription without re-parsing the English file.

    Per option 3: the row is the active state; the Automation slot is the
    user's original intent. A subsequent re-parse will *re-enable* anything
    whose English line still exists, so disabling via this endpoint is a
    soft-state change rather than a permanent erase. To permanently remove
    a trigger, either delete the row (DELETE) AND edit the English file, or
    just edit the English file and re-save.
    """
    db = get_db()
    row = await db.get_event_subscription(sub_id)
    if not row or row.get("agent_id") != agent_id:
        raise HTTPException(status_code=404, detail="Event subscription not found.")
    if row.get("owner_user_id") != body.user_id and not await db.is_user_admin(body.user_id):
        raise HTTPException(status_code=403, detail="Not the owner of this subscription.")

    fields: Dict[str, Any] = {}
    if body.enabled is not None:
        fields["enabled"] = body.enabled
    if body.silent is not None:
        fields["silent"] = body.silent
    if body.channel is not None:
        fields["channel"] = body.channel or None
    if body.channel_recipient is not None:
        fields["channel_recipient"] = body.channel_recipient or None
    if body.prompt is not None:
        fields["prompt"] = body.prompt
    if body.task_label is not None:
        fields["task_label"] = body.task_label
    if body.filter is not None:
        import json as _json
        fields["filter_json"] = _json.dumps(body.filter or {}, sort_keys=True)

    updated = await db.update_event_subscription(sub_id, **fields)

    # If the filter changed and this is a push source, the provider-side
    # watch may no longer reflect the new filter (e.g. Gmail label filter).
    # Re-register so the watch matches.
    needs_rereg = (body.filter is not None) and bool(row.get("external_subscription_id"))
    if needs_rereg:
        try:
            await _rereg_subscription(updated)
            updated = await db.get_event_subscription(sub_id)
        except Exception as e:
            logger.warning("Auto re-register after PATCH failed for %s: %s", sub_id, e)
    return {"subscription": _decorate_subscription_health(updated)}


@router.post("/agents/{agent_id}/event-subscriptions/{sub_id}/re-register")
async def reregister_agent_event_subscription(
    request: Request, agent_id: str, sub_id: str, user_id: str = Query(...),
):
    user_id = await assert_caller_is(request, user_id)
    """Re-run the source plugin's ``register_subscription`` for this row.

    Used after Pub/Sub topic recreation, OAuth reconnect, or to clear a
    ``last_status=error`` state. Updates external_subscription_id,
    external_expiration_at, and external_metadata to the new values.
    """
    db = get_db()
    row = await db.get_event_subscription(sub_id)
    if not row or row.get("agent_id") != agent_id:
        raise HTTPException(status_code=404, detail="Event subscription not found.")
    if row.get("owner_user_id") != user_id and not await db.is_user_admin(user_id):
        raise HTTPException(status_code=403, detail="Not the owner of this subscription.")
    try:
        await _rereg_subscription(row)
    except Exception as e:
        logger.warning("re-register failed for %s: %s", sub_id, e)
        await db.update_event_subscription(sub_id, last_status="error", last_error=str(e)[:400])
        raise HTTPException(status_code=400, detail=f"register failed: {e}")
    fresh = await db.get_event_subscription(sub_id)
    return {"subscription": _decorate_subscription_health(fresh)}


@router.post("/agents/{agent_id}/event-subscriptions/{sub_id}/test-fire")
async def test_fire_event_subscription(
    request: Request, agent_id: str, sub_id: str, user_id: str = Query(...),
):
    user_id = await assert_caller_is(request, user_id)
    """Synthesize a fake event and run it through the router for this sub.

    Bypasses the source's filter (we manufacture a NormalizedEvent that
    already matches) so you can verify the agent's behavior end-to-end
    without waiting for a real event from the provider. Logs a delivery
    row marked status='test' for audit clarity.
    """
    db = get_db()
    row = await db.get_event_subscription(sub_id)
    if not row or row.get("agent_id") != agent_id:
        raise HTTPException(status_code=404, detail="Event subscription not found.")
    if row.get("owner_user_id") != user_id and not await db.is_user_admin(user_id):
        raise HTTPException(status_code=403, detail="Not the owner of this subscription.")

    from app.events.types import NormalizedEvent
    from app.events.executor import execute_event_subscription
    from datetime import datetime, timezone
    import uuid as _uuid

    fake = NormalizedEvent(
        source=row["source"],
        event_type=row["event_type"],
        owner_user_id=row["owner_user_id"],
        external_id=f"test-{_uuid.uuid4().hex[:12]}",
        occurred_at=datetime.now(timezone.utc).isoformat(),
        payload={
            "_test_fire": True,
            "label": row.get("task_label"),
            "filter": row.get("filter") or {},
            "note": "Synthesized by /test-fire — not from a real provider event.",
        },
        raw_ref={},
    )

    delivery_id = await db.insert_event_delivery(
        subscription_id=sub_id,
        source=row["source"],
        event_type=row["event_type"],
        event_external_id=fake.external_id,
        owner_user_id=row["owner_user_id"],
        agent_id=agent_id,
        status="pending",
        payload_excerpt=fake.payload_excerpt(),
    )
    try:
        result = await execute_event_subscription(row, fake)
        await db.update_event_delivery(
            delivery_id,
            status="test" if result.get("ok") else "error",
            error=result.get("error"),
            session_id=result.get("session_id"),
        )
        await db.update_event_subscription(
            sub_id,
            last_status="ok" if result.get("ok") else "error",
            last_error=result.get("error"),
            last_event_at=fake.occurred_at,
        )
        return {"result": result, "delivery_id": delivery_id}
    except Exception as e:
        logger.exception("test-fire failed: %s", e)
        await db.update_event_delivery(delivery_id, status="error", error=str(e)[:400])
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/agents/{agent_id}/event-subscriptions/{sub_id}")
async def delete_agent_event_subscription(
    request: Request, agent_id: str, sub_id: str, user_id: str = Query(...),
):
    user_id = await assert_caller_is(request, user_id)
    """Delete the row AND unregister the provider-side watch.

    Per option 3, the English file is left alone — a future re-parse with
    the same line in the file will re-create the row + register a new
    provider watch.
    """
    db = get_db()
    row = await db.get_event_subscription(sub_id)
    if not row or row.get("agent_id") != agent_id:
        raise HTTPException(status_code=404, detail="Event subscription not found.")
    if row.get("owner_user_id") != user_id and not await db.is_user_admin(user_id):
        raise HTTPException(status_code=403, detail="Not the owner of this subscription.")

    # Best-effort unregister at provider; deletion proceeds either way.
    try:
        from app.events import get_manager
        src = get_manager().get(row["source"])
        if src is not None:
            await src.unregister_subscription(row)
    except Exception as e:
        logger.warning("Provider unregister failed for %s (ignored): %s", sub_id, e)

    deleted = await db.delete_event_subscription(sub_id)
    return {"deleted": bool(deleted)}


async def _rereg_subscription(row: Dict[str, Any]) -> None:
    """Re-run ``register_subscription`` for a row and persist the new IDs.

    Shared by the PATCH (filter changed) and POST re-register endpoints.
    Tries to unregister the old watch first so providers don't keep firing
    against a stale subscription id.
    """
    from app.events import get_manager
    db = get_db()
    src = get_manager().get(row["source"])
    if src is None:
        raise RuntimeError(f"Source {row['source']!r} not registered")
    if row.get("external_subscription_id"):
        try:
            await src.unregister_subscription(row)
        except Exception as e:
            logger.debug("unregister-before-rereg failed: %s", e)
    reg = await src.register_subscription(
        owner_user_id=row["owner_user_id"],
        event_type=row["event_type"],
        filter_dict=row.get("filter") or {},
    )
    await db.update_event_subscription(
        row["id"],
        external_subscription_id=reg.external_subscription_id,
        external_resource_id=reg.external_resource_id,
        external_expiration_at=reg.external_expiration_at,
        external_metadata=reg.external_metadata or {},
        poll_cursor=reg.poll_cursor,
        last_status="ok",
        last_error=None,
    )


@router.delete("/agents/{agent_id}/my-prompts/{slot_name}")
async def delete_my_prompt(request: Request, agent_id: str, slot_name: str, user_id: str = Query(...)):
    """Remove the caller's override row for a single slot."""
    user_id = await assert_caller_is(request, user_id)
    db = get_db()
    deleted = await db.delete_override(agent_id, slot_name, user_id)
    return {"deleted": bool(deleted)}


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, user_id: str = Query(...)):
    """Delete a custom agent. Cannot delete system agents."""
    db = get_db()
    deleted = await db.delete_custom_agent(agent_id=agent_id, user_id=user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Agent not found, not owned by this user, or is a system agent.")
    return {"deleted": True, "agent_id": agent_id}


@router.post("/agents/test")
async def test_agent(req: TestAgentRequest):
    """
    Run a sample message through an agent configuration and return the response.
    Used by the Agent Management panel test sandbox — does NOT create a persistent session.
    The agent's prompts are used as-is from its current saved state.
    """
    import uuid as _uuid_mod
    db = get_db()

    # Resolve the agent config
    agents = await db.list_agents_for_user(req.user_id, include_admin=await db.is_user_admin(req.user_id))
    target = next((a for a in agents if a.get("id") == req.agent_id), None)
    if not target:
        # Try looking up by template id directly
        templates = await db.list_agent_templates(include_admin=True)
        target = next((t for t in templates if t.get("id") == req.agent_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Agent not found.")

    # Build a system prompt by resolving the caller's slots (admin base + overrides).
    from app.agent.prompts import build_system_prompt

    test_session_id = req.session_id or f"test-{str(_uuid_mod.uuid4())[:8]}"

    agent_id_for_resolve = target.get("id", req.agent_id)
    resolved_slots = await db.resolve_prompts(agent_id_for_resolve, user_id=req.user_id)
    context_docs = [
        {"id": s["slot_name"], "content": s["content"]}
        for s in resolved_slots if (s.get("content") or "").strip()
    ]

    system_prompt = await build_system_prompt(
        context_docs,
        brain_context=None,
        user_id=req.user_id,
    )

    # Run a single-turn agent loop (non-streaming)
    from app.agent.loop import run_agent_loop_buffered
    result = await run_agent_loop_buffered(
        user_id=req.user_id,
        session_id=test_session_id,
        user_message=req.message,
        system_prompt=system_prompt,
        agent_id=target.get("id", req.agent_id),
        history=[],
        max_turns=3,
        db=db,
        agent_template_id=target.get("template_id") or target.get("id"),
    )

    # Fetch the interactions stored during this test run so the UI can render
    # a pipeline loop without a separate round-trip (avoids session-ownership checks).
    interactions: List[Dict[str, Any]] = []
    try:
        import sqlite3 as _sqlite3
        from pathlib import Path as _Path
        _db_path = _Path(__file__).resolve().parent.parent / "db" / "local.db"
        if _db_path.exists():
            _conn = _sqlite3.connect(str(_db_path))
            _conn.row_factory = _sqlite3.Row
            _rows = _conn.execute(
                "SELECT id, session_id, role, content, tool_name, metadata, created_at "
                "FROM interactions WHERE session_id = ? ORDER BY created_at ASC",
                (test_session_id,),
            ).fetchall()
            _conn.close()
            interactions = [dict(r) for r in _rows]
    except Exception:
        pass  # frontend falls back to plain reply display

    return {"reply": result, "session_id": test_session_id, "interactions": interactions}


# ── Connection catalog — defines all known connection types ───────────────────

_CONNECTION_CATALOG = [
    # ── Channels ──
    {"connection_type": "telegram",  "section": "channel",     "display_name": "Telegram",        "status": "available"},
    {"connection_type": "twilio",    "section": "channel",     "display_name": "Twilio (SMS/Call)","status": "coming_soon"},
    {"connection_type": "email",     "section": "channel",     "display_name": "Email",            "status": "coming_soon"},
    {"connection_type": "whatsapp",  "section": "channel",     "display_name": "WhatsApp",         "status": "coming_soon"},
    {"connection_type": "discord",   "section": "channel",     "display_name": "Discord",          "status": "coming_soon"},
    {"connection_type": "slack",     "section": "channel",     "display_name": "Slack",            "status": "coming_soon"},
    # ── Integrations ──
    {"connection_type": "google",    "section": "integration", "display_name": "Google",           "status": "available"},
    {"connection_type": "microsoft", "section": "integration", "display_name": "Microsoft 365",    "status": "available"},
    {"connection_type": "yahoo",     "section": "integration", "display_name": "Yahoo",            "status": "available"},
    {"connection_type": "dropbox",   "section": "integration", "display_name": "Dropbox",          "status": "available"},
    {"connection_type": "github",    "section": "integration", "display_name": "GitHub",           "status": "coming_soon"},
    {"connection_type": "bank",      "section": "integration", "display_name": "Bank Accounts",    "status": "coming_soon"},
    {"connection_type": "search",    "section": "integration", "display_name": "Search Engine",    "status": "coming_soon"},
    {"connection_type": "scraper",          "section": "integration", "display_name": "Web Scraper",     "status": "available"},
    {"connection_type": "browser_session",  "section": "integration", "display_name": "Browser Session", "status": "available"},
    # ── Social Media ──
    {"connection_type": "facebook",  "section": "social",      "display_name": "Facebook",         "status": "available"},
    {"connection_type": "instagram", "section": "social",      "display_name": "Instagram",        "status": "available"},
    {"connection_type": "twitter",   "section": "social",      "display_name": "X (Twitter)",      "status": "available"},
    {"connection_type": "linkedin",  "section": "social",      "display_name": "LinkedIn",         "status": "available"},
    {"connection_type": "tiktok",    "section": "social",      "display_name": "TikTok",           "status": "available"},
    {"connection_type": "pinterest", "section": "social",      "display_name": "Pinterest",        "status": "available"},
    {"connection_type": "reddit",    "section": "social",      "display_name": "Reddit",           "status": "available"},
    {"connection_type": "snapchat",  "section": "social",      "display_name": "Snapchat",         "status": "available"},
    {"connection_type": "twitch",    "section": "social",      "display_name": "Twitch",           "status": "available"},
    # ── Marketplaces ──
    {"connection_type": "ebay",      "section": "marketplace", "display_name": "eBay",             "status": "available"},
    {"connection_type": "etsy",      "section": "marketplace", "display_name": "Etsy",             "status": "available"},
    {"connection_type": "shopify",   "section": "marketplace", "display_name": "Shopify",          "status": "available"},
    {"connection_type": "amazon",    "section": "marketplace", "display_name": "Amazon Seller",    "status": "available"},

    # ── Agent Tools (host-side privileged capabilities) ──
    {"connection_type": "codebase_admin", "section": "ability", "display_name": "Codebase Admin", "status": "available"},
    {"connection_type": "create_tools",   "section": "ability", "display_name": "Create Tools",   "status": "available"},
    {"connection_type": "automation",     "section": "ability", "display_name": "Automation",     "status": "available"},
]


@router.get("/agents/{agent_id}/connections")
async def get_agent_connections(agent_id: str, user_id: str = Query(...)):
    """
    Return connections for an agent, filtered to those the admin has configured.
    Unconfigured providers are hidden so they can't be toggled on from the UI.
    Bot tokens in config are masked to last 4 chars.
    For Google: merges user's auth_elements status (email, name, picture).
    """
    import json as _json
    from app.admin.integrations import get_admin_configured_providers
    db = get_db()
    rows = await db.get_agent_connections(agent_id)
    saved = {r["connection_type"]: r for r in rows}

    # Only surface integrations the admin has configured (plus the per-agent/
    # per-user ones that need no admin OAuth setup). Unconfigured providers
    # are hidden from the agent page entirely so they cannot be toggled on.
    configured_providers = await get_admin_configured_providers(user_id)

    # Fetch auth_elements for all OAuth-backed providers.
    # Maps connection_type → service key stored in auth_elements.
    # facebook and instagram both alias to the "meta" OAuth app.
    _OAUTH_PROVIDERS = {
        "google":    "google",
        "microsoft": "microsoft",
        "yahoo":     "yahoo",
        "dropbox":   "dropbox",
        "facebook":  "meta",
        "instagram": "meta",
        "twitter":   "twitter",
        "linkedin":  "linkedin",
        "tiktok":    "tiktok",
        "pinterest": "pinterest",
        "reddit":    "reddit",
        "snapchat":  "snapchat",
        "twitch":    "twitch",
        "ebay":      "ebay",
        "etsy":      "etsy",
        "shopify":   "shopify",
        "amazon":    "amazon",
    }
    # Tokens are scoped per (user, agent); look up only this agent's row so that
    # signing into Google on Agent A does NOT make Agent B appear connected.
    from app.integrations.oauth_helper import oauth_label
    _label = oauth_label(agent_id)
    _service_cache: dict[str, dict] = {}
    provider_auth: dict[str, dict] = {}
    for ct, service_key in _OAUTH_PROVIDERS.items():
        try:
            if service_key not in _service_cache:
                elem = await db.auth_element_get(user_id, service_key, _label)
                if elem:
                    cfg = elem.get("config", {})
                    if isinstance(cfg, str):
                        cfg = _json.loads(cfg)
                    _service_cache[service_key] = cfg
                else:
                    _service_cache[service_key] = {}
            cfg = _service_cache[service_key]
            if cfg:
                provider_auth[ct] = cfg
        except Exception:
            pass

    result = []
    for entry in _CONNECTION_CATALOG:
        ct = entry["connection_type"]
        if ct not in configured_providers:
            continue
        row = saved.get(ct)
        config = {}
        if row:
            try:
                config = _json.loads(row.get("config") or "{}")
            except Exception:
                config = {}
            # Mask sensitive token fields
            if "bot_token" in config and config["bot_token"]:
                tok = config["bot_token"]
                config["bot_token"] = "•" * max(0, len(tok) - 4) + tok[-4:]

        item = {
            **entry,
            "enabled": bool(row["enabled"]) if row else False,
            "config": config,
        }

        # Merge OAuth account info for providers that use auth_elements
        if ct in _OAUTH_PROVIDERS:  # dict membership check
            auth = provider_auth.get(ct)
            if auth:
                item[f"{ct}_connected"] = True
                item[f"{ct}_email"] = auth.get("email", "")
                item[f"{ct}_name"] = auth.get("name", "")
                item[f"{ct}_picture"] = auth.get("picture", "")
                item[f"{ct}_connected_at"] = auth.get("connected_at", "")
            else:
                item[f"{ct}_connected"] = False

        result.append(item)
    is_admin = await _is_agent_admin(db, agent_id, user_id)
    return {"connections": result, "user_role": "admin" if is_admin else "member"}


@router.put("/agents/{agent_id}/connections/{connection_type}")
async def upsert_agent_connection(
    agent_id: str,
    connection_type: str,
    req: UpsertConnectionRequest,
):
    """
    Create or update a connection on an agent.
    Accepts enabled flag and arbitrary config dict.
    If bot_token in config starts with bullets (masked), preserve existing token.
    """
    import json as _json
    db = get_db()

    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can modify connection settings.")

    # Resolve catalog entry for section
    catalog_entry = next(
        (c for c in _CONNECTION_CATALOG if c["connection_type"] == connection_type), None
    )
    if catalog_entry is None:
        raise HTTPException(status_code=400, detail=f"Unknown connection type: {connection_type}")
    if catalog_entry["status"] == "coming_soon":
        raise HTTPException(status_code=400, detail=f"{catalog_entry['display_name']} is not yet available.")

    # Refuse to enable a provider the admin hasn't configured. (Disable is
    # always allowed so the UI can clean up stale rows if creds were removed.)
    if req.enabled:
        from app.admin.integrations import get_admin_configured_providers
        if connection_type not in await get_admin_configured_providers(req.user_id):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{catalog_entry['display_name']} has not been configured in "
                    f"App Config → Integrations. Ask an admin to set it up first."
                ),
            )

    new_config = dict(req.config or {})

    # If bot_token is masked (starts with bullet), keep existing token
    incoming_token = new_config.get("bot_token", "")
    if incoming_token and "•" in incoming_token:
        existing_rows = await db.get_agent_connections(agent_id)
        existing = next((r for r in existing_rows if r["connection_type"] == connection_type), None)
        if existing:
            try:
                old_cfg = _json.loads(existing.get("config") or "{}")
                new_config["bot_token"] = old_cfg.get("bot_token", "")
            except Exception:
                pass

    row = await db.upsert_agent_connection(
        agent_id=agent_id,
        connection_type=connection_type,
        section=catalog_entry["section"],
        enabled=req.enabled,
        config=new_config,
    )

    # Signal manager to reload Telegram connections if needed
    if connection_type == "telegram":
        try:
            from app.communications.manager import get_plugin_manager
            pm = get_plugin_manager()
            if hasattr(pm, "reload_agent_connections"):
                import asyncio
                asyncio.create_task(pm.reload_agent_connections())
        except Exception:
            pass

    # Return masked config
    resp_config = dict(new_config)
    if "bot_token" in resp_config and resp_config["bot_token"]:
        tok = resp_config["bot_token"]
        resp_config["bot_token"] = "•" * max(0, len(tok) - 4) + tok[-4:]

    return {
        "connection": {
            **catalog_entry,
            "enabled": req.enabled,
            "config": resp_config,
        }
    }


@router.get("/agents/{agent_id}/connections/google/authorize")
async def google_authorize_for_agent(request: Request, agent_id: str, user_id: str = Query(...)):
    """Generate Google OAuth authorization URL for a user+agent pair."""
    db = get_db()
    await _require_connection_enabled(db, agent_id, "google")
    from app.admin.integrations import get_google_creds, build_google_authorize_url
    client_id, _ = await get_google_creds()
    if not client_id:
        return {"error": "Google OAuth not configured. Admin must set credentials in App Config → Integrations."}
    authorize_url = await build_google_authorize_url(user_id=user_id, agent_id=agent_id, request=request)
    return {"authorize_url": authorize_url}


@router.delete("/agents/{agent_id}/connections/google/disconnect")
async def google_disconnect_for_agent(agent_id: str, user_id: str = Query(...)):
    """Disconnect Google account for a user (revoke OAuth only, preserve admin toggle)."""
    from app.admin.integrations import revoke_and_delete_google
    deleted = await revoke_and_delete_google(user_id, agent_id)
    return {"status": "ok", "deleted": deleted}


@router.get("/agents/{agent_id}/connections/microsoft/authorize")
async def microsoft_authorize_for_agent(agent_id: str, user_id: str = Query(...)):
    """Generate Microsoft OAuth authorization URL for a user+agent pair."""
    db = get_db()
    await _require_connection_enabled(db, agent_id, "microsoft")
    from app.admin.integrations import get_microsoft_creds, build_microsoft_authorize_url
    client_id, _ = await get_microsoft_creds()
    if not client_id:
        return {"error": "Microsoft OAuth not configured. Admin must set credentials in App Config → Integrations."}
    authorize_url = await build_microsoft_authorize_url(user_id=user_id, agent_id=agent_id)
    return {"authorize_url": authorize_url}


@router.delete("/agents/{agent_id}/connections/microsoft/disconnect")
async def microsoft_disconnect_for_agent(agent_id: str, user_id: str = Query(...)):
    """Disconnect Microsoft account for a user (revoke OAuth only, preserve admin toggle)."""
    from app.admin.integrations import revoke_and_delete_microsoft
    deleted = await revoke_and_delete_microsoft(user_id, agent_id)
    return {"status": "ok", "deleted": deleted}


@router.get("/agents/{agent_id}/connections/yahoo/authorize")
async def yahoo_authorize_for_agent(agent_id: str, user_id: str = Query(...)):
    """Generate Yahoo OAuth authorization URL for a user+agent pair."""
    db = get_db()
    await _require_connection_enabled(db, agent_id, "yahoo")
    from app.admin.integrations import get_yahoo_creds, build_yahoo_authorize_url
    client_id, _ = await get_yahoo_creds()
    if not client_id:
        return {"error": "Yahoo OAuth not configured. Admin must set credentials in App Config → Integrations."}
    authorize_url = await build_yahoo_authorize_url(user_id=user_id, agent_id=agent_id)
    return {"authorize_url": authorize_url}


@router.delete("/agents/{agent_id}/connections/yahoo/disconnect")
async def yahoo_disconnect_for_agent(agent_id: str, user_id: str = Query(...)):
    """Disconnect Yahoo account for a user (revoke OAuth only, preserve admin toggle)."""
    from app.admin.integrations import revoke_and_delete_yahoo
    deleted = await revoke_and_delete_yahoo(user_id, agent_id)
    return {"status": "ok", "deleted": deleted}


@router.get("/agents/{agent_id}/connections/dropbox/authorize")
async def dropbox_authorize_for_agent(agent_id: str, user_id: str = Query(...)):
    """Generate Dropbox OAuth authorization URL for a user+agent pair."""
    db = get_db()
    await _require_connection_enabled(db, agent_id, "dropbox")
    from app.admin.integrations import get_dropbox_creds, build_dropbox_authorize_url
    client_id, _ = await get_dropbox_creds()
    if not client_id:
        return {"error": "Dropbox OAuth not configured. Admin must set credentials in App Config → Integrations."}
    authorize_url = await build_dropbox_authorize_url(user_id=user_id, agent_id=agent_id)
    return {"authorize_url": authorize_url}


@router.delete("/agents/{agent_id}/connections/dropbox/disconnect")
async def dropbox_disconnect_for_agent(agent_id: str, user_id: str = Query(...)):
    """Disconnect Dropbox account for a user (revoke OAuth only, preserve admin toggle)."""
    from app.admin.integrations import revoke_and_delete_dropbox
    deleted = await revoke_and_delete_dropbox(user_id, agent_id)
    return {"status": "ok", "deleted": deleted}


# ── Social Media OAuth routes ─────────────────────────────────────────────────
# Meta covers both Facebook and Instagram via a single OAuth app.

@router.get("/agents/{agent_id}/connections/facebook/authorize")
async def facebook_authorize_for_agent(agent_id: str, user_id: str = Query(...)):
    """Generate Meta (Facebook/Instagram) OAuth authorization URL."""
    db = get_db()
    await _require_connection_enabled(db, agent_id, "facebook")
    from app.admin.integrations import get_meta_creds, build_meta_authorize_url
    client_id, _ = await get_meta_creds()
    if not client_id:
        return {"error": "Meta OAuth not configured. Admin must set credentials in App Config → Integrations."}
    authorize_url = await build_meta_authorize_url(user_id=user_id, agent_id=agent_id)
    return {"authorize_url": authorize_url}


@router.delete("/agents/{agent_id}/connections/facebook/disconnect")
async def facebook_disconnect_for_agent(agent_id: str, user_id: str = Query(...)):
    """Disconnect Meta account (removes Facebook + Instagram access, preserves admin toggle)."""
    from app.admin.integrations import revoke_and_delete_meta
    deleted = await revoke_and_delete_meta(user_id, agent_id)
    return {"status": "ok", "deleted": deleted}


@router.get("/agents/{agent_id}/connections/instagram/authorize")
async def instagram_authorize_for_agent(agent_id: str, user_id: str = Query(...)):
    """Generate Meta (Facebook/Instagram) OAuth authorization URL."""
    db = get_db()
    await _require_connection_enabled(db, agent_id, "instagram")
    from app.admin.integrations import get_meta_creds, build_meta_authorize_url
    client_id, _ = await get_meta_creds()
    if not client_id:
        return {"error": "Meta OAuth not configured. Admin must set credentials in App Config → Integrations."}
    authorize_url = await build_meta_authorize_url(user_id=user_id, agent_id=agent_id)
    return {"authorize_url": authorize_url}


@router.delete("/agents/{agent_id}/connections/instagram/disconnect")
async def instagram_disconnect_for_agent(agent_id: str, user_id: str = Query(...)):
    """Disconnect Meta account (removes Facebook + Instagram access, preserves admin toggle)."""
    from app.admin.integrations import revoke_and_delete_meta
    deleted = await revoke_and_delete_meta(user_id, agent_id)
    return {"status": "ok", "deleted": deleted}


@router.get("/agents/{agent_id}/connections/twitter/authorize")
async def twitter_authorize_for_agent(agent_id: str, user_id: str = Query(...)):
    """Generate Twitter/X OAuth authorization URL."""
    db = get_db()
    await _require_connection_enabled(db, agent_id, "twitter")
    from app.admin.integrations import get_twitter_creds, build_twitter_authorize_url
    client_id, _ = await get_twitter_creds()
    if not client_id:
        return {"error": "Twitter/X OAuth not configured. Admin must set credentials in App Config → Integrations."}
    authorize_url, _ = await build_twitter_authorize_url(user_id=user_id, agent_id=agent_id)
    return {"authorize_url": authorize_url}


@router.delete("/agents/{agent_id}/connections/twitter/disconnect")
async def twitter_disconnect_for_agent(agent_id: str, user_id: str = Query(...)):
    """Disconnect Twitter/X account (revoke OAuth only, preserve admin toggle)."""
    from app.admin.integrations import revoke_and_delete_twitter
    deleted = await revoke_and_delete_twitter(user_id, agent_id)
    return {"status": "ok", "deleted": deleted}


@router.get("/agents/{agent_id}/connections/linkedin/authorize")
async def linkedin_authorize_for_agent(agent_id: str, user_id: str = Query(...)):
    """Generate LinkedIn OAuth authorization URL."""
    db = get_db()
    await _require_connection_enabled(db, agent_id, "linkedin")
    from app.admin.integrations import get_linkedin_creds, build_linkedin_authorize_url
    client_id, _ = await get_linkedin_creds()
    if not client_id:
        return {"error": "LinkedIn OAuth not configured. Admin must set credentials in App Config → Integrations."}
    authorize_url = await build_linkedin_authorize_url(user_id=user_id, agent_id=agent_id)
    return {"authorize_url": authorize_url}


@router.delete("/agents/{agent_id}/connections/linkedin/disconnect")
async def linkedin_disconnect_for_agent(agent_id: str, user_id: str = Query(...)):
    """Disconnect LinkedIn account (revoke OAuth only, preserve admin toggle)."""
    from app.admin.integrations import revoke_and_delete_linkedin
    deleted = await revoke_and_delete_linkedin(user_id, agent_id)
    return {"status": "ok", "deleted": deleted}


@router.get("/agents/{agent_id}/connections/tiktok/authorize")
async def tiktok_authorize_for_agent(agent_id: str, user_id: str = Query(...)):
    """Generate TikTok OAuth authorization URL."""
    db = get_db()
    await _require_connection_enabled(db, agent_id, "tiktok")
    from app.admin.integrations import get_tiktok_creds, build_tiktok_authorize_url
    client_id, _ = await get_tiktok_creds()
    if not client_id:
        return {"error": "TikTok OAuth not configured. Admin must set credentials in App Config → Integrations."}
    authorize_url, _ = await build_tiktok_authorize_url(user_id=user_id, agent_id=agent_id)
    return {"authorize_url": authorize_url}


@router.delete("/agents/{agent_id}/connections/tiktok/disconnect")
async def tiktok_disconnect_for_agent(agent_id: str, user_id: str = Query(...)):
    """Disconnect TikTok account (revoke OAuth only, preserve admin toggle)."""
    from app.admin.integrations import revoke_and_delete_tiktok
    deleted = await revoke_and_delete_tiktok(user_id, agent_id)
    return {"status": "ok", "deleted": deleted}


@router.get("/agents/{agent_id}/connections/pinterest/authorize")
async def pinterest_authorize_for_agent(agent_id: str, user_id: str = Query(...)):
    """Generate Pinterest OAuth authorization URL."""
    db = get_db()
    await _require_connection_enabled(db, agent_id, "pinterest")
    from app.admin.integrations import get_pinterest_creds, build_pinterest_authorize_url
    client_id, _ = await get_pinterest_creds()
    if not client_id:
        return {"error": "Pinterest OAuth not configured. Admin must set credentials in App Config → Integrations."}
    authorize_url = await build_pinterest_authorize_url(user_id=user_id, agent_id=agent_id)
    return {"authorize_url": authorize_url}


@router.delete("/agents/{agent_id}/connections/pinterest/disconnect")
async def pinterest_disconnect_for_agent(agent_id: str, user_id: str = Query(...)):
    """Disconnect Pinterest account (revoke OAuth only, preserve admin toggle)."""
    from app.admin.integrations import revoke_and_delete_pinterest
    deleted = await revoke_and_delete_pinterest(user_id, agent_id)
    return {"status": "ok", "deleted": deleted}


@router.get("/agents/{agent_id}/connections/reddit/authorize")
async def reddit_authorize_for_agent(agent_id: str, user_id: str = Query(...)):
    """Generate Reddit OAuth authorization URL."""
    db = get_db()
    await _require_connection_enabled(db, agent_id, "reddit")
    from app.admin.integrations import get_reddit_creds, build_reddit_authorize_url
    client_id, _ = await get_reddit_creds()
    if not client_id:
        return {"error": "Reddit OAuth not configured. Admin must set credentials in App Config → Integrations."}
    authorize_url = await build_reddit_authorize_url(user_id=user_id, agent_id=agent_id)
    return {"authorize_url": authorize_url}


@router.delete("/agents/{agent_id}/connections/reddit/disconnect")
async def reddit_disconnect_for_agent(agent_id: str, user_id: str = Query(...)):
    """Disconnect Reddit account (revoke OAuth only, preserve admin toggle)."""
    from app.admin.integrations import revoke_and_delete_reddit
    deleted = await revoke_and_delete_reddit(user_id, agent_id)
    return {"status": "ok", "deleted": deleted}


@router.get("/agents/{agent_id}/connections/snapchat/authorize")
async def snapchat_authorize_for_agent(agent_id: str, user_id: str = Query(...)):
    """Generate Snapchat OAuth authorization URL."""
    db = get_db()
    await _require_connection_enabled(db, agent_id, "snapchat")
    from app.admin.integrations import get_snapchat_creds, build_snapchat_authorize_url
    client_id, _ = await get_snapchat_creds()
    if not client_id:
        return {"error": "Snapchat OAuth not configured. Admin must set credentials in App Config → Integrations."}
    authorize_url = await build_snapchat_authorize_url(user_id=user_id, agent_id=agent_id)
    return {"authorize_url": authorize_url}


@router.delete("/agents/{agent_id}/connections/snapchat/disconnect")
async def snapchat_disconnect_for_agent(agent_id: str, user_id: str = Query(...)):
    """Disconnect Snapchat account (revoke OAuth only, preserve admin toggle)."""
    from app.admin.integrations import revoke_and_delete_snapchat
    deleted = await revoke_and_delete_snapchat(user_id, agent_id)
    return {"status": "ok", "deleted": deleted}


@router.get("/agents/{agent_id}/connections/twitch/authorize")
async def twitch_authorize_for_agent(agent_id: str, user_id: str = Query(...)):
    """Generate Twitch OAuth authorization URL."""
    db = get_db()
    await _require_connection_enabled(db, agent_id, "twitch")
    from app.admin.integrations import get_twitch_creds, build_twitch_authorize_url
    client_id, _ = await get_twitch_creds()
    if not client_id:
        return {"error": "Twitch OAuth not configured. Admin must set credentials in App Config → Integrations."}
    authorize_url = await build_twitch_authorize_url(user_id=user_id, agent_id=agent_id)
    return {"authorize_url": authorize_url}


@router.delete("/agents/{agent_id}/connections/twitch/disconnect")
async def twitch_disconnect_for_agent(agent_id: str, user_id: str = Query(...)):
    """Disconnect Twitch account (revoke OAuth only, preserve admin toggle)."""
    from app.admin.integrations import revoke_and_delete_twitch
    deleted = await revoke_and_delete_twitch(user_id, agent_id)
    return {"status": "ok", "deleted": deleted}


# ── Marketplace OAuth routes ──────────────────────────────────────────────

@router.get("/agents/{agent_id}/connections/ebay/authorize")
async def ebay_authorize_for_agent(request: Request, agent_id: str, user_id: str = Query(...)):
    """Generate eBay OAuth authorization URL for a user+agent pair."""
    db = get_db()
    await _require_connection_enabled(db, agent_id, "ebay")
    from app.admin.integrations import get_ebay_creds, build_ebay_authorize_url
    client_id, _ = await get_ebay_creds()
    if not client_id:
        return {"error": "eBay OAuth not configured. Admin must set credentials in App Config → Integrations."}
    authorize_url = await build_ebay_authorize_url(user_id=user_id, agent_id=agent_id, request=request)
    return {"authorize_url": authorize_url}


@router.delete("/agents/{agent_id}/connections/ebay/disconnect")
async def ebay_disconnect_for_agent(agent_id: str, user_id: str = Query(...)):
    """Disconnect eBay account (revoke OAuth only, preserve admin toggle)."""
    from app.admin.integrations import revoke_and_delete_ebay
    deleted = await revoke_and_delete_ebay(user_id, agent_id)
    return {"status": "ok", "deleted": deleted}


@router.get("/agents/{agent_id}/connections/etsy/authorize")
async def etsy_authorize_for_agent(request: Request, agent_id: str, user_id: str = Query(...)):
    """Generate Etsy OAuth authorization URL for a user+agent pair."""
    db = get_db()
    await _require_connection_enabled(db, agent_id, "etsy")
    from app.admin.integrations import get_etsy_creds, build_etsy_authorize_url
    client_id, _ = await get_etsy_creds()
    if not client_id:
        return {"error": "Etsy OAuth not configured. Admin must set credentials in App Config → Integrations."}
    authorize_url = await build_etsy_authorize_url(user_id=user_id, agent_id=agent_id, request=request)
    return {"authorize_url": authorize_url}


@router.delete("/agents/{agent_id}/connections/etsy/disconnect")
async def etsy_disconnect_for_agent(agent_id: str, user_id: str = Query(...)):
    """Disconnect Etsy account (revoke OAuth only, preserve admin toggle)."""
    from app.admin.integrations import revoke_and_delete_etsy
    deleted = await revoke_and_delete_etsy(user_id, agent_id)
    return {"status": "ok", "deleted": deleted}


@router.get("/agents/{agent_id}/connections/shopify/authorize")
async def shopify_authorize_for_agent(
    request: Request,
    agent_id: str,
    user_id: str = Query(...),
    shop: str = Query(..., description="Shopify shop domain, e.g. 'my-store' or 'my-store.myshopify.com'"),
):
    """Generate Shopify install URL for a specific shop. Requires shop domain."""
    db = get_db()
    await _require_connection_enabled(db, agent_id, "shopify")
    from app.admin.integrations import get_shopify_creds, build_shopify_authorize_url
    client_id, _ = await get_shopify_creds()
    if not client_id:
        return {"error": "Shopify OAuth not configured. Admin must set credentials in App Config → Integrations."}
    if not shop:
        return {"error": "Shop domain is required (e.g. 'my-store.myshopify.com')."}
    try:
        authorize_url = await build_shopify_authorize_url(
            user_id=user_id, shop=shop, agent_id=agent_id, request=request,
        )
    except ValueError as e:
        return {"error": str(e)}
    return {"authorize_url": authorize_url}


@router.delete("/agents/{agent_id}/connections/shopify/disconnect")
async def shopify_disconnect_for_agent(agent_id: str, user_id: str = Query(...)):
    """Disconnect Shopify shop (revoke OAuth only, preserve admin toggle)."""
    from app.admin.integrations import revoke_and_delete_shopify
    deleted = await revoke_and_delete_shopify(user_id, agent_id)
    return {"status": "ok", "deleted": deleted}


@router.get("/agents/{agent_id}/connections/amazon/authorize")
async def amazon_authorize_for_agent(
    request: Request,
    agent_id: str,
    user_id: str = Query(...),
    region: str = Query("NA", description="Amazon SP-API region: NA, EU, or FE"),
):
    """Generate Amazon Seller Central authorization URL for a user+agent pair."""
    db = get_db()
    await _require_connection_enabled(db, agent_id, "amazon")
    from app.admin.integrations import get_amazon_creds, build_amazon_authorize_url
    client_id, _ = await get_amazon_creds()
    if not client_id:
        return {"error": "Amazon LWA not configured. Admin must set credentials in App Config → Integrations."}
    authorize_url = await build_amazon_authorize_url(
        user_id=user_id, region=region, agent_id=agent_id, request=request,
    )
    return {"authorize_url": authorize_url}


@router.delete("/agents/{agent_id}/connections/amazon/disconnect")
async def amazon_disconnect_for_agent(agent_id: str, user_id: str = Query(...)):
    """Disconnect Amazon SP-API (revoke OAuth only, preserve admin toggle)."""
    from app.admin.integrations import revoke_and_delete_amazon
    deleted = await revoke_and_delete_amazon(user_id, agent_id)
    return {"status": "ok", "deleted": deleted}


class ManageAdminRequest(BaseModel):
    user_id: str        # caller (must already be admin)
    target_user_id: str # user to add as admin


@router.post("/agents/{agent_id}/admins")
async def add_agent_admin(agent_id: str, req: ManageAdminRequest):
    """Add target_user_id to an agent's admin_users list. Caller must be an existing admin."""
    db = get_db()
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")
    added = await db.add_agent_admin(agent_id, req.target_user_id)
    roles = await db.get_agent_roles(agent_id)
    return {"admin_users": roles["admin_users"], "added": added}


@router.get("/agents/{agent_id}/members")
async def list_agent_members(agent_id: str, user_id: str = Query(...)):
    """
    List the agent's admin_users and member_users joined with profile + activity stats.
    Caller must be a global admin OR in the agent's admin_users list.
    Returns {"admins": [...], "members": [...]} where each entry has
    user_id, username, display_name, is_admin, is_approved, channel,
    last_login_at, created_at, session_count, interaction_count.
    """
    from app.auth.users import get_user_by_id as _auth_get_user_by_id

    db = get_db()
    if not await _is_agent_admin(db, agent_id, user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")

    agent_row = await db.get_agent_by_id(agent_id)
    if not agent_row:
        raise HTTPException(status_code=404, detail="Agent not found.")
    user_mode = agent_row.get("user_mode") or "anonymous"

    roles = await db.get_agent_roles(agent_id)
    admin_ids: list[str] = list(roles.get("admin_users") or [])
    member_ids: list[str] = list(roles.get("member_users") or [])
    authorized_set = set(roles.get("authorized_users") or [])
    all_ids = list(dict.fromkeys(admin_ids + member_ids))

    if not all_ids:
        return {"admins": [], "members": [], "user_mode": user_mode}

    conn = db._get_conn()
    profile_map: dict[str, dict] = {}
    identity_map: dict[str, dict] = {}
    session_counts: dict[str, int] = {}
    interaction_counts: dict[str, int] = {}
    try:
        placeholders = ",".join("?" * len(all_ids))
        for r in conn.execute(
            f"SELECT user_id, is_admin, created_at, last_login_at "
            f"FROM user_profiles WHERE user_id IN ({placeholders})",
            all_ids,
        ).fetchall():
            profile_map[r["user_id"]] = dict(r)
        for r in conn.execute(
            f"SELECT user_id, display_name, channel FROM channel_identities "
            f"WHERE user_id IN ({placeholders})",
            all_ids,
        ).fetchall():
            # Keep first/most-recent-ish; if multiple, last wins (fine for display)
            identity_map[r["user_id"]] = dict(r)
        for r in conn.execute(
            f"SELECT user_id, COUNT(*) AS n FROM sessions "
            f"WHERE agent_id = ? AND user_id IN ({placeholders}) GROUP BY user_id",
            [agent_id, *all_ids],
        ).fetchall():
            session_counts[r["user_id"]] = r["n"]
        for r in conn.execute(
            f"SELECT s.user_id AS user_id, COUNT(*) AS n FROM interactions i "
            f"JOIN sessions s ON s.id = i.session_id "
            f"WHERE s.agent_id = ? AND s.user_id IN ({placeholders}) "
            f"GROUP BY s.user_id",
            [agent_id, *all_ids],
        ).fetchall():
            interaction_counts[r["user_id"]] = r["n"]
    finally:
        conn.close()

    def _build(uid: str) -> dict:
        prof = profile_map.get(uid, {})
        ident = identity_map.get(uid, {})
        auth_user = _auth_get_user_by_id(uid)
        username = auth_user.username if auth_user else None
        display_name = (
            (auth_user.display_name if auth_user else None)
            or ident.get("display_name")
            or username
            or uid
        )
        return {
            "user_id": uid,
            "username": username,
            "display_name": display_name,
            "channel": ident.get("channel"),
            "is_admin": bool(prof.get("is_admin", 0)),
            "is_approved": bool(auth_user.is_approved) if auth_user else None,
            "created_at": prof.get("created_at"),
            "last_login_at": prof.get("last_login_at"),
            "session_count": session_counts.get(uid, 0),
            "interaction_count": interaction_counts.get(uid, 0),
            "is_authorized": uid in authorized_set,
        }

    admins = [_build(uid) for uid in admin_ids]
    members = [_build(uid) for uid in member_ids if uid not in set(admin_ids)]
    return {"admins": admins, "members": members, "user_mode": user_mode}


class _AuthorizeRequest(BaseModel):
    user_id: str   # caller (must be agent admin)


@router.post("/agents/{agent_id}/members/{target_user_id}/authorize")
async def authorize_agent_member(agent_id: str, target_user_id: str, req: _AuthorizeRequest):
    """Mark a user as authorized for this agent. Caller must be agent admin."""
    db = get_db()
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")
    authorized_users = await db.set_agent_authorized(agent_id, target_user_id, True)
    return {"authorized_users": authorized_users, "is_authorized": True}


@router.post("/agents/{agent_id}/members/{target_user_id}/restrict")
async def restrict_agent_member(agent_id: str, target_user_id: str, req: _AuthorizeRequest):
    """Remove a user from the authorized list for this agent. Caller must be agent admin."""
    db = get_db()
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")
    authorized_users = await db.set_agent_authorized(agent_id, target_user_id, False)
    return {"authorized_users": authorized_users, "is_authorized": False}


class _SetUserModeRequest(BaseModel):
    user_id: str   # caller (must be agent admin)
    user_mode: str # 'anonymous' | 'register' | 'authorized'


@router.post("/agents/{agent_id}/user-mode")
async def set_agent_user_mode(agent_id: str, req: _SetUserModeRequest):
    """Set the agent's user_mode policy. Caller must be agent admin."""
    if req.user_mode not in ("anonymous", "register", "authorized"):
        raise HTTPException(status_code=400, detail="Invalid user_mode.")
    db = get_db()
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")
    conn = db._get_conn()
    try:
        cursor = conn.execute(
            "UPDATE agents SET user_mode = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (req.user_mode, agent_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Agent not found.")
    finally:
        conn.close()
    return {"agent_id": agent_id, "user_mode": req.user_mode}


@router.post("/agents/{agent_id}/anon-session")
async def create_anon_session(agent_id: str, req: AnonSessionRequest):
    """
    Create an anonymous session for a public agent URL visitor.
    No JWT required. Returns a token so the visitor can chat.
    """
    import uuid as _uuid_mod
    db = get_db()

    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")

    mode = (agent.get("user_mode") or "anonymous")
    if mode in ("register", "authorized"):
        raise HTTPException(
            status_code=403,
            detail=("This agent requires a registered account."
                    if mode == "register"
                    else "This agent requires admin authorization. Sign in with an authorized account."),
        )

    browser_id = req.browser_id or _uuid_mod.uuid4().hex
    from app.communications.auth import get_or_create_identity
    identity = await get_or_create_identity(channel="web_public", external_id=browser_id)

    await db.add_agent_member(agent_id, identity.user_id)

    from app.auth.jwt import create_access_token
    token = create_access_token(username=identity.user_id, user_id=identity.user_id)

    return {
        "token": token,
        "user_id": identity.user_id,
        "session_id": identity.user_id,
    }


@router.post("/agent-templates/config")
async def set_template_discoverable(req: UpdateTemplateRequest):
    """
    Update admin-controlled fields on an agent template (admin only).
    Currently supports: discoverable.
    """
    db = get_db()
    await _require_admin(db, req.user_id)
    updates = {}
    if req.discoverable is not None:
        updates["discoverable"] = 1 if req.discoverable else 0
    if not updates:
        raise HTTPException(status_code=400, detail="No updatable fields provided.")
    updated = await db.update_agent_template_fields(template_id=req.template_id, updates=updates)
    if updated is None:
        raise HTTPException(status_code=404, detail="Template not found.")
    return {"template": _safe_agent(updated)}



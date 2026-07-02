"""
Agent management API.

Provides endpoints for listing, creating, updating, deleting, and configuring
user agents. Also exposes the user profile (including is_admin flag).

Endpoints
---------
GET  /api/v1/user/profile               — user profile + admin flag + tutorial prefs
PUT  /api/v1/user/tutorial-prefs        — persist tutorial walkthrough state (cross-device)
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

from fastapi import APIRouter, Header, HTTPException, Query, Request
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


class ReorderAgentsRequest(BaseModel):
    user_id: str
    order: List[str]  # agent ids, top-to-bottom (index 0 = top of the list)


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
    # Icon (stored in metadata['icon'] — the agents table has no icon column)
    icon: Optional[str] = None
    max_turn_count: Optional[int] = None
    max_wall_seconds: Optional[float] = None
    max_identical_tool_calls: Optional[int] = None
    max_stall_strikes: Optional[int] = None
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
    # Default chat execution mode for NEW sessions with this agent: 'ask' | 'plan'
    # | 'auto'. Stored in metadata['default_execution_mode']; the chat pill seeds a
    # fresh session from it (ui/chat-side-panel/js/chat-ui.js). Blank ⇒ 'ask'.
    default_execution_mode: Optional[str] = None
    # Per-agent LLM override (stored in metadata['llm_config'])
    llm_config: Optional[Dict[str, Any]] = None
    # Per-agent chat UI copy override — partial dict of _CHAT_UI_KEYS, merged
    # into metadata['chat_ui']. Blank value for a key clears it (use default).
    chat_ui: Optional[Dict[str, Any]] = None
    # Local Claude Code engine config — partial dict (folder/extra_flags/model/
    # act_freely/append_persona), shallow-merged into metadata['claude_code'].
    # Only meaningful on agents whose metadata.engine == "claude_code".
    claude_code: Optional[Dict[str, Any]] = None
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

# Built-in utility agents that should stay off the user's Agents page unless the
# "Show system agents" toggle is on. They are still fully usable/configurable.
_SYSTEM_UTILITY_IDS = {
    "agent-builder",        # Agent Manager
    "source-controller",    # GitHub agent
    "user-impersonator",    # Suggested Replies (placeholder) agent
    "admin-agent",
    "diagnostic-agent",
    "chat-panel-engineer",
    "codebase-engineer",
    "integration-admin-agent",
}


# The per-agent chat UI copy an agent admin can override. Each falls back to
# the app-wide default in app/defaults/app-prompts.json ("ui_messages.chat") when
# left blank. Edited on the agent's Config tab; stored in metadata["chat_ui"];
# consumed on the frontend by ui/shared/js/app-prompts.js (agentChatMsg).
_CHAT_UI_KEYS = (
    "welcome_bubble",
    "new_session_bubble",
    "switched_agent_bubble",
    "pill_placeholder",
    "pill_locked_placeholder",
)
_CHAT_UI_MAX_LEN = 2000


def _merge_chat_ui(existing, incoming) -> dict:
    """Merge an incoming partial chat_ui override into the stored one.

    Only the known keys are kept; values are coerced to strings capped at
    _CHAT_UI_MAX_LEN. A blank value clears that key, so the agent falls back to
    the app-wide default for it. Merging (rather than replacing) lets each
    Config-tab field save just its own key without clobbering the others.
    """
    out: dict = {}
    if isinstance(existing, dict):
        for k in _CHAT_UI_KEYS:
            v = existing.get(k)
            if isinstance(v, str) and v.strip():
                out[k] = v
    if isinstance(incoming, dict):
        for k in _CHAT_UI_KEYS:
            if k not in incoming:
                continue
            v = "" if incoming.get(k) is None else str(incoming.get(k))
            v = v[:_CHAT_UI_MAX_LEN]
            if v.strip():
                out[k] = v
            else:
                out.pop(k, None)
    return out


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
            # Guard against double-encoded JSON (a JSON string inside a JSON string).
            if isinstance(meta, str):
                meta = _json.loads(meta)
        except Exception:
            meta = {}
    result["llm_config"] = meta.get("llm_config") if isinstance(meta, dict) else {"use_default": True}
    # Per-agent chat UI copy override (welcome/system bubbles + message-box
    # hints). Empty dict ⇒ this agent uses the app-wide defaults from
    # app/defaults/app-prompts.json. See _merge_chat_ui / _CHAT_UI_KEYS.
    cu = meta.get("chat_ui") if isinstance(meta, dict) else None
    result["chat_ui"] = cu if isinstance(cu, dict) else {}
    # Alternate runtime engine (e.g. "claude_code") + its per-agent config. Absent
    # ⇒ a normal WebAgent-LLM agent. Drives the Config tab's "Claude Code" card and
    # the loop's engine dispatch (app/agent/loop.py stream_agent_events).
    result["engine"] = meta.get("engine") if isinstance(meta, dict) else None
    cc = meta.get("claude_code") if isinstance(meta, dict) else None
    result["claude_code"] = cc if isinstance(cc, dict) else {}
    tc = meta.get("terminal_chat") if isinstance(meta, dict) else None
    result["terminal_chat"] = tc if isinstance(tc, dict) else {}
    # Icon from metadata (the agents table has no icon column; custom agents store
    # it in metadata['icon']). Returns the raw string or empty string — the
    # frontend falls back to the default 'bot' icon when icon is falsy.
    result["icon"] = (meta.get("icon") or "") if isinstance(meta, dict) else ""
    # Default chat execution mode for NEW sessions ('ask' | 'plan' | 'auto'). Edited
    # on the Config tab and read by ui/chat-side-panel/js/chat-ui.js when seeding a
    # fresh session. When UNSET, agents cloned from the default WebAgent template
    # start in 'plan' (matches default.json metadata.default_execution_mode), so even
    # pre-existing default agents created before this field honour it; everything
    # else falls back to 'ask'. An explicit stored value always wins.
    _dem = meta.get("default_execution_mode") if isinstance(meta, dict) else ""
    if _dem not in ("ask", "plan", "auto"):
        _engine = meta.get("engine") if isinstance(meta, dict) else None
        if _engine == "claude_code":
            # Claude Code agents had only a binary act_freely toggle before this
            # field existed; surface its equivalent (True ⇒ Auto, False ⇒ Ask) so
            # the pill + Config selector match what the engine actually does until
            # the admin picks a mode. See engines/claude_code._resolve_permission_mode.
            _cc = meta.get("claude_code") if isinstance(meta.get("claude_code"), dict) else {}
            _dem = "auto" if _cc.get("act_freely", True) else "ask"
        elif agent.get("template_id") == "default":
            _dem = "plan"
        else:
            _dem = ""
    result["default_execution_mode"] = _dem
    # Derive a single ``system`` flag the agents page uses to keep utility agents
    # (Suggested Replies / user-impersonator, source-controller, Agent Manager,
    # etc.) off the user's list by default, behind a "Show system agents" toggle.
    if agent.get("source") == "custom":
        # A user's own agent is only "system" if it explicitly opts in via metadata.
        result["system"] = bool(meta.get("system_agent") or meta.get("hidden_from_user")) if isinstance(meta, dict) else False
    else:
        tid = (agent.get("id") or agent.get("template_id") or "")
        result["system"] = bool(agent.get("is_system")) or tid in _SYSTEM_UTILITY_IDS
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


async def _require_ability_enabled(db, agent_id: str, ability_id: str) -> None:
    """Raise 403 if the OAuth ability isn't enabled on this agent.

    Covers fine-grained capabilities in `agent_abilities` (distinct from the
    provider-level rows in `agent_connections`).
    """
    if not hasattr(db, "get_agent_abilities"):
        return
    rows = await db.get_agent_abilities(agent_id)
    match = next((r for r in rows if r.get("ability_id") == ability_id), None)
    if not match or not match.get("enabled"):
        raise HTTPException(status_code=403, detail=f"Ability '{ability_id}' is not enabled on this agent.")


# ── Routes ────────────────────────────────────────────────────────────────────

def _parse_tutorial_prefs(raw):
    """Parse the stored tutorial_prefs JSON blob into a dict; return None if absent/broken."""
    if not isinstance(raw, str) or not raw:
        return None
    import json as _json
    try:
        v = _json.loads(raw)
        return v if isinstance(v, dict) else None
    except Exception:
        return None


@router.get("/user/profile")
async def get_user_profile(request: Request, user_id: str = Query(...)):
    """Return user profile including is_admin flag and tutorial walkthrough state."""
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    profile = await db.get_user_profile(user_id)
    if not profile:
        # Return a safe default rather than 404 — profile is auto-created on first write
        return {
            "user_id": user_id,
            "is_admin": False,
            "default_agent_id": None,
            "tutorial_prefs": None,
        }
    return {
        "user_id": profile["user_id"],
        "is_admin": bool(profile.get("is_admin")),
        "default_agent_id": profile.get("default_agent_id"),
        "tutorial_prefs": _parse_tutorial_prefs(profile.get("tutorial_prefs")),
    }


class TutorialPrefsRequest(BaseModel):
    user_id: str
    prefs: Dict[str, Any]


@router.put("/user/tutorial-prefs")
async def update_tutorial_prefs(req: TutorialPrefsRequest, request: Request):
    """Persist the tutorial walkthrough state for this user (cross-device).

    Body: { user_id, prefs: { enabled: bool, currentStep: int } }
    """
    uid = await assert_caller_is(request, req.user_id)
    import json as _json
    db = get_db()
    await db.upsert_user_profile(uid, tutorial_prefs=_json.dumps(req.prefs))
    return {"tutorial_prefs": req.prefs}


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
async def list_agents(request: Request, user_id: str = Query(...), include_system: bool = Query(False),
                      view: str = Query("active")):
    """
    List the user's own agents (the agents they added themselves).

    By default only non-system agents the user administers are returned, so the
    Agents page stays clean. Pass ``include_system=true`` to ALSO include the
    built-in utility agents (Suggested Replies / user-impersonator, GitHub /
    source-controller, Agent Manager / agent-builder, etc.) — used by the
    "Show system agents" toggle. Each entry carries ``source`` ('custom' or
    'template') and a derived ``system`` boolean.

    New users with zero custom agents get the ``default`` template auto-provisioned
    as their first agent on first list, so the agent square appears immediately on
    the Agents page (matching the same logic the chat pill uses in sessions.js).
    """
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    bin_view = (view == "bin")
    clones_view = (view == "clones")
    is_admin = await db.is_user_admin(user_id)
    if clones_view:
        all_agents = await db.list_agents_for_user(user_id, include_admin=is_admin,
                                                   view="clones")
    else:
        all_agents = await db.list_agents_for_user(user_id, include_admin=is_admin,
                                                   view=("bin" if bin_view else "active"))
    out = []
    for a in all_agents:
        safe = _safe_agent(a)
        if a.get("source") == "custom":
            # The user's own agents — hide system-flagged ones unless asked for.
            if safe.get("system") and not include_system:
                continue
            out.append(safe)
        elif include_system and safe.get("system"):
            # Built-in utility templates, only when the toggle is on.
            out.append(safe)

    # ── Auto-provision default agent for new users ──────────────────────────
    # If the user has zero custom agents (no "out" entries from the custom path
    # above) and we aren't showing system agents, the Agents page would be
    # completely empty. Clone the default template same as the chat pill does
    # in sessions.js (ensureWebagentAgent), so the agent square is present from
    # the very first visit.
    if not out and not include_system and not bin_view and not clones_view:
        try:
            new_agent = await db.create_custom_agent(
                user_id=user_id,
                name="WebAgent",
                description="Your all-purpose WebAgent — chat, tools, web, browser, code, pages, and source control.",
                template_id="default",
            )
            if new_agent:
                safe = _safe_agent(new_agent)
                # Clear the cached shared data so downstream consumers pick it up
                safe["is_user_default"] = 1
                out.append(safe)
        except Exception as e:
            logger.warning("Auto-provision default agent for %s failed: %s", user_id, e)

    return {"agents": out}


@router.post("/agents/reorder")
async def reorder_agents(req: ReorderAgentsRequest, request: Request):
    """
    Persist the manual order of a user's custom agents (drag-to-reorder in the
    chat-header agent dropdown). Each id in ``order`` gets sort_order = its
    index; only agents the caller administers are touched. The same order is
    consumed by the Agents page so the two views stay in sync.
    """
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not hasattr(db, "reorder_agents"):
        raise HTTPException(status_code=501, detail="Reordering is not supported by the active storage backend.")
    updated = await db.reorder_agents(req.user_id, req.order)
    return {"success": True, "updated": updated}


@router.post("/agents")
async def create_agent(req: CreateAgentRequest, request: Request):
    """
    Create a new custom agent cloned from the default template.
    Returns the new agent with editable fields only.
    """
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
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
    safe = _safe_agent(agent)
    # Live-sync every open tab/device for this user so the Agents grid shows the
    # new agent without a manual refresh.
    from app.api.chat import notify_user
    await notify_user(req.user_id, {
        "type": "agent_created", "user_id": req.user_id, "agent": safe,
    })
    return {"agent": safe}


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
async def get_agent(request: Request, agent_id: str, user_id: str = Query(...)):
    """Get a single custom agent by id (must be owned by user)."""
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    # Check custom agents table
    agents = await db.list_agents_for_user(user_id)
    for a in agents:
        if a.get("id") == agent_id:
            return {"agent": _safe_agent(a)}
    raise HTTPException(status_code=404, detail="Agent not found.")


@router.put("/agents/{agent_id}")
async def update_agent(agent_id: str, req: UpdateAgentRequest, request: Request):
    """
    Update editable fields on a custom agent. Caller must be an agent admin.

    Two write lanes coexist on this endpoint:
      - Agent-row fields (name, model, allowed_tools, etc.) via `updates`.
      - Admin-base prompt slots via `slots` (full slot set — reconciled).
        Optional `reset_overrides_for` wipes per-user override rows for the
        listed slot_names at save time.
    """
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can edit this agent.")

    import json as _json
    payload = req.dict()
    slots_in = payload.pop("slots", None)
    reset_for = payload.pop("reset_overrides_for", None)
    icon_in = payload.pop("icon", None)
    llm_config_in = payload.pop("llm_config", None)
    chat_ui_in = payload.pop("chat_ui", None)
    claude_code_in = payload.pop("claude_code", None)
    terminal_chat_in = payload.pop("terminal_chat", None)
    exec_mode_in = payload.pop("default_execution_mode", None)
    # Normalize the default chat mode (accept legacy read/write aliases) and reject
    # anything else so the metadata only ever holds 'ask' | 'plan' | 'auto'.
    if exec_mode_in is not None:
        _m = str(exec_mode_in).strip().lower()
        _m = {"read": "plan", "write": "ask"}.get(_m, _m)
        if _m not in ("ask", "plan", "auto"):
            raise HTTPException(status_code=400, detail="Invalid default_execution_mode.")
        exec_mode_in = _m
    updates = {k: v for k, v in payload.items()
               if k not in ("user_id",) and v is not None}

    # Merge metadata-backed blobs (llm_config, chat_ui, icon, default chat mode)
    # into the agent's metadata. All are read-modify-write against the current
    # metadata so they don't clobber each other or the rest of the blob.
    if (llm_config_in is not None or chat_ui_in is not None or claude_code_in is not None
            or terminal_chat_in is not None
            or icon_in is not None or exec_mode_in is not None):
        current = await db.get_agent_by_id(agent_id)
        meta = {}
        if current:
            meta_raw = current.get("metadata")
            if isinstance(meta_raw, str):
                try:
                    meta = _json.loads(meta_raw)
                except Exception:
                    pass
            elif isinstance(meta_raw, dict):
                meta = dict(meta_raw)
        if llm_config_in is not None:
            meta["llm_config"] = llm_config_in
        if chat_ui_in is not None:
            meta["chat_ui"] = _merge_chat_ui(meta.get("chat_ui"), chat_ui_in)
        if claude_code_in is not None:
            # Shallow-merge so saving one field (e.g. just the folder) keeps the rest.
            _cc = meta.get("claude_code")
            _cc = dict(_cc) if isinstance(_cc, dict) else {}
            _cc.update(claude_code_in)
            meta["claude_code"] = _cc
        if terminal_chat_in is not None:
            # Shallow-merge so saving one field (e.g. just the command) keeps the rest.
            _tc = meta.get("terminal_chat")
            _tc = dict(_tc) if isinstance(_tc, dict) else {}
            _tc.update(terminal_chat_in)
            meta["terminal_chat"] = _tc
        if icon_in is not None:
            # Store icon in metadata — the agents table has no dedicated icon column.
            # An empty/blank string means "clear the icon" (falls back to default).
            meta["icon"] = icon_in.strip() or ""
        if exec_mode_in is not None:
            meta["default_execution_mode"] = exec_mode_in
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
    # The `__skills__` slot is managed via the dedicated skills endpoints, not
    # the generic slot editor — hide it here so it doesn't show as raw JSON.
    slots = [s for s in slots if s.get("slot_name") != "__skills__"]
    if is_template:
        # Templates have no per-agent admin role — only global admins may edit.
        is_admin = await db.is_user_admin(user_id)
    else:
        is_admin = await _is_agent_admin(db, agent_id, user_id)
    return {"slots": slots, "user_role": "admin" if is_admin else "member"}


@router.get("/agents/{agent_id}/skills")
async def get_agent_skills_ep(request: Request, agent_id: str, user_id: str = Query(...)):
    """Return the agent's on-demand skills (from its `__skills__` prompt slot)
    plus whether the caller may edit them."""
    user_id = await assert_caller_is(request, user_id)
    db = get_db()
    skills = await db.get_agent_skills(agent_id)
    try:
        is_admin = await _is_agent_admin(db, agent_id, user_id)
    except Exception:
        is_admin = await db.is_user_admin(user_id)
    return {"skills": skills, "user_role": "admin" if is_admin else "member"}


class UpdateSkillsRequest(BaseModel):
    user_id: str
    skills: List[Dict[str, Any]]


@router.put("/agents/{agent_id}/skills")
async def put_agent_skills_ep(agent_id: str, req: UpdateSkillsRequest, request: Request):
    """Replace the agent's full skills list (admin-only). Persists to the
    `__skills__` prompt slot in agent_prompts."""
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can edit skills.")
    saved = await db.set_agent_skills(agent_id, req.skills, updated_by=f"admin:{req.user_id}")
    return {"skills": saved}


@router.get("/agents/{agent_id}/my-prompts")
async def get_my_prompts(request: Request, agent_id: str, user_id: str = Query(...)):
    """Return the caller's prompt slots with per-user override map.

    Shape matches what the frontend prompt-slots panel expects:
    ``{slots, overrides, user_role}``.
    """
    user_id = await assert_caller_is(request, user_id)
    db = get_db()
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")
    slots = await db.list_slots(agent_id, user_id=user_id)
    slots = [s for s in slots if s.get("slot_name") != "__skills__"]
    overrides: Dict[str, str] = {}
    for s in slots:
        oc = s.get("override_content")
        if oc is not None:
            overrides[s["slot_name"]] = oc
        del s["override_content"]
    is_admin = await _is_agent_admin(db, agent_id, user_id)
    return {"slots": slots, "overrides": overrides, "user_role": "admin" if is_admin else "member"}


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
    target_device: Optional[str] = None      # device instance-id to run on; '' clears it (run locally)
    target_offline: Optional[str] = None      # 'wait' | 'skip' when the target is offline at fire time


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
    if body.target_device is not None:
        # '' / whitespace clears the target (run on the firing device locally).
        fields["target_device"] = body.target_device.strip() or None
    if body.target_offline is not None:
        fields["target_offline"] = "skip" if body.target_offline == "skip" else "wait"
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
    # Clean up any dedicated clone agent this automation created
    if row.get("runner_agent_id"):
        try:
            conn = db._get_conn()
            if conn:
                conn.execute("DELETE FROM agents WHERE id = ? AND status = 'clone'", (row["runner_agent_id"],))
                conn.commit()
                conn.close()
        except Exception as e:
            logger.warning("Failed to clean up clone agent %s: %s", row["runner_agent_id"], e)

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

    # Clean up clone agent if this subscription had one
    if row.get("runner_agent_id"):
        try:
            conn = db._get_conn()
            try:
                conn.execute("DELETE FROM agents WHERE id = ? AND status = 'clone'", (row["runner_agent_id"],))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("Failed to clean up clone agent %s: %s", row.get("runner_agent_id"), e)

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
async def delete_agent(request: Request, agent_id: str, user_id: str = Query(...),
                       permanent: bool = Query(False)):
    """
    Recycling-bin delete for a custom agent. Cannot touch system agents.

    Default (``permanent=false``): SOFT delete — the agent moves to the bin
    (status -> 'trashed') and disappears from the Agents page, but keeps its
    prompts, sessions and transcripts so it can be restored.

    ``permanent=true``: HARD delete — used by the bin's own trash button. Erases
    the agent and everything that belongs to it (prompts, sessions, transcripts,
    connections, abilities). This is irreversible.
    """
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    if permanent:
        deleted = await db.delete_custom_agent(agent_id=agent_id, user_id=user_id)
    else:
        deleted = await db.trash_custom_agent(agent_id=agent_id, user_id=user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Agent not found, not owned by this user, or is a system agent.")
    # Live-sync every open tab/device: a permanent delete drops the card from the
    # bin view; a soft delete moves it out of the main grid into the bin.
    from app.api.chat import notify_user
    await notify_user(user_id, {
        "type": "agent_deleted" if permanent else "agent_trashed",
        "user_id": user_id, "agent_id": agent_id, "permanent": permanent,
    })
    return {"deleted": True, "agent_id": agent_id, "permanent": permanent}


@router.post("/agents/{agent_id}/restore")
async def restore_agent(request: Request, agent_id: str, user_id: str = Query(...)):
    """Restore a trashed agent from the recycling bin back to the Agents page."""
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    restored = await db.restore_custom_agent(agent_id=agent_id, user_id=user_id)
    if not restored:
        raise HTTPException(status_code=404, detail="Agent not found in the bin, or not owned by this user.")
    # Live-sync: the agent leaves the bin and reappears on the main grid.
    from app.api.chat import notify_user
    await notify_user(user_id, {
        "type": "agent_restored", "user_id": user_id, "agent_id": agent_id,
    })
    return {"restored": True, "agent_id": agent_id}


@router.post("/agents/test")
async def test_agent(req: TestAgentRequest, request: Request):
    """
    Run a sample message through an agent configuration and return the response.
    Used by the Agent Management panel test sandbox — does NOT create a persistent session.
    The agent's prompts are used as-is from its current saved state.
    """
    import uuid as _uuid_mod
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)

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
        from app.db import get_db as _get_db
        _conn = _get_db()._get_conn()
        try:
            _rows = _conn.execute(
                "SELECT id, session_id, role, content, tool_name, metadata, created_at "
                "FROM interactions WHERE session_id = ? ORDER BY created_at ASC",
                (test_session_id,),
            ).fetchall()
            interactions = [dict(r) for r in _rows]
        finally:
            _conn.close()
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
    # ── Integrations · Productivity ──
    {"connection_type": "google",    "section": "integration", "display_name": "Google",           "status": "available"},
    {"connection_type": "microsoft", "section": "integration", "display_name": "Microsoft 365",    "status": "available"},
    {"connection_type": "yahoo",     "section": "integration", "display_name": "Yahoo",            "status": "available"},
    {"connection_type": "dropbox",   "section": "integration", "display_name": "Dropbox",          "status": "available"},
    {"connection_type": "notion",        "section": "integration", "display_name": "Notion",        "status": "coming_soon"},
    {"connection_type": "airtable",      "section": "integration", "display_name": "Airtable",      "status": "coming_soon"},
    {"connection_type": "google_sheets", "section": "integration", "display_name": "Google Sheets", "status": "coming_soon"},
    # ── Integrations · Developer ──
    {"connection_type": "github",    "section": "integration", "display_name": "GitHub",           "status": "coming_soon"},
    {"connection_type": "gitlab",    "section": "integration", "display_name": "GitLab",           "status": "coming_soon"},
    {"connection_type": "jira",      "section": "integration", "display_name": "Jira / Linear",    "status": "coming_soon"},
    # ── Integrations · CRM & Email ──
    {"connection_type": "hubspot",    "section": "integration", "display_name": "HubSpot",         "status": "coming_soon"},
    {"connection_type": "salesforce", "section": "integration", "display_name": "Salesforce",      "status": "coming_soon"},
    {"connection_type": "mailchimp",  "section": "integration", "display_name": "Mailchimp",       "status": "coming_soon"},
    # ── Integrations · Payments ──
    {"connection_type": "stripe",    "section": "integration", "display_name": "Stripe",           "status": "coming_soon"},
    {"connection_type": "paypal",    "section": "integration", "display_name": "PayPal",           "status": "coming_soon"},
    {"connection_type": "square",    "section": "integration", "display_name": "Square",           "status": "coming_soon"},
    {"connection_type": "bank",      "section": "integration", "display_name": "Bank Accounts",    "status": "coming_soon"},
    {"connection_type": "search",    "section": "integration", "display_name": "Search Engine",    "status": "coming_soon"},
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
    # DROP-IN: the host-ability rows are injected below from plugins/abilities/
    # (see app.abilities). Do NOT hardcode abilities here — drop a file in that
    # folder instead. (Web Scraper / Browser Cookies are now ordinary drop-in
    # abilities under plugins/abilities/Web/, supplied via _inject_ability_rows.)
]


def _inject_ability_rows() -> None:
    """Append the discovered host-ability rows to the connection catalog.

    Each row carries its UI metadata (description/icon/color/group/simple) so the
    two ability panels render generically. Fail-open: a scan error just leaves
    the catalog without ability rows rather than breaking the connections API.

    ⚠ DROP-IN POLICY — abilities are discovered from plugins/abilities/ here; do
    NOT hardcode a new ability anywhere in this file. Drop a file in
    plugins/abilities/ and it appears in the catalog + both panels automatically.
    See CLAUDE.md "Core vs. plugins".
    """
    try:
        from app.abilities import connection_rows
        existing = {c["connection_type"] for c in _CONNECTION_CATALOG}
        for row in connection_rows():
            if row["connection_type"] not in existing:
                _CONNECTION_CATALOG.append(row)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Could not inject ability rows from plugins/abilities: %s", e)


_inject_ability_rows()


@router.get("/abilities/catalog")
async def get_abilities_catalog():
    """Render-time metadata for the two ability panels (admin Agent Settings +
    per-agent Abilities tab). Pure static catalogue (no per-agent data), built
    from the drop-in files in plugins/abilities/ — so both panels render
    generically with no hardcoded per-ability constants.
    """
    try:
        from app.abilities import ui_catalog, reload
        # Re-scan plugins/abilities/ on each catalog fetch (once per page load) so
        # a freshly-dropped or freshly-edited <id>.json descriptor appears with no
        # server restart — the discovery scan is a handful of small dir reads.
        # Mirrors the pages catalog above; keeps the drop-in story honest (edit a
        # descriptor, reload the page, see the change) instead of "stale until
        # restart".
        reload()
        return ui_catalog()
    except Exception as e:
        logger.warning("Could not build abilities catalog: %s", e)
        return {"groups": [], "abilities": {}, "credential_members": []}


@router.get("/pages/catalog")
async def get_pages_catalog():
    """Render-time metadata for the app shell's main header tabs and the Admin
    Tools sidebar views. Built from the drop-in ``page.json`` descriptors under
    ui/ and ui/admin-tools/, merged with the admin's order/label/icon/hidden
    overrides in data/config/main-panel-pages.json + admin-panel-pages.json — so
    the shell renders generically with no hardcoded per-page constants.
    """
    try:
        from app import ui_pages
        from app.admin import page_config
        # Re-scan the ui/ folders on each catalog fetch (once per page load) so a
        # freshly-dropped ui/<page>/page.json folder appears with no server
        # restart — the discovery scan is a handful of small dir reads. Also drop
        # the override cache so external edits to the *-panel-pages.json files
        # (git pull, manual edit) are honoured; the admin write endpoints keep the
        # cache in sync on their own.
        ui_pages.reload()
        page_config.reload()
        return ui_pages.ui_catalog()
    except Exception as e:
        logger.warning("Could not build pages catalog: %s", e)
        return {"main": [], "admin": []}


@router.get("/abilities/{ability_id}/config-schema")
async def get_ability_config_schema(ability_id: str):
    """Return the per-ability config schema (the companion .json file beside
    the .py plugin) so the frontend can render per-ability settings rows.
    Returns 404 when the ability has no config schema."""
    try:
        from app.abilities import ability_config_schema
        schema = ability_config_schema(ability_id)
        # For each setting that declares a `ceiling` rule, attach the admin's
        # current APP-LEVEL value as `ceiling_value` — the GLOBAL MAXIMUM the
        # per-agent tree must clamp to (boolean lock / number cap). These are
        # non-secret knobs, so it's safe on this public schema endpoint; the admin
        # table ignores it. Falls back to the field default when the admin never
        # set it. See app/admin/ability_config.effective_ability_config.
        if isinstance(schema, dict) and isinstance(schema.get("settings"), list):
            try:
                from app.admin import ability_config as _abcfg
                admin_vals = _abcfg.get_ability_config(ability_id)
            except Exception:
                admin_vals = {}
            # COPY before annotating — `ability_config_schema` hands back the
            # cached descriptor's settings list by reference, so mutating a field
            # in place would pollute the shared catalog across requests.
            schema = dict(schema)
            new_settings = []
            for field in schema["settings"]:
                if isinstance(field, dict) and field.get("ceiling"):
                    field = dict(field)
                    field["ceiling_value"] = admin_vals.get(field.get("key"), field.get("default"))
                new_settings.append(field)
            schema["settings"] = new_settings
        # Return null rather than 404 so browsers don't log this as a console
        # error. Frontends check for null instead of relying on status code.
        return schema or None
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Could not load ability config schema for %s: %s", ability_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/abilities/{ability_id}/config")
async def get_ability_config_values(
    ability_id: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Return the stored APP-LEVEL (admin scope) non-secret config values for an
    ability, as ``{ability_settings: {key: value}}`` — so the admin Agent Tools
    panel can pre-fill the fields with what was previously saved. Empty map when
    nothing has been saved yet. Backed by app/admin/ability_config.py."""
    from app.admin import ability_config as _abcfg
    from app.admin.integrations import resolve_user_id
    db = get_db()
    user_id = resolve_user_id(authorization or "", token or "")
    await _require_admin(db, user_id)
    # Auto-seed: make sure the repo-local config file exists (created/seeded from
    # the vault on first access) before we read, so a missing file self-heals
    # rather than returning stale emptiness.
    try:
        await _abcfg.ensure_bootstrapped(db)
    except Exception as e:
        logger.debug("ability_config ensure_bootstrapped (get) skipped: %s", e)
    return {"ability_settings": _abcfg.get_ability_config(ability_id)}


class AbilityConfigRequest(BaseModel):
    """Save payload for an ability's non-secret APP-LEVEL config knobs (admin
    scope). ``ability_settings`` is a flat ``{key: value}`` map matching the
    ability's config-schema fields."""
    ability_settings: Dict[str, Any] = {}


@router.put("/abilities/{ability_id}/config")
async def save_ability_config_values(
    ability_id: str,
    req: AbilityConfigRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Persist an ability's APP-LEVEL (admin scope) non-secret config values into
    the repo-local ``data/config/agent-abilities.json`` (NOT the vault — these are
    non-secret knobs). The file is auto-created/seeded if missing, and the change
    is written immediately. Backed by app/admin/ability_config.py
    ``set_ability_config``."""
    from app.admin import ability_config as _abcfg
    from app.admin.integrations import resolve_user_id
    db = get_db()
    user_id = resolve_user_id(authorization or "", token or "")
    await _require_admin(db, user_id)
    # Auto-seed before writing: ensures the config file exists with its other
    # sections (abilities/tools/order) intact, so saving config never clobbers a
    # not-yet-loaded file. Self-heals if the file was deleted.
    try:
        await _abcfg.ensure_bootstrapped(db)
    except Exception as e:
        logger.debug("ability_config ensure_bootstrapped (put) skipped: %s", e)
    settings = req.ability_settings if isinstance(req.ability_settings, dict) else {}
    _abcfg.set_ability_config(ability_id, settings)
    return {"status": "ok", "ability_settings": _abcfg.get_ability_config(ability_id)}


class AbilityCredentialsRequest(BaseModel):
    """Save payload for an ability's declarative credentials. ``values`` carries
    one entry per declared field; a blank secret field leaves it unchanged."""
    values: Dict[str, Any] = {}
    agent_id: Optional[str] = None


@router.get("/abilities/{ability_id}/credentials")
async def get_ability_credentials(
    ability_id: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Return the ability's credential fields + non-secret values + which secrets
    are set (NEVER the secret values). Returns null when the ability declares no
    ``credentials`` block. The caller is resolved from the Bearer token. See
    app/abilities/credentials.py."""
    from app.abilities import credentials as _creds
    from app.admin.integrations import resolve_user_id
    spec = _creds.ability_credentials_spec(ability_id)
    if not spec:
        return None
    user_id = resolve_user_id(authorization or "", token or "")
    db = get_db()
    try:
        is_admin = bool(user_id) and await db.is_user_admin(user_id)
    except Exception:
        is_admin = False
    view = await _creds.public_view(ability_id, user_id=user_id)
    if view is not None:
        # admin-scope secrets are editable only by an admin; user/agent scope is
        # the caller's own.
        view["can_edit"] = bool(is_admin) or spec.get("scope") != "admin"
    return view


@router.post("/abilities/{ability_id}/credentials")
async def save_ability_credentials(
    ability_id: str,
    req: AbilityCredentialsRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Persist an ability's credentials into the encrypted vault (generic)."""
    from app.abilities import credentials as _creds
    from app.admin.integrations import resolve_user_id, ANONYMOUS_KEY
    spec = _creds.ability_credentials_spec(ability_id)
    if not spec:
        raise HTTPException(status_code=404, detail="This ability has no credentials to configure.")
    user_id = resolve_user_id(authorization or "", token or "")
    db = get_db()
    scope = spec.get("scope", "user")
    if scope == "admin":
        await _require_admin(db, user_id)
    elif not user_id or user_id == ANONYMOUS_KEY:
        raise HTTPException(status_code=401, detail="Sign in to save these credentials.")
    ok = await _creds.save_credentials(
        ability_id, req.values or {}, user_id=user_id, agent_id=req.agent_id or "",
    )
    return {"status": "ok" if ok else "error",
            "configured": await _creds.is_configured(ability_id, user_id=user_id, agent_id=req.agent_id or "")}


@router.delete("/abilities/{ability_id}/credentials")
async def delete_ability_credentials(
    ability_id: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
    agent_id: Optional[str] = Query(None),
):
    """Clear an ability's stored credentials."""
    from app.abilities import credentials as _creds
    from app.admin.integrations import resolve_user_id
    spec = _creds.ability_credentials_spec(ability_id)
    if not spec:
        raise HTTPException(status_code=404, detail="This ability has no credentials to configure.")
    user_id = resolve_user_id(authorization or "", token or "")
    db = get_db()
    if spec.get("scope") == "admin":
        await _require_admin(db, user_id)
    ok = await _creds.delete_credentials(ability_id, user_id=user_id, agent_id=agent_id or "")
    return {"status": "ok" if ok else "error"}


@router.get("/agents/{agent_id}/connections")
async def get_agent_connections(request: Request, agent_id: str, user_id: str = Query(...)):
    """
    Return connections for an agent, filtered to those the admin has configured.
    Unconfigured providers are hidden so they can't be toggled on from the UI.
    Bot tokens in config are masked to last 4 chars.
    For Google: merges user's auth_elements status (email, name, picture).
    """
    import json as _json
    from app.admin.integrations import get_admin_configured_providers
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    rows = await db.get_agent_connections(agent_id)
    saved = {r["connection_type"]: r for r in rows}

    # Per-agent ability + skill visibility (visible / discoverable) — surfaced on
    # each ability connection so the Abilities tab can render its eye toggles.
    try:
        _ability_modes = await db.get_agent_ability_modes(agent_id)
    except Exception:
        _ability_modes = {}
    try:
        _skill_modes = await db.get_agent_skill_modes(agent_id)
    except Exception:
        _skill_modes = {}
    # Per-agent DEFAULT visibility — the fallback applied to an ability with no
    # explicit per-ability choice, so the Abilities tab shows each ability's TRUE
    # effective mode (discoverable for a discovery-default agent) rather than a
    # misleading "visible".
    try:
        _discovery_default = await db.get_agent_discovery_default(agent_id)
    except Exception:
        _discovery_default = None
    # Per-agent ability ACCESS level (everyone / registered / admin) — surfaced on
    # each ability connection so the Abilities tab can render its "Available to"
    # control. Absent → everyone (no restriction).
    try:
        _ability_access = await db.get_agent_ability_access(agent_id)
    except Exception:
        _ability_access = {}

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

        # A locked-on safety ability (e.g. Context Control) is always enabled —
        # its toggle is fixed ON and cannot be turned off — regardless of whether
        # a per-agent row exists or what it says.
        _locked_on = bool(entry.get("locked_on"))
        item = {
            **entry,
            "enabled": True if _locked_on else (bool(row["enabled"]) if row else False),
            "config": config,
        }

        # Ability + bundled-skill visibility (per-agent discovery).
        if entry.get("section") == "ability":
            from app.tools.tool_modes import resolve_ability_mode, resolve_skill_mode, resolve_ability_access
            item["ability_mode"] = resolve_ability_mode(ct, _ability_modes, _discovery_default)
            item["available_to"] = resolve_ability_access(ct, _ability_access)
            try:
                from app.abilities import ability_feature_with_skill
                _feat = ability_feature_with_skill(ct)
            except Exception:
                _feat = None
            if _feat:
                item["has_skill"] = True
                item["skill_summary"] = _feat.get("skill_summary") or ""
                item["skill_mode"] = resolve_skill_mode(ct, _skill_modes, _feat.get("skill_mode"))
            else:
                item["has_skill"] = False

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
    request: Request,
):
    """
    Create or update a connection on an agent.
    Accepts enabled flag and arbitrary config dict.
    If bot_token in config starts with bullets (masked), preserve existing token.
    """
    import json as _json
    db = get_db()

    req.user_id = await assert_caller_is(request, req.user_id)
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

    # A locked-on safety ability cannot be disabled — coerce any disable attempt
    # back to enabled so the row stays on no matter what the client sends.
    if catalog_entry.get("locked_on") and not req.enabled:
        req.enabled = True

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

    # Newly-enabled ability → default its tools to "discoverable" (hybrid rule).
    # Only sets tools that have no explicit per-tool mode yet, so prior choices
    # survive. No-op for abilities that provide no built-in tools.
    if req.enabled:
        try:
            from app.tools.tool_modes import tools_for_ability
            _seed = tools_for_ability(connection_type)
            if _seed:
                await db.seed_agent_tool_modes(agent_id, _seed, "discoverable")
        except Exception as _se:
            logger.debug("tool-mode seed (connection %s) failed: %s", connection_type, _se)

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


class SetToolModeRequest(BaseModel):
    user_id: str
    mode: str  # "always" | "discoverable" | (anything else clears the override)


class SetAbilityAccessRequest(BaseModel):
    user_id: str
    level: str  # "everyone" | "registered" | "admin" | (anything else → everyone)


@router.get("/agents/{agent_id}/tools")
async def list_agent_tools(request: Request, agent_id: str, user_id: str = Query(...)):
    """Return the agent's actual loaded tool set with each tool's exposure mode.

    Drives the per-agent Tools panel. The list comes from the real tool loader
    (after ability gating), so it matches exactly what the agent can use. Each
    entry carries: name, description, ability (the gating ability or null),
    the resolved mode (core / always / discoverable), locked (core tools), and
    whether its full schema is sent by default.
    """
    import json as _json
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")

    from app.tools.loader import load_tools, TIER_1_ALWAYS_ON
    from app.tools.tool_modes import (
        resolve_mode, is_locked, is_sent, ability_for_tool,
    )
    from app.tools.tool_defaults import resolve_permission
    # Display-only grouping for always-on core tools (no ability gates them, but a
    # "virtual" row like Core ▸ Base lists them). Kept separate from
    # ability_for_tool so these tools are never withheld by ability gating.
    from app.abilities import virtual_ability_for_tool as _virtual_ability_for_tool

    # App-wide global per-tool defaults — so the panel shows the EFFECTIVE
    # inherited mode/permission (agent-override ▸ global ▸ built-in), not just
    # the agent's own settings. Empty {} when unset.
    try:
        from app.admin.integrations import get_global_tool_defaults
        global_defaults = await get_global_tool_defaults()
    except Exception:
        global_defaults = {}

    allowed = agent.get("allowed_tools")
    if isinstance(allowed, str):
        try:
            allowed = _json.loads(allowed)
        except Exception:
            allowed = []

    try:
        tools = await load_tools(
            user_id,
            agent_id=agent_id,
            agent_template_id=agent.get("template_id"),
            allowed_tools=allowed or [],
            session_id="",
        )
    except Exception as e:
        logger.warning("list_agent_tools: load_tools failed for %s: %s", agent_id, e)
        tools = {}

    # ≈token estimator (the same char-based heuristic that drives the chat
    # context gauge) — so the "see schema" preview can show each tool's cost in
    # each visibility mode without a heavyweight tokenizer.
    try:
        from app.agent.context_control import estimate_tokens as _est
    except Exception:
        def _est(_msgs):
            return 0

    modes = await db.get_agent_tool_modes(agent_id)
    out: list[dict] = []
    for name, info in sorted(tools.items()):
        try:
            desc = (info.handler.__doc__ or "").strip().split("\n")[0]
        except Exception:
            desc = ""
        params = info.parameters if hasattr(info, "parameters") else {}
        # "visible" = the full tool definition the model receives every turn;
        # "discoverable" = just the one-line # [TOOLS] index entry.
        try:
            _vis_text = _json.dumps({"name": name, "description": desc, "parameters": params})
        except Exception:
            _vis_text = name
        _disc_text = f"- `{name}` — {desc}" if desc else f"- `{name}`"
        # The GLOBAL ceiling (admin per-tool default) — the floor of strictness
        # this agent can't go below: Auto < Ask < Deny. A global "deny" on a
        # Tier-1 always-on tool can't actually bind (it's never denied), so its
        # ceiling shows as "auto". The per-agent UI greys out any looser option.
        _g_perm = (global_defaults.get(name) or {}).get("permission")
        if _g_perm == "deny" and name in TIER_1_ALWAYS_ON:
            _ceiling = "auto"
        elif _g_perm in ("auto", "ask", "deny"):
            _ceiling = _g_perm
        else:
            _ceiling = "auto"
        # A tool that inherently pauses for confirmation can't be shown — or set —
        # looser than "Ask". Fold that built-in floor into the ceiling (take the
        # stricter of the admin global limit and this floor). Auto < Ask < Deny.
        if getattr(info, "requires_confirmation", False):
            _rank = {"auto": 0, "ask": 1, "deny": 2}
            if _rank.get(_ceiling, 0) < 1:
                _ceiling = "ask"
        out.append({
            "name": name,
            "description": desc,
            # Real gating ability first; fall back to a display-only virtual row
            # (Core ▸ Base) so always-on core tools still group under a heading.
            "ability": ability_for_tool(name) or _virtual_ability_for_tool(name),
            "mode": resolve_mode(name, modes, global_defaults),
            "permission": resolve_permission(name, agent, global_defaults),
            "ceiling": _ceiling,
            "locked": is_locked(name),
            "sent": is_sent(name, modes, [], global_defaults),
            "destructive": bool(getattr(info, "destructive", False)
                                or getattr(info, "requires_confirmation", False)),
            "schema": params,
            "tokens_visible": _est([{"content": _vis_text}]),
            "tokens_discoverable": _est([{"content": _disc_text}]),
        })
    is_admin = await _is_agent_admin(db, agent_id, user_id)
    return {"tools": out, "count": len(out),
            "user_role": "admin" if is_admin else "member"}


@router.put("/agents/{agent_id}/tools/{tool_name}")
async def set_agent_tool_mode_endpoint(
    agent_id: str, tool_name: str, req: SetToolModeRequest, request: Request,
):
    """Set how a tool is exposed to the agent: 'always' (full schema every turn)
    or 'discoverable' (load on demand). Any other value clears the override back
    to the default. Core tools cannot be changed."""
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can change tool settings.")
    from app.tools.tool_modes import is_locked, resolve_mode
    if is_locked(tool_name):
        raise HTTPException(
            status_code=400,
            detail=f"'{tool_name}' is a core tool — it is always available and cannot be changed.",
        )
    modes = await db.set_agent_tool_mode(agent_id, tool_name, req.mode)
    return {"tool": {"name": tool_name, "mode": resolve_mode(tool_name, modes)}}


@router.put("/agents/{agent_id}/abilities/{ability_id}/visibility")
async def set_agent_ability_visibility(
    agent_id: str, ability_id: str, req: SetToolModeRequest, request: Request,
):
    """Set how an ability is exposed to the agent: 'visible' (its tools + how-to
    skill are shown now) or 'discoverable' (collapsed to one # [ABILITIES] entry;
    the agent reveals it with load_ability). Any other value clears the override
    back to the 'visible' default. Per-agent only — admins set the ceiling
    (enable + permission), agents tune discovery for their role."""
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can change ability settings.")
    from app.tools.tool_modes import resolve_ability_mode
    modes = await db.set_agent_ability_mode(agent_id, ability_id, req.mode)
    return {"ability": {"id": ability_id, "mode": resolve_ability_mode(ability_id, modes)}}


@router.put("/agents/{agent_id}/abilities/{ability_id}/access")
async def set_agent_ability_access(
    agent_id: str, ability_id: str, req: SetAbilityAccessRequest, request: Request,
):
    """Set an ability's required caller-access level on this agent: 'everyone'
    (anyone, incl. anonymous guests — the default), 'registered' (signed-in
    accounts only), or 'admin' (admins only). 'everyone' (or any unknown value)
    clears the restriction. Enforced server-side at tool-assembly time
    (app/tools/loader.load_tools via app/agent/ability_access) — the gated
    ability's tools are never built for a caller below the level, so it's a real
    boundary, not a prompt hint. Per-agent only."""
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can change ability settings.")
    from app.tools.tool_modes import resolve_ability_access
    access = await db.set_agent_ability_access(agent_id, ability_id, req.level)
    return {"ability": {"id": ability_id, "available_to": resolve_ability_access(ability_id, access)}}


@router.put("/agents/{agent_id}/abilities/{ability_id}/skill-visibility")
async def set_agent_skill_visibility(
    agent_id: str, ability_id: str, req: SetToolModeRequest, request: Request,
):
    """Set how an ability's bundled **skill** is exposed to the agent: 'visible'
    (its how-to body is shown every turn) or 'discoverable' (load on demand via
    load_skill). Any other value clears the override back to the descriptor
    default. Per-agent."""
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can change skill settings.")
    from app.tools.tool_modes import resolve_skill_mode
    modes = await db.set_agent_skill_mode(agent_id, ability_id, req.mode)
    return {"skill": {"ability_id": ability_id, "mode": resolve_skill_mode(ability_id, modes)}}


@router.get("/agents/{agent_id}/abilities/{ability_id}/schema-preview")
async def ability_schema_preview(request: Request, agent_id: str, ability_id: str, user_id: str = Query(...)):
    """The ACTUAL text the agent receives for one ability, given its current
    visibility config — so the Abilities tab can show exactly what's sent:

    - ability **discoverable** → just the one-line `# [ABILITIES]` entry (hidden);
    - ability **visible**, every tool+skill visible → the **complete** schema;
    - ability **visible**, mixed → the real mix (visible tools' full schemas +
      discoverable tools' one-line entries; skill body vs its load-on-demand line).

    Returns `{state: hidden|partial|full, text, tokens}` (≈tokens via the same
    char heuristic as the chat gauge).
    """
    import json as _json
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")

    from app.tools.tool_modes import (
        resolve_mode, resolve_ability_mode, resolve_skill_mode, VISIBLE,
    )
    from app.tools.loader import load_tools, ABILITY_TOOLS
    from app.abilities import ui_catalog, ability_feature_with_skill
    try:
        from app.agent.context_control import estimate_tokens as _est
    except Exception:
        def _est(_m):
            return 0

    ab_modes = await db.get_agent_ability_modes(agent_id)
    sk_modes = await db.get_agent_skill_modes(agent_id)
    tool_modes = await db.get_agent_tool_modes(agent_id)
    try:
        from app.admin.integrations import get_global_tool_defaults
        gdef = await get_global_tool_defaults()
    except Exception:
        gdef = {}

    meta = (ui_catalog() or {}).get("abilities", {}).get(ability_id, {})
    display = meta.get("display_name") or ability_id
    summary = meta.get("skill_summary") or meta.get("description") or ""

    # Discoverable ability → the agent only sees the one-line [ABILITIES] entry.
    if resolve_ability_mode(ability_id, ab_modes) != VISIBLE:
        text = f"# [ABILITIES]\n- `{ability_id}` — {display}: {summary}".strip()
        return {"state": "hidden", "text": text, "tokens": _est([{"content": text}])}

    # Visible ability → compose the real tool + skill text.
    allowed = agent.get("allowed_tools")
    if isinstance(allowed, str):
        try:
            allowed = _json.loads(allowed)
        except Exception:
            allowed = []
    try:
        tools = await load_tools(
            user_id, agent_id=agent_id, agent_template_id=agent.get("template_id"),
            allowed_tools=allowed or [], session_id="",
        )
    except Exception:
        tools = {}

    parts: list[str] = []
    n_visible = 0
    n_total = 0
    for name in [n for n in ABILITY_TOOLS.get(ability_id, []) if n in tools]:
        info = tools.get(name)
        desc = ((info.handler.__doc__ or "").strip().split("\n")[0]
                if info and hasattr(info, "handler") else "")
        n_total += 1
        if resolve_mode(name, tool_modes, gdef) == VISIBLE:
            n_visible += 1
            params = info.parameters if (info and hasattr(info, "parameters")) else {}
            parts.append(_json.dumps({"name": name, "description": desc, "parameters": params}, indent=2))
        else:
            parts.append(f"- `{name}` — {desc}")

    feat = ability_feature_with_skill(ability_id)
    if feat:
        n_total += 1
        if resolve_skill_mode(ability_id, sk_modes, feat.get("skill_mode")) == VISIBLE:
            n_visible += 1
            parts.append(f"# [SKILL: {display}]\n{(feat.get('skill') or '').strip()}".strip())
        else:
            parts.append(f"# [SKILLS]\n- {display} (load on demand): {feat.get('skill_summary') or summary}".strip())

    text = "\n\n".join(p for p in parts if p).strip() or "(this ability exposes no tools or skill)"
    if n_total == 0:
        state = "full"
    elif n_visible == 0:
        state = "hidden"
    elif n_visible == n_total:
        state = "full"
    else:
        state = "partial"
    return {"state": state, "text": text, "tokens": _est([{"content": text}])}


class ManageAdminRequest(BaseModel):
    user_id: str        # caller (must already be admin)
    target_user_id: str # user to add as admin


@router.post("/agents/{agent_id}/admins")
async def add_agent_admin(agent_id: str, req: ManageAdminRequest, request: Request):
    """Add target_user_id to an agent's admin_users list. Caller must be an existing admin."""
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")
    added = await db.add_agent_admin(agent_id, req.target_user_id)
    roles = await db.get_agent_roles(agent_id)
    return {"admin_users": roles["admin_users"], "added": added}


@router.get("/agents/{agent_id}/members")
async def list_agent_members(request: Request, agent_id: str, user_id: str = Query(...)):
    """
    List the agent's admin_users and member_users joined with profile + activity stats.
    Caller must be a global admin OR in the agent's admin_users list.
    Returns {"admins": [...], "members": [...]} where each entry has
    user_id, username, display_name, is_admin, is_approved, channel,
    last_login_at, created_at, session_count, interaction_count.
    """
    from app.auth.users import get_user_by_id as _auth_get_user_by_id

    db = get_db()
    user_id = await assert_caller_is(request, user_id)
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
async def authorize_agent_member(agent_id: str, target_user_id: str, req: _AuthorizeRequest, request: Request):
    """Mark a user as authorized for this agent. Caller must be agent admin."""
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")
    authorized_users = await db.set_agent_authorized(agent_id, target_user_id, True)
    return {"authorized_users": authorized_users, "is_authorized": True}


@router.post("/agents/{agent_id}/members/{target_user_id}/restrict")
async def restrict_agent_member(agent_id: str, target_user_id: str, req: _AuthorizeRequest, request: Request):
    """Remove a user from the authorized list for this agent. Caller must be agent admin."""
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")
    authorized_users = await db.set_agent_authorized(agent_id, target_user_id, False)
    return {"authorized_users": authorized_users, "is_authorized": False}


class _SetUserModeRequest(BaseModel):
    user_id: str   # caller (must be agent admin)
    user_mode: str # 'anonymous' | 'register' | 'authorized'


@router.post("/agents/{agent_id}/user-mode")
async def set_agent_user_mode(agent_id: str, req: _SetUserModeRequest, request: Request):
    """Set the agent's user_mode policy. Caller must be agent admin."""
    if req.user_mode not in ("anonymous", "register", "authorized"):
        raise HTTPException(status_code=400, detail="Invalid user_mode.")
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
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


# ── Per-agent OAuth Abilities (3-tier OAuth system) ──────────────────────

class UpsertAbilityRequest(BaseModel):
    user_id: str                                # caller (must be agent admin)
    enabled: Optional[bool] = None              # toggle on/off
    source:  Optional[str]  = None              # "platform" | "byo"


class SetByoCredsRequest(BaseModel):
    user_id: str
    client_id: str
    client_secret: str


def _is_known_ability(ability_id: str) -> bool:
    from app.integrations.ability_registry import get_ability
    return get_ability(ability_id) is not None


@router.get("/agents/{agent_id}/abilities")
async def list_agent_abilities(request: Request, agent_id: str, user_id: str = Query(...)):
    """Return every registered OAuth ability with its enabled/source/byo state for this agent.

    Merges three layers in one response so the agent admin UI can render the
    full picture in a single fetch:
      - registry definition (display name, scopes, provider)
      - app-admin policy (mode, scope caps)
      - per-agent state (enabled flag, source, BYO client id [secret masked])

    Read-only members get the same payload — server-side mutation gates kick
    in on the PUT endpoints below.
    """
    from app.integrations.ability_registry import ABILITIES
    from app.admin.integrations import get_all_oauth_ability_configs
    db = get_db()
    user_id = await assert_caller_is(request, user_id)
    rows = (await db.get_agent_abilities(agent_id)) if hasattr(db, "get_agent_abilities") else []
    by_id = {r["ability_id"]: r for r in rows}

    # Batch-fetch all ability admin policies in a single query instead of
    # calling get_oauth_ability_config N times (N = ~58 abilities).
    all_configs = await get_all_oauth_ability_configs()

    out: list[dict] = []
    for ab in ABILITIES.values():
        row = by_id.get(ab.id) or {}
        policy = all_configs.get(ab.id, {
            "mode": "platform_only",
            "platform_scopes": list(ab.scopes),
            "max_byo_scopes": list(ab.scopes),
        })
        byo_cid = row.get("byo_client_id", "") or ""
        # Mask BYO client_id to last 6 chars so members never see real values.
        masked_cid = ("•" * max(0, len(byo_cid) - 6) + byo_cid[-6:]) if byo_cid else ""
        out.append({
            "id": ab.id,
            "provider": ab.provider,
            "display_name": ab.display_name,
            "description": ab.description,
            "scopes": list(ab.scopes),
            "implicit": ab.implicit,
            "mode": policy["mode"],
            "enabled": bool(row.get("enabled", False)),
            "source": row.get("source") or "platform",
            "byo_client_id_masked": masked_cid,
            "byo_configured": bool(row.get("byo_client_id") and row.get("byo_client_secret_ref")),
        })
    is_admin = await _is_agent_admin(db, agent_id, user_id)
    return {"abilities": out, "user_role": "admin" if is_admin else "member"}


@router.put("/agents/{agent_id}/abilities/{ability_id}")
async def upsert_agent_ability(
    agent_id: str,
    ability_id: str,
    req: UpsertAbilityRequest,
    request: Request,
):
    """Toggle an ability on/off and/or switch source (platform vs BYO).

    On enable, returns `reauth_required: true` plus a fresh authorize URL when
    the agent's existing token doesn't already cover this ability's scopes —
    the UI prompts the user to re-consent before the agent can use the new
    capability.
    """
    if not _is_known_ability(ability_id):
        raise HTTPException(status_code=404, detail=f"Unknown ability: {ability_id}")
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can change abilities.")

    from app.admin.integrations import get_oauth_ability_config
    from app.integrations.ability_registry import get_ability

    ab = get_ability(ability_id)
    policy = await get_oauth_ability_config(ability_id)
    # Enforce app-admin mode policy.
    if req.enabled and policy["mode"] == "disabled":
        raise HTTPException(status_code=400, detail=f"Ability '{ability_id}' is disabled by the app admin.")
    new_source = req.source
    if new_source is None:
        new_source = "platform"
    if new_source not in ("platform", "byo"):
        raise HTTPException(status_code=400, detail="source must be 'platform' or 'byo'")
    if new_source == "platform" and policy["mode"] == "byo_only":
        raise HTTPException(status_code=400, detail="App admin requires BYO for this ability.")
    if new_source == "byo" and policy["mode"] == "platform_only":
        raise HTTPException(status_code=400, detail="App admin disallows BYO for this ability.")

    if not hasattr(db, "upsert_agent_ability"):
        raise HTTPException(status_code=500, detail="Backend does not support agent_abilities yet.")
    row = await db.upsert_agent_ability(
        agent_id, ability_id,
        enabled=req.enabled,
        source=new_source,
    )

    # Newly-enabled ability → default its tools to "discoverable" (hybrid rule).
    # No-op for OAuth abilities that provide no built-in tools.
    if req.enabled:
        try:
            from app.tools.tool_modes import tools_for_ability
            _seed = tools_for_ability(ability_id)
            if _seed:
                await db.seed_agent_tool_modes(agent_id, _seed, "discoverable")
        except Exception as _se:
            logger.debug("tool-mode seed (ability %s) failed: %s", ability_id, _se)

    # Check whether the existing token already covers this ability — if not,
    # build a fresh authorize URL so the UI can prompt re-consent.
    reauth_required = False
    authorize_url = None
    if req.enabled and ab is not None:
        try:
            from app.integrations.oauth_helper import check_ability_authorized
            authorized = await check_ability_authorized(req.user_id, agent_id, ab.provider, ability_id)
            if not authorized:
                reauth_required = True
                authorize_url = await _build_authorize_url_for(ab.provider, req.user_id, agent_id, source=new_source)
        except Exception as e:
            logger.debug("re-auth check failed for %s: %s", ability_id, e)

    return {
        "ability": {
            "id": ability_id,
            "enabled": bool(row.get("enabled")) if row else False,
            "source": row.get("source") if row else new_source,
        },
        "reauth_required": reauth_required,
        "authorize_url": authorize_url,
    }


@router.put("/agents/{agent_id}/abilities/{ability_id}/byo-creds")
async def set_agent_ability_byo_creds(
    agent_id: str,
    ability_id: str,
    req: SetByoCredsRequest,
    request: Request,
):
    """Set the agent admin's BYO OAuth client id + secret for this ability."""
    if not _is_known_ability(ability_id):
        raise HTTPException(status_code=404, detail=f"Unknown ability: {ability_id}")
    db = get_db()
    req.user_id = await assert_caller_is(request, req.user_id)
    if not await _is_agent_admin(db, agent_id, req.user_id):
        raise HTTPException(status_code=403, detail="Only agent admins can set BYO credentials.")
    cid = (req.client_id or "").strip()
    csec = (req.client_secret or "").strip()
    if not cid or not csec:
        raise HTTPException(status_code=400, detail="client_id and client_secret are required.")
    if not hasattr(db, "upsert_agent_ability"):
        raise HTTPException(status_code=500, detail="Backend does not support agent_abilities yet.")
    await db.upsert_agent_ability(
        agent_id, ability_id,
        source="byo", byo_client_id=cid, byo_client_secret_ref=csec,
    )
    return {"status": "ok"}


async def _build_authorize_url_for(
    provider: str, user_id: str, agent_id: str, *, source: str = "platform",
    request: Optional[Request] = None,
) -> Optional[str]:
    """Centralized authorize-URL builder used by ability re-consent flow."""
    from app.admin import integrations as ints
    p = (provider or "").lower()
    if p == "google":
        return await ints.build_google_authorize_url(user_id, agent_id, request, source=source)
    if p == "microsoft":
        return await ints.build_microsoft_authorize_url(user_id, agent_id, request, source=source)
    if p == "yahoo":
        return await ints.build_yahoo_authorize_url(user_id, agent_id, request, source=source)
    if p == "dropbox":
        return await ints.build_dropbox_authorize_url(user_id, agent_id, request, source=source)
    if p == "meta":
        return await ints.build_meta_authorize_url(user_id, agent_id, request, source=source)
    if p == "twitter":
        url, _ = await ints.build_twitter_authorize_url(user_id, agent_id, request, source=source)
        return url
    if p == "linkedin":
        return await ints.build_linkedin_authorize_url(user_id, agent_id, request, source=source)
    if p == "tiktok":
        return await ints.build_tiktok_authorize_url(user_id, agent_id, request, source=source)
    if p == "pinterest":
        return await ints.build_pinterest_authorize_url(user_id, agent_id, request, source=source)
    if p == "reddit":
        return await ints.build_reddit_authorize_url(user_id, agent_id, request, source=source)
    if p == "snapchat":
        return await ints.build_snapchat_authorize_url(user_id, agent_id, request, source=source)
    if p == "twitch":
        return await ints.build_twitch_authorize_url(user_id, agent_id, request, source=source)
    if p == "ebay":
        return await ints.build_ebay_authorize_url(user_id, agent_id, request, source=source)
    if p == "etsy":
        return await ints.build_etsy_authorize_url(user_id, agent_id, request, source=source)
    if p == "amazon":
        return await ints.build_amazon_authorize_url(user_id, "NA", agent_id, request, source=source)
    # shopify needs a shop domain — caller has to use the per-provider authorize endpoint.
    return None


# ── Per-agent OAuth landing routes + homepage stub ────────────────────────

# Routes outside the `/api/v1` prefix — these are user-facing URLs registered
# on the global app router, mounted from app/main.py. Anchored to the agent's
# stable homepage `/agents/{id}` so the visualizer can take it over later.

from fastapi.responses import RedirectResponse

# Separate router — these URLs (e.g. `/agents/{id}`) live outside the
# `/api/v1` prefix used by the main agents router above.
agent_pages_router = APIRouter()


@agent_pages_router.get("/agents/{agent_id}")
async def agent_homepage(agent_id: str):
    """Agent homepage stub. 302s to the agent detail view in the main app.

    The visualizer integration will eventually render the agent UI at this
    URL directly; for now we keep the URL stable and bounce users into the
    SPA so deep-links still work.
    """
    return RedirectResponse(url=f"/#agent/{agent_id}", status_code=302)


@agent_pages_router.get("/agents/{agent_id}/oauth/callback/{provider}")
async def per_agent_oauth_landing(agent_id: str, provider: str, request: Request):
    """Per-agent OAuth landing URL.

    For platform OAuth flows (Phase 2), the provider redirects to the standard
    `/api/v1/oauth/callback/{provider}` URI directly — this route is reserved
    for BYO flows where the agent admin has registered THIS URI in their own
    OAuth project (Phase 3). On hit, we forward to the standard callback with
    the same query string so the existing token-exchange logic picks up.
    """
    target = f"/api/v1/oauth/callback/{provider}"
    qs = request.url.query
    if qs:
        target = f"{target}?{qs}"
    return RedirectResponse(url=target, status_code=307)


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



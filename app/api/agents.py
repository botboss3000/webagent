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
POST /api/v1/agents/{agent_id}/set-default — set as the user's default agent
GET  /api/v1/agents/templates           — list agent templates (for tool breakdown display)
POST /api/v1/agents/test                — run a test message through an agent config
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["agents"])


# ── Request / Response models ─────────────────────────────────────────────────

class CreateAgentRequest(BaseModel):
    user_id: str
    name: str
    description: Optional[str] = ""
    template_id: Optional[str] = "default"


class UpdateAgentRequest(BaseModel):
    user_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    max_turn_count: Optional[int] = None
    agent_prompt: Optional[str] = None
    user_prompt: Optional[str] = None
    skills_prompt: Optional[str] = None
    tasks_prompt: Optional[str] = None
    misc_prompt: Optional[str] = None
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


class UpdateTemplateRequest(BaseModel):
    user_id: str
    template_id: str
    discoverable: Optional[bool] = None


class UpsertConnectionRequest(BaseModel):
    user_id: str
    enabled: bool
    config: Optional[Dict[str, Any]] = None


class AnonSessionRequest(BaseModel):
    browser_id: Optional[str] = None


class SetDefaultRequest(BaseModel):
    user_id: str


class TestAgentRequest(BaseModel):
    user_id: str
    agent_id: str          # template id (e.g. 'default') or custom agents.id UUID
    message: str
    session_id: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_agent(agent: dict) -> dict:
    """Strip locked/internal fields before returning to client."""
    HIDDEN = {"system_prompt", "bootstrap_tools", "provider", "metadata"}
    result = {k: v for k, v in agent.items() if k not in HIDDEN}
    # Deserialize JSON list fields so the client receives actual arrays
    import json as _json
    for field in ("allowed_tools", "custom_tool_ids", "loop_logic", "admin_users", "member_users"):
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
    return {"agent": _safe_agent(agent)}


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
    Update editable fields on a custom agent.
    Only name, description, max_turn_count, and the five context prompts
    may be changed. System prompt and bootstrap_tools are locked.
    """
    db = get_db()
    updates = {k: v for k, v in req.dict().items()
               if k not in ("user_id",) and v is not None}
    updated = await db.update_agent_fields(
        agent_id=agent_id,
        user_id=req.user_id,
        updates=updates,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Agent not found or not owned by this user.")
    if any(k in updates for k in ("trigger_type", "trigger_key")):
        from app.agent import trigger_index
        trigger_index.build()
    return {"agent": _safe_agent(updated)}


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, user_id: str = Query(...)):
    """Delete a custom agent. Cannot delete system agents."""
    db = get_db()
    deleted = await db.delete_custom_agent(agent_id=agent_id, user_id=user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Agent not found, not owned by this user, or is a system agent.")
    return {"deleted": True, "agent_id": agent_id}


@router.post("/agents/{agent_id}/set-default")
async def set_default_agent(agent_id: str, req: SetDefaultRequest):
    """
    Set the user's default agent.
    agent_id may be a template id (e.g. 'default') or a custom agents.id UUID.
    Only agents with can_be_default=true may be set as default.
    """
    db = get_db()
    # Verify the agent exists and can_be_default
    agents = await db.list_agents_for_user(req.user_id, include_admin=await db.is_user_admin(req.user_id))
    target = next((a for a in agents if a.get("id") == agent_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Agent not found or not accessible to this user.")
    if not target.get("can_be_default", True):
        raise HTTPException(
            status_code=400,
            detail=f"Agent '{target.get('name', agent_id)}' cannot be set as the default agent.",
        )
    await db.set_user_default_agent(req.user_id, agent_id)
    return {"default_agent_id": agent_id}


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

    # Build a minimal system prompt from the agent's context columns
    from app.db.local import _agent_prompt_to_docs
    from app.agent.prompts import build_system_prompt

    test_session_id = req.session_id or f"test-{str(_uuid_mod.uuid4())[:8]}"

    # Assemble context docs from agent columns
    context_docs = _agent_prompt_to_docs(target)

    system_prompt = await build_system_prompt(
        agent=target,
        context_docs=context_docs,
        user_id=req.user_id,
        session_id=test_session_id,
        db=db,
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
]


@router.get("/agents/{agent_id}/connections")
async def get_agent_connections(agent_id: str, user_id: str = Query(...)):
    """
    Return all connections for an agent, merged with the full catalog.
    Available connection types include stubs for coming-soon entries.
    Bot tokens in config are masked to last 4 chars.
    For Google: merges user's auth_elements status (email, name, picture).
    """
    import json as _json
    db = get_db()
    rows = await db.get_agent_connections(agent_id)
    saved = {r["connection_type"]: r for r in rows}

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
    }
    # Cache fetched service records to avoid double-fetching (e.g. meta for both fb/ig)
    _service_cache: dict[str, dict] = {}
    provider_auth: dict[str, dict] = {}
    for ct, service_key in _OAUTH_PROVIDERS.items():
        try:
            if service_key not in _service_cache:
                elem = await db.auth_element_get(user_id, service_key, "oauth")
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
    deleted = await revoke_and_delete_google(user_id)
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
    deleted = await revoke_and_delete_microsoft(user_id)
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
    deleted = await revoke_and_delete_yahoo(user_id)
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
    deleted = await revoke_and_delete_dropbox(user_id)
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
    deleted = await revoke_and_delete_meta(user_id)
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
    deleted = await revoke_and_delete_meta(user_id)
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
    deleted = await revoke_and_delete_twitter(user_id)
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
    deleted = await revoke_and_delete_linkedin(user_id)
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
    deleted = await revoke_and_delete_tiktok(user_id)
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
    deleted = await revoke_and_delete_pinterest(user_id)
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
    deleted = await revoke_and_delete_reddit(user_id)
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
    deleted = await revoke_and_delete_snapchat(user_id)
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
    deleted = await revoke_and_delete_twitch(user_id)
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



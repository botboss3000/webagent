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

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["agents"])


# ── Request / Response models ─────────────────────────────────────────────────

class CreateAgentRequest(BaseModel):
    user_id: str
    name: str
    description: Optional[str] = ""


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
    HIDDEN = {"system_prompt", "bootstrap_tools", "provider", "max_tokens", "metadata"}
    return {k: v for k, v in agent.items() if k not in HIDDEN}


async def _require_admin(db, user_id: str) -> None:
    """Raise 403 if user is not an admin."""
    if not await db.is_user_admin(user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")


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
):
    """
    List agent templates (system agents, is_pipeline=0).
    Used by the Agent Management panel tool-breakdown display.
    If include_admin=true, requires the user to be an admin.
    """
    db = get_db()
    if include_admin:
        await _require_admin(db, user_id)
    templates = await db.list_agent_templates(include_admin=include_admin)
    return {"templates": [_safe_agent(t) for t in templates]}


@router.get("/agents")
async def list_agents(user_id: str = Query(...)):
    """
    List agents from the agents table that are assigned to the user
    (user_id = this user or owner_user_id = this user).
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
    )
    return {"agent": _safe_agent(agent)}


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str, user_id: str = Query(...)):
    """Get a single custom agent by id (must be owned by user)."""
    db = get_db()
    # Check custom agents table
    agents = await db.list_agents_for_user(user_id)
    for a in agents:
        if a.get("id") == agent_id or a.get("user_id") == agent_id:
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
        owner_user_id=req.user_id,
        updates=updates,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="Agent not found or not owned by this user.")
    return {"agent": _safe_agent(updated)}


@router.delete("/agents/{agent_id}")
async def delete_agent(agent_id: str, user_id: str = Query(...)):
    """Delete a custom agent. Cannot delete system agents."""
    db = get_db()
    deleted = await db.delete_custom_agent(agent_id=agent_id, owner_user_id=user_id)
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

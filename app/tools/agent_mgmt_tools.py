"""In-process agent-management tools for the Agent Manager.

These replace the old "manage agents via the REST API over http_request"
approach, which forced the agent to make HTTP calls against the very server
it runs inside (localhost:8080) — a self-referential loop that broke whenever
the server was busy or restarting.

Every tool here:
  - runs in-process (direct DB calls, no HTTP, no self-calling),
  - is scoped to the calling user via the harness-injected ``user_id`` — the
    model never passes an identity and cannot act on another user's agents,
  - enforces ownership in code (not by prompt instruction): writes go through
    DB methods that filter on the agent's ``admin_users`` membership.

Gated by the ``agent_management`` ability (a pure behavioural toggle — no
platform secret). Deliberately carries NO conversation-history (interactions)
or filesystem access; those stay under the heavier ``codebase_admin`` ability.

Data domains:
  | Domain                  | Access      | Tool(s)                                 |
  |-------------------------|-------------|-----------------------------------------|
  | agent_templates         | read        | list_agent_templates                    |
  | agent_prompt_templates  | read        | list_agent_templates(template_id=...)   |
  | agents (own only)       | read/write  | list_my_agents, get_agent, create_agent, update_agent |
  | agent_prompts (own)     | read/write  | edit_agent_prompt                       |
  | agent abilities (own)   | read/write  | set_agent_ability                       |
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _ok(**kw) -> str:
    return json.dumps({"status": "ok", **kw}, default=str)


def _err(message: str, **kw) -> str:
    return json.dumps({"status": "error", "message": message, **kw}, default=str)


async def _owns_agent(db, user_id: str, agent_id: str) -> bool:
    """True if the user is a global admin OR an admin of this agent.

    Mirrors app/api/agents.py::_is_agent_admin so the tool boundary matches the
    REST boundary exactly. Ownership is the gate for every write.
    """
    try:
        if await db.is_user_admin(user_id):
            return True
        roles = await db.get_agent_roles(agent_id)
        return user_id in (roles.get("admin_users") or [])
    except Exception as e:
        logger.warning("ownership check failed for agent %s / user %s: %s", agent_id, user_id, e)
        return False


def _read_template_slots(db, template_id: str) -> List[dict]:
    """Read the canonical (admin-base) prompt slots for a template, read-only.

    Uses the backend's portable connection (works on both SQLite and Postgres
    via pg_portable). agent_prompt_templates is read-only here — the Agent
    Manager may inspect canonical slot content but never edit the templates.
    """
    conn = db._get_conn()
    try:
        rows = conn.execute(
            """SELECT slot_name, order_index, lock, merge_mode, content, version, source
               FROM agent_prompt_templates
               WHERE template_id = ?
               ORDER BY order_index ASC""",
            (template_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── Templates (read-only) ─────────────────────────────────────────────────────

async def list_agent_templates(
    template_id: Optional[str] = None,
    include_admin: bool = False,
    user_id: str = "",
) -> str:
    """List the agent templates available to build new agents from.

    Without ``template_id`` → returns the catalogue of templates (id, name,
    description, model, icon). With ``template_id`` → also returns that
    template's canonical prompt slots (read-only) so you can see what an agent
    cloned from it will start with.
    """
    try:
        from app.db import get_db
        db = get_db()
        # Only platform admins may see admin-only templates.
        allow_admin = include_admin and await db.is_user_admin(user_id)
        templates = await db.list_agent_templates(include_admin=allow_admin)
        slim = [
            {
                "id": t.get("id"),
                "name": t.get("name"),
                "description": t.get("description"),
                "model": t.get("model"),
                "icon": t.get("icon"),
                "access_level": t.get("access_level"),
            }
            for t in templates
        ]
        if template_id:
            match = next((t for t in slim if t["id"] == template_id), None)
            if not match:
                return _err(f"Template '{template_id}' not found or not visible to you.")
            slots = _read_template_slots(db, template_id)
            return _ok(template=match, slots=slots, slot_count=len(slots))
        return _ok(count=len(slim), templates=slim)
    except Exception as e:
        logger.error("list_agent_templates failed: %s", e)
        return _err(str(e))


# ── Agents (read) ─────────────────────────────────────────────────────────────

async def list_my_agents(user_id: str = "") -> str:
    """List the agents this user owns (plus visible system agents).

    Each entry carries a ``source`` of 'custom' (the user's own, editable) or
    'template' (system, read-only).
    """
    try:
        from app.db import get_db
        db = get_db()
        is_admin = await db.is_user_admin(user_id)
        agents = await db.list_agents_for_user(user_id, include_admin=is_admin)
        slim = [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "description": a.get("description"),
                "template_id": a.get("template_id"),
                "model": a.get("model"),
                "provider": a.get("provider"),
                "source": a.get("source"),
            }
            for a in agents
        ]
        return _ok(count=len(slim), agents=slim)
    except Exception as e:
        logger.error("list_my_agents failed: %s", e)
        return _err(str(e))


async def get_agent(agent_id: str, user_id: str = "") -> str:
    """Get one agent the user can see, with its prompt slots and enabled abilities.

    Read-only. The agent must be owned by or visible to this user.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not agent_id:
            return _err("agent_id is required.")
        # Visibility: it must appear in the user's agent list.
        is_admin = await db.is_user_admin(user_id)
        visible = await db.list_agents_for_user(user_id, include_admin=is_admin)
        if not any(a.get("id") == agent_id for a in visible):
            return _err("Agent not found or not visible to you.")

        full = await db.fetch_agent_by_id_with_context(agent_id, user_id=user_id)
        if not full:
            return _err("Agent not found.")
        slots = await db.list_slots(agent_id, user_id=user_id)
        try:
            conns = await db.get_agent_connections(agent_id)
            abilities = [c["connection_type"] for c in conns
                         if c.get("section") == "ability" and c.get("enabled")]
        except Exception:
            abilities = []
        agent = {
            "id": full.get("id"),
            "name": full.get("name"),
            "description": full.get("description"),
            "template_id": full.get("template_id"),
            "model": full.get("model"),
            "provider": full.get("provider"),
            "temperature": full.get("temperature"),
            "max_tokens": full.get("max_tokens"),
            "max_turn_count": full.get("max_turn_count"),
        }
        return _ok(agent=agent, abilities=abilities,
                   slots=[{"slot_name": s["slot_name"], "order_index": s["order_index"],
                           "lock": s["lock"], "content": s["content"]} for s in slots])
    except Exception as e:
        logger.error("get_agent failed: %s", e)
        return _err(str(e))


# ── Agents (write) ────────────────────────────────────────────────────────────

async def create_agent(
    name: str,
    template_id: str = "default",
    description: str = "",
    user_id: str = "",
) -> str:
    """Create a new agent owned by this user, cloned from a template.

    Pick ``template_id`` from list_agent_templates. The new agent's config and
    prompt slots are copied from that template; you can refine them afterwards
    with update_agent / edit_agent_prompt / set_agent_ability.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not name or not name.strip():
            return _err("Agent name is required.")
        agent = await db.create_custom_agent(
            user_id=user_id,
            name=name.strip(),
            description=description or "",
            template_id=template_id or "default",
        )
        return _ok(agent={
            "id": agent.get("id"),
            "name": agent.get("name"),
            "description": agent.get("description"),
            "template_id": agent.get("template_id"),
        })
    except Exception as e:
        logger.error("create_agent failed: %s", e)
        return _err(str(e))


async def update_agent(
    agent_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    max_turn_count: Optional[int] = None,
    user_id: str = "",
) -> str:
    """Update editable fields on one of the user's own agents.

    Only the supplied fields change. Ownership is enforced: the update silently
    affects nothing (and returns an error) if the agent isn't yours.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not agent_id:
            return _err("agent_id is required.")
        if not await _owns_agent(db, user_id, agent_id):
            return _err("You can only edit agents you own.")
        updates: Dict[str, Any] = {}
        for k, v in (
            ("name", name), ("description", description), ("model", model),
            ("temperature", temperature), ("max_tokens", max_tokens),
            ("max_turn_count", max_turn_count),
        ):
            if v is not None:
                updates[k] = v
        if not updates:
            return _err("No fields to update were provided.")
        updated = await db.update_agent_fields(agent_id=agent_id, user_id=user_id, updates=updates)
        if updated is None:
            return _err("Agent not found or not owned by you.")
        return _ok(agent={"id": updated.get("id"), "name": updated.get("name"),
                          "description": updated.get("description"),
                          "model": updated.get("model")})
    except Exception as e:
        logger.error("update_agent failed: %s", e)
        return _err(str(e))


# ── Prompts (read/write, own agents) ──────────────────────────────────────────

async def edit_agent_prompt(
    action: str,
    agent_id: str,
    slot_name: Optional[str] = None,
    content: Optional[str] = None,
    lock: Optional[bool] = None,
    merge_mode: Optional[str] = None,
    order_index: Optional[int] = None,
    user_id: str = "",
) -> str:
    """Read or edit the prompt slots of one of the user's own agents.

    Actions:
      list   → list all admin-base slots for the agent
      get    → get one slot by slot_name
      insert → create a new slot (slot_name + content required)
      update → replace a slot's content (slot_name + content required)
      delete → delete a slot by slot_name

    Reads require the agent to be visible to you; writes require you to own it.
    This generalises the older single-agent db_query to any agent you own.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not agent_id:
            return _err("agent_id is required.")

        writing = action in ("insert", "update", "delete")
        if writing:
            if not await _owns_agent(db, user_id, agent_id):
                return _err("You can only edit prompts on agents you own.")
        else:
            is_admin = await db.is_user_admin(user_id)
            visible = await db.list_agents_for_user(user_id, include_admin=is_admin)
            if not any(a.get("id") == agent_id for a in visible):
                return _err("Agent not found or not visible to you.")

        if action == "list":
            slots = await db.list_slots(agent_id, user_id=user_id)
            return _ok(count=len(slots), slots=slots)

        if action == "get":
            if not slot_name:
                return _err("slot_name required for get.")
            slots = await db.list_slots(agent_id, user_id=user_id)
            slot = next((s for s in slots if s["slot_name"] == slot_name), None)
            if not slot:
                return _err(f"Slot '{slot_name}' not found.")
            return _ok(slot=slot)

        if action in ("insert", "update"):
            if not slot_name or content is None:
                return _err("slot_name and content are required.")
            existing = await db.list_slots(agent_id)
            current = next((s for s in existing if s["slot_name"] == slot_name), None)
            resolved_order = order_index if order_index is not None else (
                current["order_index"] if current
                else (max((s["order_index"] or 0) for s in existing) + 10 if existing else 10)
            )
            resolved_lock = lock if lock is not None else (current["lock"] if current else False)
            resolved_mode = merge_mode if merge_mode is not None else (
                current.get("merge_mode") if current else "replace")
            await db.upsert_slot(
                agent_id=agent_id,
                slot_name=slot_name,
                order_index=int(resolved_order),
                lock=bool(resolved_lock),
                merge_mode=resolved_mode,
                content=content,
                updated_by=f"agent_mgmt:{user_id}",
            )
            return _ok(slot_name=slot_name)

        if action == "delete":
            if not slot_name:
                return _err("slot_name required for delete.")
            n = await db.delete_slot(agent_id, slot_name)
            return _ok(slot_name=slot_name, deleted_rows=n)

        return _err(f"Unknown action '{action}'. Use: list, get, insert, update, delete.")
    except Exception as e:
        logger.error("edit_agent_prompt failed: %s", e)
        return _err(str(e))


# ── Abilities (read/write, own agents) ────────────────────────────────────────

async def set_agent_ability(
    agent_id: str,
    ability: str,
    enabled: bool = True,
    user_id: str = "",
) -> str:
    """Turn an ability on or off for one of the user's own agents.

    ``ability`` is a connection_type from the ability catalogue (e.g.
    'codebase_admin', 'web_access', 'diagnostics', 'agent_orchestration',
    'browser_control', 'create_tools', 'image_generation', 'visualizer',
    'agent_management').

    Enabling an ability the app admin hasn't configured is refused (mirrors the
    REST connection endpoint). Disabling is always allowed.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not agent_id or not ability:
            return _err("agent_id and ability are required.")
        if not await _owns_agent(db, user_id, agent_id):
            return _err("You can only change abilities on agents you own.")

        if enabled:
            from app.admin.integrations import get_admin_configured_providers
            if ability not in await get_admin_configured_providers(user_id):
                return _err(
                    f"Ability '{ability}' is not configured in App Config → Agent Abilities. "
                    f"An app admin must enable it before it can be turned on per-agent."
                )
        await db.upsert_agent_connection(
            agent_id=agent_id,
            connection_type=ability,
            section="ability",
            enabled=bool(enabled),
            config={},
        )
        return _ok(agent_id=agent_id, ability=ability, enabled=bool(enabled))
    except Exception as e:
        logger.error("set_agent_ability failed: %s", e)
        return _err(str(e))

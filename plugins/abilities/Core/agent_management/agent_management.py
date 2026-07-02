"""Agent Management ability — fully self-contained drop-in.

See plugins/abilities/_TEMPLATE.py for the contract. This file carries the
ENTIRE agent-management capability: the in-process tool handlers (agent/template/
prompt/ability/tool/skill CRUD, all user-scoped and ownership-enforced), their
JSON schemas, the danger labels, the factory that closes the handlers over the
calling ``user_id``, AND the per-agent limit enforcement layered on top.

These tools replace the old "manage agents via the REST API over http_request"
approach, which forced the agent to make HTTP calls against the very server it
runs inside (localhost:8080) — a self-referential loop that broke whenever the
server was busy or restarting.

Every tool here:
  - runs in-process (direct DB calls, no HTTP, no self-calling),
  - is scoped to the calling user via the harness-injected ``user_id`` — the
    model never passes an identity and cannot act on another user's agents,
  - enforces ownership in code (not by prompt instruction): writes go through
    DB methods that filter on the agent's ``admin_users`` membership.

Runtime enforcement of per-agent management limits (max agents, per-session
edit/update counters, custom-only restriction) is layered on top of the
handlers via wrapping in ``build_tools``. The config is read from the
agent_management connection row's ``config.ability_settings`` JSON, persisted by
the UI through the standard connections endpoint.

Heavy imports (the DB, tool_modes, loader) stay LAZY — inside ``build_tools`` /
handler closures — so importing this module is cheap and side-effect-free; the
loader reads TOOL_SCHEMAS / DESTRUCTIVE only AFTER calling ``build_tools``.

Data domains:
  | Domain                  | Access      | Tool(s)                                 |
  |-------------------------|-------------|-----------------------------------------|
  | agent_templates         | read        | list_agent_templates                    |
  | agent_prompt_templates  | read        | list_agent_templates(template_id=...)   |
  | agents (own only)       | read/write  | list_my_agents, get_agent, create_agent, update_agent |
  | agent_prompts (own)     | read/write  | edit_agent_prompt                       |
  | agent abilities (own)   | read/write  | set_agent_ability                       |

Gated by the ``agent_management`` ability (a pure behavioural toggle — no
platform secret). Deliberately carries NO conversation-history (interactions) or
filesystem access; those stay under the heavier ``codebase_admin`` ability.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Populated inside build_tools() from the constants below so they can never
# drift; the loader reads them AFTER calling build_tools().
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()


# ── JSON helpers ──────────────────────────────────────────────────────────────

def _ok(**kw) -> str:
    return json.dumps({"status": "ok", **kw}, default=str)


def _err(message: str, **kw) -> str:
    return json.dumps({"status": "error", "message": message, **kw}, default=str)


async def _emit_agent_created(user_id: str, agent: dict) -> None:
    """Live-sync the owner's open Agents page after an agent is created in-process.

    The UI "New Agent" button creates via the REST endpoint (app/api/agents.py),
    which broadcasts an ``agent_created`` WebSocket event so every open Agents
    grid refreshes itself without a manual reload. This in-process tool path
    creates the agent directly in the DB and bypasses that endpoint, so without
    this emit the grid stayed stale until the user refreshed. Mirrors the REST
    event shape exactly so the existing frontend handler (``onAgentLifecycleEvent``)
    picks it up unchanged. Fire-and-forget: a delivery failure must never fail
    the creation."""
    if not user_id or not agent:
        return
    try:
        from app.api.chat import notify_user
        await notify_user(user_id, {
            "type": "agent_created", "user_id": user_id, "agent": agent,
        })
    except Exception as e:
        logger.debug("agent_created notify failed: %s", e)


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


# ── Tool catalogue: availability + permission + danger ────────────────────────
# The two per-tool knobs from the Tools panel, in readable form:
#   availability — "sent" (full schema every turn) | "discover" (load on demand)
#   permission   — "auto" (runs unattended) | "ask" (confirm first) | "deny" (blocked)
_AVAIL_TO_MODE = {"sent": "always", "discover": "discoverable"}
_MODE_TO_AVAIL = {"always": "sent", "discoverable": "discover", "core": "core"}
_VALID_PERMISSIONS = ("auto", "ask", "deny")
_VALID_TRIGGERS = (
    "user_input", "slash_command", "tool_call", "schedule", "webhook", "background",
)


def _as_json_list(v) -> list:
    if isinstance(v, str):
        try:
            return json.loads(v) or []
        except Exception:
            return []
    return v or []


def _as_json_obj(v) -> dict:
    if isinstance(v, str):
        try:
            return json.loads(v) or {}
        except Exception:
            return {}
    return v or {}


async def _agent_tool_catalog(db, agent_id: str, user_id: str) -> Optional[List[dict]]:
    """Build the agent's full tool catalogue — one row per tool its enabled
    abilities provide, each carrying:
      - availability : "sent" | "discover" | "core"
      - permission   : "auto" | "ask" | "deny"
      - locked       : True for core meta-tools (cannot be changed)
      - destructive  : the tool's BUILT-IN danger label (read-only, code-defined)

    Returns None if the agent doesn't exist. The danger label comes from the
    tool's own definition — policy can require confirmation or deny a tool, but
    nothing here relabels an inherently dangerous tool as safe.
    """
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        return None
    from app.tools.loader import load_tools
    from app.tools.tool_modes import resolve_mode, is_locked, ability_for_tool

    deny = set(_as_json_list(agent.get("allowed_tools")))
    ask = set(_as_json_obj(agent.get("safety_policy")).get("destructive_tools") or [])
    modes = await db.get_agent_tool_modes(agent_id)

    # Load the full ability-gated set with an EMPTY deny list so denied tools
    # still appear — otherwise the manager couldn't see or re-enable them.
    try:
        tools = await load_tools(
            user_id, agent_id=agent_id,
            agent_template_id=agent.get("template_id"),
            allowed_tools=[], session_id="",
        )
    except Exception as e:
        logger.warning("_agent_tool_catalog: load_tools failed for %s: %s", agent_id, e)
        tools = {}

    out: List[dict] = []
    for name, info in sorted(tools.items()):
        try:
            desc = (info.handler.__doc__ or "").strip().split("\n")[0]
        except Exception:
            desc = ""
        permission = "deny" if name in deny else ("ask" if name in ask else "auto")
        out.append({
            "name": name,
            "description": desc,
            "ability": ability_for_tool(name),
            "availability": _MODE_TO_AVAIL.get(resolve_mode(name, modes), "sent"),
            "permission": permission,
            "locked": is_locked(name),
            "destructive": bool(getattr(info, "destructive", False)
                                or getattr(info, "requires_confirmation", False)),
        })
    return out


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

    A template is only a starting point — create_agent can also build a
    from-scratch agent with NO template (pass template_id="none"), so if nothing
    here fits, start blank and write the persona yourself.
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
            "max_wall_seconds": full.get("max_wall_seconds"),
            "max_identical_tool_calls": full.get("max_identical_tool_calls"),
            "max_stall_strikes": full.get("max_stall_strikes"),
            "trigger_type": full.get("trigger_type"),
            "trigger_key": full.get("trigger_key"),
            "loop_logic": _as_json_list(full.get("loop_logic")),
            "user_mode": full.get("user_mode"),
        }
        # Tool overview: counts + only the tools whose settings differ from the
        # default (sent + auto). Use list_agent_tools for the full per-tool list.
        catalog = await _agent_tool_catalog(db, agent_id, user_id) or []
        customized = [t for t in catalog
                      if t["availability"] != "sent" or t["permission"] != "auto"]
        tools_overview = {
            "total": len(catalog),
            "discover": sum(1 for t in catalog if t["availability"] == "discover"),
            "ask": sum(1 for t in catalog if t["permission"] == "ask"),
            "deny": sum(1 for t in catalog if t["permission"] == "deny"),
            "customized": customized,
        }
        return _ok(agent=agent, abilities=abilities, tools=tools_overview,
                   slots=[{"slot_name": s["slot_name"], "order_index": s["order_index"],
                           "lock": s["lock"], "content": s["content"]} for s in slots])
    except Exception as e:
        logger.error("get_agent failed: %s", e)
        return _err(str(e))


# ── Agents (write) ────────────────────────────────────────────────────────────

async def create_agent(
    name: str,
    template_id: str = "",
    description: str = "",
    user_id: str = "",
) -> str:
    """Create a new agent owned by this user. YOU choose how it starts.

    First call list_agent_templates to see the available starting points, then
    decide between two paths:
      - **From a template** — pass that template's ``template_id``. The new
        agent's config and prompt slots are copied from it.
      - **From scratch (no template)** — pass ``template_id="none"`` (or leave it
        blank). The agent is a true blank slate: no config or prompts inherited,
        just the app-global baseline identity. You then write its persona with
        edit_agent_prompt. Use this when no template fits.

    EITHER way the agent is created as a BARE genui — **no abilities are
    enabled**. This is deliberate: you add only the abilities you justified in
    the plan, one at a time, with `set_agent_ability`, so every capability is
    intentional (never inherited-then-pruned). Refine prompts/limits afterwards
    with update_agent / edit_agent_prompt.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not name or not name.strip():
            return _err("Agent name is required.")
        agent = await db.create_custom_agent(
            user_id=user_id,
            name=name.strip(),
            # Pass through verbatim: a real id clones that template; a blank /
            # "none" value creates a from-scratch agent (the DB layer normalises
            # the no-template sentinels). No silent fallback to "default".
            template_id=template_id or "",
            seed_abilities=False,  # bare genui — abilities added deliberately
        )
        slim = {
            "id": agent.get("id"),
            "name": agent.get("name"),
            "description": agent.get("description"),
            "template_id": agent.get("template_id"),
        }
        # Live-sync the owner's open Agents page (see _emit_agent_created).
        await _emit_agent_created(user_id, slim)
        return _ok(agent=slim)
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
    max_wall_seconds: Optional[int] = None,
    max_identical_tool_calls: Optional[int] = None,
    max_stall_strikes: Optional[int] = None,
    trigger_type: Optional[str] = None,
    trigger_key: Optional[str] = None,
    user_id: str = "",
) -> str:
    """Update editable config fields on one of the user's own agents.

    Covers identity (name, description), the model settings (model, temperature,
    max_tokens), the guardrail limits (max_turn_count, max_wall_seconds,
    max_identical_tool_calls, max_stall_strikes), and the trigger (trigger_type,
    trigger_key). Only the supplied fields change. Use set_agent_tool for per-tool
    availability/permission, and set_agent_ability for abilities.

    Ownership is enforced: the update affects nothing (and returns an error) if
    the agent isn't yours.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not agent_id:
            return _err("agent_id is required.")
        if not await _owns_agent(db, user_id, agent_id):
            return _err("You can only edit agents you own.")
        if trigger_type is not None and trigger_type not in _VALID_TRIGGERS:
            return _err(f"trigger_type must be one of: {', '.join(_VALID_TRIGGERS)}.")
        updates: Dict[str, Any] = {}
        for k, v in (
            ("name", name), ("description", description), ("model", model),
            ("temperature", temperature), ("max_tokens", max_tokens),
            ("max_turn_count", max_turn_count), ("max_wall_seconds", max_wall_seconds),
            ("max_identical_tool_calls", max_identical_tool_calls),
            ("max_stall_strikes", max_stall_strikes),
            ("trigger_type", trigger_type), ("trigger_key", trigger_key),
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

# Known write/mutating tools per ability — a safety net so read_only reliably
# denies them even if a tool's built-in `destructive` flag isn't set. The
# destructive-flag scan below is the primary mechanism; this just guarantees the
# common cases (notably UI Admin "read-only", used to let an agent SEE the app's
# UI code without being able to change it).
_READ_ONLY_DENY_FALLBACK = {
    "ui_admin": {"write_source", "edit_source", "patch_source", "delete_source"},
    "codebase_admin": {"write_source", "edit_source", "patch_source", "delete_source",
                       "run_command", "restart_server", "commit_and_push", "db_query"},
}


async def set_agent_ability(
    agent_id: str,
    ability: str,
    enabled: bool = True,
    read_only: bool = False,
    deny_tools: Optional[List[str]] = None,
    ask_tools: Optional[List[str]] = None,
    user_id: str = "",
) -> str:
    """Turn an ability on or off for one of the user's own agents.

    ``ability`` is a connection_type from the ability catalogue (e.g.
    'codebase_admin', 'web_access', 'diagnostics', 'agent_orchestration',
    'browser_control', 'create_tools', 'image_generation', 'visualizer',
    'ui_admin', 'agent_management').

    Optional one-call tool tightening (applied only when enabling):
      - read_only=True : deny every WRITE/mutating tool the ability provides, so
                         the agent gets the ability's read tools only. Use this to
                         give an agent UI Admin (or Codebase Admin) so it can READ
                         the app's UI/source without being able to change it.
      - deny_tools=[…] : also deny these specific tools (must belong to the ability).
      - ask_tools=[…]  : require user confirmation for these specific tools.
    These compose with set_agent_tool, which can still tune individual tools later.

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
        # Newly-enabled ability → default its tools to "discoverable" (hybrid
        # rule); only fills tools with no explicit mode yet. No-op otherwise.
        applied: Dict[str, Any] = {}
        if enabled:
            try:
                from app.tools.tool_modes import tools_for_ability
                seed = tools_for_ability(ability)
                if seed:
                    await db.seed_agent_tool_modes(agent_id, seed, "discoverable")
            except Exception as e:
                logger.debug("tool-mode seed for ability %s failed: %s", ability, e)

            # ── Optional one-call read-only / deny / ask tightening ──
            if read_only or deny_tools or ask_tools:
                # Names this ability actually provides (from the live catalog so
                # destructive flags are accurate); reject names that don't belong.
                catalog = await _agent_tool_catalog(db, agent_id, user_id) or []
                ability_tools = {t["name"]: t for t in catalog if t.get("ability") == ability}
                deny: set = set()
                ask: set = set()
                if read_only:
                    deny |= {n for n, t in ability_tools.items() if t.get("destructive")}
                    deny |= (_READ_ONLY_DENY_FALLBACK.get(ability, set()) & set(ability_tools))
                for n in (deny_tools or []):
                    if n in ability_tools:
                        deny.add(n)
                for n in (ask_tools or []):
                    if n in ability_tools and n not in deny:
                        ask.add(n)
                if deny or ask:
                    await _apply_tool_permissions(db, agent_id, user_id, deny, ask)
                    applied = {"denied": sorted(deny), "ask": sorted(ask)}

        return _ok(agent_id=agent_id, ability=ability, enabled=bool(enabled), **({"tools": applied} if applied else {}))
    except Exception as e:
        logger.error("set_agent_ability failed: %s", e)
        return _err(str(e))


async def _apply_tool_permissions(db, agent_id: str, user_id: str, deny: set, ask: set) -> None:
    """Merge a batch of deny/ask tool permissions into the agent in one write.

    deny wins over ask for any tool named in both. Existing permissions for other
    tools are preserved (this is additive — it never clears another tool's gate).
    """
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        return
    deny_set = set(_as_json_list(agent.get("allowed_tools")))
    safety = _as_json_obj(agent.get("safety_policy"))
    ask_set = set(safety.get("destructive_tools") or [])
    for n in deny:
        ask_set.discard(n)
        deny_set.add(n)
    for n in ask:
        if n not in deny_set:
            ask_set.add(n)
    safety["destructive_tools"] = sorted(ask_set)
    await db.update_agent_fields(
        agent_id=agent_id, user_id=user_id,
        updates={"allowed_tools": sorted(deny_set), "safety_policy": safety},
    )


# ── Tools (read/write, own agents) ────────────────────────────────────────────

async def list_agent_tools(
    agent_id: str,
    ability: Optional[str] = None,
    query: Optional[str] = None,
    user_id: str = "",
) -> str:
    """List one of your visible agents' tools, each with its current settings.

    Every row carries:
      - availability : 'sent' (full schema every turn) | 'discover' (loaded on
                       demand) | 'core' (always sent, locked)
      - permission   : 'auto' (runs unattended) | 'ask' (confirm first) |
                       'deny' (the agent can't use it)
      - locked       : true for core tools (cannot be changed)
      - destructive  : the tool's built-in danger label (read-only)
      - ability      : the ability that provides the tool, if any

    Optional filters: ``ability`` (exact match) and ``query`` (substring on the
    tool name or description). Read-only — use set_agent_tool to change settings.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not agent_id:
            return _err("agent_id is required.")
        is_admin = await db.is_user_admin(user_id)
        visible = await db.list_agents_for_user(user_id, include_admin=is_admin)
        if not any(a.get("id") == agent_id for a in visible):
            return _err("Agent not found or not visible to you.")
        catalog = await _agent_tool_catalog(db, agent_id, user_id)
        if catalog is None:
            return _err("Agent not found.")
        if ability:
            catalog = [t for t in catalog if t["ability"] == ability]
        if query:
            q = query.lower()
            catalog = [t for t in catalog
                       if q in t["name"].lower() or q in (t["description"] or "").lower()]
        return _ok(count=len(catalog), tools=catalog)
    except Exception as e:
        logger.error("list_agent_tools failed: %s", e)
        return _err(str(e))


async def set_agent_tool(
    agent_id: str,
    tool: str,
    availability: Optional[str] = None,
    permission: Optional[str] = None,
    user_id: str = "",
) -> str:
    """Set ONE tool's availability and/or permission on one of your own agents.

      availability : 'sent' = full schema sent every turn; 'discover' = name
                     only until the agent loads it on demand (saves context).
      permission   : 'auto' = runs unattended; 'ask' = requires confirmation
                     first; 'deny' = the agent cannot use the tool at all.

    Provide at least one of the two. Core tools are locked and cannot be changed.
    This sets POLICY only — a tool's built-in danger label is fixed in code and
    cannot be changed here (you can require confirmation or deny a tool, but you
    cannot mark a dangerous tool as safe). Check tool names with list_agent_tools.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not agent_id or not tool:
            return _err("agent_id and tool are required.")
        if availability is None and permission is None:
            return _err("Provide availability and/or permission.")
        if availability is not None and availability not in _AVAIL_TO_MODE:
            return _err("availability must be 'sent' or 'discover'.")
        if permission is not None and permission not in _VALID_PERMISSIONS:
            return _err("permission must be 'auto', 'ask', or 'deny'.")
        if not await _owns_agent(db, user_id, agent_id):
            return _err("You can only change tools on agents you own.")

        from app.tools.tool_modes import is_locked
        if is_locked(tool):
            return _err(f"'{tool}' is a core tool — it is always available and cannot be changed.")

        catalog = await _agent_tool_catalog(db, agent_id, user_id)
        if catalog is None:
            return _err("Agent not found.")
        if not any(t["name"] == tool for t in catalog):
            return _err(
                f"'{tool}' is not one of this agent's tools. Enable the ability that "
                f"provides it first, or check the name with list_agent_tools."
            )

        changed: Dict[str, Any] = {}
        if availability is not None:
            await db.set_agent_tool_mode(agent_id, tool, _AVAIL_TO_MODE[availability])
            changed["availability"] = availability

        if permission is not None:
            agent = await db.get_agent_by_id(agent_id)
            deny_set = set(_as_json_list(agent.get("allowed_tools")))
            safety = _as_json_obj(agent.get("safety_policy"))
            ask_set = set(safety.get("destructive_tools") or [])
            # Clear then re-apply, so the three states are mutually exclusive.
            deny_set.discard(tool)
            ask_set.discard(tool)
            if permission == "deny":
                deny_set.add(tool)
            elif permission == "ask":
                ask_set.add(tool)
            safety["destructive_tools"] = sorted(ask_set)
            updated = await db.update_agent_fields(
                agent_id=agent_id, user_id=user_id,
                updates={"allowed_tools": sorted(deny_set), "safety_policy": safety},
            )
            if updated is None:
                return _err("Agent not found or not owned by you.")
            changed["permission"] = permission

        return _ok(agent_id=agent_id, tool=tool, **changed)
    except Exception as e:
        logger.error("set_agent_tool failed: %s", e)
        return _err(str(e))


# ── Skills (read/write, own agents) ───────────────────────────────────────────

_VALID_SKILL_MODES = ("always_on", "selectable")


async def manage_agent_skills(
    action: str,
    agent_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    body: Optional[str] = None,
    mode: Optional[str] = None,
    enabled: Optional[bool] = None,
    user_id: str = "",
) -> str:
    """Read or edit the on-demand SKILLS of one of the user's own agents.

    A skill is a named knowledge pack stored on the agent. Each has:
      - name        : short identifier the agent uses with load_skill
      - description : ALWAYS shown to the agent — say WHEN to use this skill
      - body        : the full step-by-step instructions
      - mode        : 'always_on'  → body is always in the agent's context, or
                      'selectable' → body is hidden; the agent only sees the
                       name + description and pulls the body in with load_skill
                       when a task matches (use this for specialized playbooks)
      - enabled     : whether the skill is active at all

    Actions:
      list   → list the agent's skills (name, description, mode, enabled)
      set    → add or update a skill by name. NEW skill: name required, provide
               description + body; mode defaults to 'selectable', enabled true.
               EXISTING skill: omitted fields keep their current value.
      remove → delete a skill by name

    Reads require the agent be visible to you; writes require you to own it.
    Prefer 'selectable' for niche/specialized skills and 'always_on' only for
    guidance the agent should follow on every turn.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not agent_id:
            return _err("agent_id is required.")

        if action == "list":
            is_admin = await db.is_user_admin(user_id)
            visible = await db.list_agents_for_user(user_id, include_admin=is_admin)
            if not any(a.get("id") == agent_id for a in visible):
                return _err("Agent not found or not visible to you.")
            skills = await db.get_agent_skills(agent_id)
            return _ok(count=len(skills), skills=skills)

        if not await _owns_agent(db, user_id, agent_id):
            return _err("You can only edit skills on agents you own.")
        skills = await db.get_agent_skills(agent_id)

        if action == "set":
            if not name or not name.strip():
                return _err("name is required for set.")
            nm = name.strip()
            if mode is not None and mode not in _VALID_SKILL_MODES:
                return _err("mode must be 'always_on' or 'selectable'.")
            existing = next((s for s in skills if s["name"].lower() == nm.lower()), None)
            if existing:
                if description is not None:
                    existing["description"] = description
                if body is not None:
                    existing["body"] = body
                if mode is not None:
                    existing["mode"] = mode
                if enabled is not None:
                    existing["enabled"] = bool(enabled)
            else:
                skills.append({
                    "name": nm,
                    "description": description or "",
                    "body": body or "",
                    "mode": mode or "selectable",
                    "enabled": True if enabled is None else bool(enabled),
                })
            saved = await db.set_agent_skills(agent_id, skills, updated_by=f"agent_mgmt:{user_id}")
            return _ok(
                skill=nm, count=len(saved),
                skills=[{"name": s["name"], "mode": s["mode"], "enabled": s["enabled"]} for s in saved],
            )

        if action == "remove":
            if not name or not name.strip():
                return _err("name is required for remove.")
            target = name.strip().lower()
            kept = [s for s in skills if s["name"].lower() != target]
            if len(kept) == len(skills):
                return _err(f"No skill named '{name}'.")
            saved = await db.set_agent_skills(agent_id, kept, updated_by=f"agent_mgmt:{user_id}")
            return _ok(removed=name, count=len(saved))

        return _err(f"Unknown action '{action}'. Use: list, set, remove.")
    except Exception as e:
        logger.error("manage_agent_skills failed: %s", e)
        return _err(str(e))


# ── Tool schemas + danger labels ──────────────────────────────────────────────
# The 10 agent-management tool schemas. The four read tools run free; the six
# write tools confirm-gate (see _DESTRUCTIVE below). build_tools() copies these
# into the module-level TOOL_SCHEMAS / DESTRUCTIVE that the loader reads after
# the call.
_TOOL_SCHEMAS: Dict[str, dict] = {
    "list_agent_templates": {
        "type": "object",
        "properties": {
            "template_id": {"type": "string", "description": "Optional: a template id to also return its canonical prompt slots."},
            "include_admin": {"type": "boolean", "description": "Include admin-only templates (platform admins only).", "default": False},
        },
        "required": [],
    },
    "list_my_agents": {"type": "object", "properties": {}, "required": []},
    "get_agent": {
        "type": "object",
        "properties": {"agent_id": {"type": "string", "description": "The agent's id."}},
        "required": ["agent_id"],
    },
    "list_agent_tools": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "The agent whose tools to list (must be visible to you)."},
            "ability": {"type": "string", "description": "Optional: only tools provided by this ability."},
            "query": {"type": "string", "description": "Optional: substring filter on tool name or description."},
        },
        "required": ["agent_id"],
    },
    "create_agent": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Display name for the new agent."},
            "template_id": {"type": "string", "description": "How the agent starts: a template id from list_agent_templates to clone that template's config + prompts, OR 'none' (or blank) for a from-scratch blank-slate agent with no template. There is no silent default — choose one."},
            "description": {"type": "string", "description": "Short description of the agent."},
        },
        "required": ["name"],
    },
    "update_agent": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "The agent to update (must be yours)."},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "model": {"type": "string", "description": "Model id (e.g. an OpenRouter model slug)."},
            "temperature": {"type": "number"},
            "max_tokens": {"type": "integer"},
            "max_turn_count": {"type": "integer", "description": "Max turns per run; 0 = unlimited."},
            "max_wall_seconds": {"type": "integer", "description": "Wall-clock cap per run in seconds."},
            "max_identical_tool_calls": {"type": "integer", "description": "Loop-breaker for repeated identical calls; 0 = off."},
            "max_stall_strikes": {"type": "integer", "description": "Stall-guard strikes before stopping; 0 = off."},
            "trigger_type": {"type": "string", "enum": ["user_input", "slash_command", "tool_call", "schedule", "webhook", "background"], "description": "What starts the agent."},
            "trigger_key": {"type": "string", "description": "Command/event key for the trigger type."},
        },
        "required": ["agent_id"],
    },
    "set_agent_tool": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "The agent to change (must be yours)."},
            "tool": {"type": "string", "description": "The tool name (see list_agent_tools)."},
            "availability": {"type": "string", "enum": ["sent", "discover"], "description": "'sent' = full schema every turn; 'discover' = loaded on demand."},
            "permission": {"type": "string", "enum": ["auto", "ask", "deny"], "description": "'auto' = runs unattended; 'ask' = confirm first; 'deny' = blocked. Sets policy only — cannot relabel a tool's built-in danger."},
        },
        "required": ["agent_id", "tool"],
    },
    "edit_agent_prompt": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "get", "insert", "update", "delete"], "description": "What to do with the prompt slots."},
            "agent_id": {"type": "string", "description": "The agent whose prompts to read/edit."},
            "slot_name": {"type": "string", "description": "Slot name (e.g. system, agent, user, skills, tasks, misc)."},
            "content": {"type": "string", "description": "New slot content (for insert/update)."},
            "lock": {"type": "boolean", "description": "Whether per-user overrides are blocked for this slot."},
            "merge_mode": {"type": "string", "description": "'replace' or 'append'."},
            "order_index": {"type": "integer", "description": "Sort order of the slot."},
        },
        "required": ["action", "agent_id"],
    },
    "set_agent_ability": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "The agent to change (must be yours)."},
            "ability": {"type": "string", "description": "Ability connection_type, e.g. codebase_admin, web_access, diagnostics, agent_orchestration, browser_control, create_tools, image_generation, visualizer, ui_admin, agent_management."},
            "enabled": {"type": "boolean", "description": "Turn the ability on (true) or off (false).", "default": True},
            "read_only": {"type": "boolean", "description": "When enabling: deny every write/mutating tool the ability provides, leaving only its read tools. Use to give an agent UI Admin or Codebase Admin so it can READ code without changing it.", "default": False},
            "deny_tools": {"type": "array", "items": {"type": "string"}, "description": "When enabling: also deny these specific tools of the ability (the agent can't use them)."},
            "ask_tools": {"type": "array", "items": {"type": "string"}, "description": "When enabling: require user confirmation for these specific tools of the ability."},
        },
        "required": ["agent_id", "ability"],
    },
    "manage_agent_skills": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "set", "remove"], "description": "list the agent's skills, set (add/update) one, or remove one."},
            "agent_id": {"type": "string", "description": "The agent whose skills to read/edit (must be yours to write)."},
            "name": {"type": "string", "description": "Skill name (required for set/remove)."},
            "description": {"type": "string", "description": "When to use the skill — ALWAYS shown to the agent."},
            "body": {"type": "string", "description": "The full step-by-step instructions for the skill."},
            "mode": {"type": "string", "enum": ["always_on", "selectable"], "description": "'always_on' = body always in context; 'selectable' = body hidden until the agent calls load_skill. Defaults to 'selectable'."},
            "enabled": {"type": "boolean", "description": "Whether the skill is active."},
        },
        "required": ["action", "agent_id"],
    },
}

# The READ tools (list_agent_templates, list_my_agents, get_agent,
# list_agent_tools) run free. The WRITE tools reconfigure the user's agents
# (create/update/retool/re-prompt/re-ability/re-skill) so they confirm-gate in
# Ask/Plan mode. (Agent *deletion* is gated separately behind the
# allow_agent_deletion config flag, not exposed as its own tool here.)
_DESTRUCTIVE: set = {
    "create_agent",
    "update_agent",
    "set_agent_tool",
    "edit_agent_prompt",
    "set_agent_ability",
    "manage_agent_skills",
}


# ── Factory: build the 10 handlers, each closing over user_id ─────────────────

def _build_agent_mgmt_tools(user_id: str) -> Dict[str, Any]:
    """Build the 10 agent-management tool handlers, each closing over ``user_id``.

    The nested wrappers carry no docstring of their own, so each tool's real
    usage doc is copied from the underlying in-process function — this feeds both
    the tool-call description and the # [TOOLS] index.
    """

    async def _amt_list_templates_wrapper(template_id: Optional[str] = None,
                                          include_admin: bool = False):
        return await list_agent_templates(template_id=template_id,
                                          include_admin=include_admin, user_id=user_id)

    async def _amt_list_agents_wrapper():
        return await list_my_agents(user_id=user_id)

    async def _amt_get_agent_wrapper(agent_id: str):
        return await get_agent(agent_id=agent_id, user_id=user_id)

    async def _amt_list_tools_wrapper(agent_id: str, ability: Optional[str] = None,
                                      query: Optional[str] = None):
        return await list_agent_tools(agent_id=agent_id, ability=ability,
                                      query=query, user_id=user_id)

    async def _amt_create_agent_wrapper(name: str, template_id: str = "",
                                        description: str = ""):
        return await create_agent(name=name, template_id=template_id,
                                  description=description, user_id=user_id)

    async def _amt_update_agent_wrapper(agent_id: str, name: Optional[str] = None,
                                        description: Optional[str] = None,
                                        model: Optional[str] = None,
                                        temperature: Optional[float] = None,
                                        max_tokens: Optional[int] = None,
                                        max_turn_count: Optional[int] = None,
                                        max_wall_seconds: Optional[int] = None,
                                        max_identical_tool_calls: Optional[int] = None,
                                        max_stall_strikes: Optional[int] = None,
                                        trigger_type: Optional[str] = None,
                                        trigger_key: Optional[str] = None):
        return await update_agent(agent_id=agent_id, name=name,
                                  description=description, model=model,
                                  temperature=temperature, max_tokens=max_tokens,
                                  max_turn_count=max_turn_count,
                                  max_wall_seconds=max_wall_seconds,
                                  max_identical_tool_calls=max_identical_tool_calls,
                                  max_stall_strikes=max_stall_strikes,
                                  trigger_type=trigger_type, trigger_key=trigger_key,
                                  user_id=user_id)

    async def _amt_set_tool_wrapper(agent_id: str, tool: str,
                                    availability: Optional[str] = None,
                                    permission: Optional[str] = None):
        return await set_agent_tool(agent_id=agent_id, tool=tool,
                                    availability=availability, permission=permission,
                                    user_id=user_id)

    async def _amt_edit_prompt_wrapper(action: str, agent_id: str,
                                       slot_name: Optional[str] = None,
                                       content: Optional[str] = None,
                                       lock: Optional[bool] = None,
                                       merge_mode: Optional[str] = None,
                                       order_index: Optional[int] = None):
        return await edit_agent_prompt(action=action, agent_id=agent_id,
                                       slot_name=slot_name, content=content, lock=lock,
                                       merge_mode=merge_mode, order_index=order_index,
                                       user_id=user_id)

    async def _amt_set_ability_wrapper(agent_id: str, ability: str, enabled: bool = True,
                                       read_only: bool = False,
                                       deny_tools: Optional[List[str]] = None,
                                       ask_tools: Optional[List[str]] = None):
        return await set_agent_ability(agent_id=agent_id, ability=ability,
                                       enabled=enabled, read_only=read_only,
                                       deny_tools=deny_tools, ask_tools=ask_tools,
                                       user_id=user_id)

    async def _amt_manage_skills_wrapper(action: str, agent_id: str,
                                         name: Optional[str] = None,
                                         description: Optional[str] = None,
                                         body: Optional[str] = None,
                                         mode: Optional[str] = None,
                                         enabled: Optional[bool] = None):
        return await manage_agent_skills(action=action, agent_id=agent_id, name=name,
                                         description=description, body=body, mode=mode,
                                         enabled=enabled, user_id=user_id)

    # The nested wrappers carry no docstring of their own, so the model would see
    # an empty/generic description. Copy each tool's real usage doc from the
    # underlying in-process function — this feeds both the tool-call description
    # and the # [TOOLS] index.
    _amt_list_templates_wrapper.__doc__ = list_agent_templates.__doc__
    _amt_list_agents_wrapper.__doc__    = list_my_agents.__doc__
    _amt_get_agent_wrapper.__doc__      = get_agent.__doc__
    _amt_list_tools_wrapper.__doc__     = list_agent_tools.__doc__
    _amt_create_agent_wrapper.__doc__   = create_agent.__doc__
    _amt_update_agent_wrapper.__doc__   = update_agent.__doc__
    _amt_set_tool_wrapper.__doc__       = set_agent_tool.__doc__
    _amt_edit_prompt_wrapper.__doc__    = edit_agent_prompt.__doc__
    _amt_set_ability_wrapper.__doc__    = set_agent_ability.__doc__
    _amt_manage_skills_wrapper.__doc__  = manage_agent_skills.__doc__

    return {
        "list_agent_templates": _amt_list_templates_wrapper,
        "list_my_agents": _amt_list_agents_wrapper,
        "get_agent": _amt_get_agent_wrapper,
        "list_agent_tools": _amt_list_tools_wrapper,
        "create_agent": _amt_create_agent_wrapper,
        "update_agent": _amt_update_agent_wrapper,
        "set_agent_tool": _amt_set_tool_wrapper,
        "edit_agent_prompt": _amt_edit_prompt_wrapper,
        "set_agent_ability": _amt_set_ability_wrapper,
        "manage_agent_skills": _amt_manage_skills_wrapper,
    }


# ── Per-agent config defaults ────────────────────────────────────────────────

_MGMT_CONFIG_DEFAULTS = {
    "max_agents_per_user": 10,
    "allow_agent_deletion": False,
    "allow_discoverable_agents": False,
    "max_prompt_edits_per_session": 10,
    "max_agent_updates_per_session": 20,
    "restrict_to_custom_only": False,
}

# Per-session counters — resets on server restart (fine for a safety guardrail).
_SESSION_COUNTERS: dict = {}  # {session_id: {"prompt_edits": N, "agent_updates": N}}
_COUNTER_LOCK = asyncio.Lock()

# Tool names that increment the agent_updates counter.
_UPDATE_TOOLS = {"update_agent", "set_agent_tool", "set_agent_ability", "manage_agent_skills"}

# Tool names that need restrict_to_custom_only enforcement (target_agent_id
# is the first positional arg after self for all of these).
_RESTRICT_TOOLS = {"update_agent", "set_agent_tool", "edit_agent_prompt",
                   "set_agent_ability", "manage_agent_skills"}

# Tool names an agent must NOT be allowed to point at ITSELF. An agent
# re-enabling its own abilities or widening its own tools would let it climb
# out of the box its admin put it in (observed: an agent flipped its own
# agent_orchestration ability back on). Self-targeting these is always refused;
# the agent can still manage OTHER agents it owns.
_SELF_PROTECT_TOOLS = {"update_agent", "set_agent_tool", "edit_agent_prompt",
                       "set_agent_ability", "manage_agent_skills"}


# ── Config loader ────────────────────────────────────────────────────────────

async def _load_mgmt_config(agent_id: str) -> dict:
    """Read per-agent management limits from the agent_management connection
    row's config JSON.  Returns defaults for any missing keys."""
    if not agent_id:
        return dict(_MGMT_CONFIG_DEFAULTS)
    limits = dict(_MGMT_CONFIG_DEFAULTS)
    try:
        from app.db import get_db
        db = get_db()
        conns = await db.get_agent_connections(agent_id)
        for c in conns:
            if c.get("connection_type") == "agent_management" and c.get("section") == "ability":
                raw = c.get("config")
                if isinstance(raw, str):
                    raw = json.loads(raw or "{}")
                if isinstance(raw, dict):
                    settings = raw.get("ability_settings") or {}
                    for k in _MGMT_CONFIG_DEFAULTS:
                        if k in settings:
                            v = settings[k]
                            if k in ("max_agents_per_user", "max_prompt_edits_per_session",
                                     "max_agent_updates_per_session"):
                                try:
                                    v = int(v)
                                except (TypeError, ValueError):
                                    continue
                            elif k in ("allow_agent_deletion", "allow_discoverable_agents",
                                      "restrict_to_custom_only"):
                                v = bool(v)
                            limits[k] = v
                break
    except Exception as e:
        logger.debug("Failed to load management config for agent %s: %s", agent_id, e)
    return limits


# ── Limit-check helpers ──────────────────────────────────────────────────────

def _limit_err(msg: str) -> str:
    return json.dumps({"status": "error", "error": msg})


async def _target_source(agent_id: str) -> str:
    """Best-effort lookup of an agent's source field. Returns '' on failure."""
    try:
        from app.db import get_db
        db = get_db()
        ag = await db.get_agent_by_id(agent_id)
        return (ag or {}).get("source") or ""
    except Exception:
        return ""


async def _user_agent_count(user_id: str) -> int:
    """Best-effort count of agents owned by a user. Returns 0 on failure."""
    try:
        from app.db import get_db
        db = get_db()
        agents = await db.list_user_agents(user_id)
        return len(agents) if agents else 0
    except Exception:
        return 0


async def _check_mgmt_limit(agent_id: str, tool_name: str, session_id: str,
                            user_id: str = "", target_agent_id: str = "",
                            discoverable: bool = False) -> str:
    """Check management limits before executing a tool.

    Returns an error JSON string on breach, or an empty string if OK.
    The caller should return the error string if non-empty.
    """
    cfg = await _load_mgmt_config(agent_id)

    # ── Restrict to custom only ──────────────────────────────────────────
    if cfg.get("restrict_to_custom_only") and tool_name in _RESTRICT_TOOLS and target_agent_id:
        src = await _target_source(target_agent_id)
        if src and src != "custom":
            return _limit_err(
                f"This agent is restricted to custom agents only. "
                f"Target agent '{target_agent_id}' has source '{src}'."
            )

    # ── Max agents per user (create only) ────────────────────────────────
    if tool_name == "create_agent" and user_id:
        count = await _user_agent_count(user_id)
        max_agents = cfg.get("max_agents_per_user", 10)
        if count >= max_agents:
            return _limit_err(
                f"Agent limit reached: user already has {count} agents "
                f"(max {max_agents}). Delete unused agents or ask the human."
            )

    # ── Discoverable check (create only) ────────────────────────────────
    if tool_name == "create_agent" and discoverable and not cfg.get("allow_discoverable_agents"):
        return _limit_err(
            "Discoverable agents are not allowed by this agent's configuration. "
            "Set discoverable=false."
        )

    # ── Per-session counters ────────────────────────────────────────────
    ctr = None
    async with _COUNTER_LOCK:
        if session_id not in _SESSION_COUNTERS:
            _SESSION_COUNTERS[session_id] = {"prompt_edits": 0, "agent_updates": 0}
        ctr = _SESSION_COUNTERS[session_id]

    if tool_name == "edit_agent_prompt":
        ctr["prompt_edits"] += 1
        max_edits = cfg.get("max_prompt_edits_per_session", 10)
        if ctr["prompt_edits"] > max_edits:
            return _limit_err(
                f"Prompt edit limit reached: {ctr['prompt_edits']} edits this "
                f"session (max {max_edits}). Start a new session to reset."
            )

    if tool_name in _UPDATE_TOOLS:
        ctr["agent_updates"] += 1
        max_updates = cfg.get("max_agent_updates_per_session", 20)
        if ctr["agent_updates"] > max_updates:
            return _limit_err(
                f"Agent update limit reached: {ctr['agent_updates']} updates "
                f"this session (max {max_updates}). Start a new session to reset."
            )

    return ""


# ── build_tools — wraps the factory handlers with limit enforcement ──────────

# We need the wrapper to be an async callable that passes through keyword args.
# Since the factory returns handlers with varying signatures, we wrap each via
# a closure that knows which args to extract for limit checks.

def _wrap_with_limits(handler, agent_id: str, session_id: str,
                      tool_name: str, user_id: str):
    """Return an async wrapper that checks limits before delegating to handler."""
    # Extract target_agent_id from the first positional arg for tools that have one.
    # For create_agent, extract discoverable from kwargs.
    extract_target = tool_name in _RESTRICT_TOOLS

    async def wrapped(*args, **kwargs):
        target_agent_id = ""
        if extract_target:
            # The LLM may pass the target positionally OR as agent_id=...; check both
            # so neither the self-protect block nor the custom-only restriction can
            # be sidestepped by argument style.
            if args and args[0]:
                target_agent_id = str(args[0])
            elif kwargs.get("agent_id"):
                target_agent_id = str(kwargs["agent_id"])

        # ── Self-modification block ──────────────────────────────────────
        # An agent may manage other agents it owns, but never re-arm itself.
        if (tool_name in _SELF_PROTECT_TOOLS and agent_id
                and target_agent_id == agent_id):
            return _limit_err(
                "An agent cannot modify its own abilities, tools, prompts, or "
                "config. Ask a human to change this agent's setup, or target a "
                "different agent you own."
            )

        discoverable = False
        if tool_name == "create_agent":
            discoverable = bool(kwargs.get("discoverable", False))

        error = await _check_mgmt_limit(
            agent_id=agent_id, tool_name=tool_name,
            session_id=session_id, user_id=user_id,
            target_agent_id=target_agent_id,
            discoverable=discoverable,
        )
        if error:
            return error
        return await handler(*args, **kwargs)

    # Copy docstring so the tool description stays intact.
    wrapped.__doc__ = handler.__doc__
    return wrapped


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the 10 agent-management tools, each
    wrapped with per-agent limit enforcement (max agents, session counters,
    custom-only restriction)."""
    handlers = _build_agent_mgmt_tools(user_id)

    # Wrap mutation tools with limit checks.  Read-only tools pass through.
    enforce = (
        "create_agent", "update_agent", "set_agent_tool",
        "edit_agent_prompt", "set_agent_ability", "manage_agent_skills",
    )
    for name in enforce:
        if name in handlers and agent_id:
            handlers[name] = _wrap_with_limits(
                handlers[name], agent_id=agent_id, session_id=session_id,
                tool_name=name, user_id=user_id,
            )

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update(_TOOL_SCHEMAS)
    DESTRUCTIVE.clear()
    DESTRUCTIVE.update(_DESTRUCTIVE)

    return handlers

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
        # Newly-enabled ability → default its tools to "discoverable" (hybrid
        # rule); only fills tools with no explicit mode yet. No-op otherwise.
        if enabled:
            try:
                from app.tools.tool_modes import tools_for_ability
                seed = tools_for_ability(ability)
                if seed:
                    await db.seed_agent_tool_modes(agent_id, seed, "discoverable")
            except Exception as e:
                logger.debug("tool-mode seed for ability %s failed: %s", ability, e)
        return _ok(agent_id=agent_id, ability=ability, enabled=bool(enabled))
    except Exception as e:
        logger.error("set_agent_ability failed: %s", e)
        return _err(str(e))


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

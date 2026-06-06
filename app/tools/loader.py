"""
Tool loader for dynamic tool loading from database.

================================================================================
 THIS IS CORE — do NOT add new integrations / providers / capabilities here.
================================================================================
New capabilities are DROP-IN FILES in a plugin folder, auto-discovered at
runtime — never wired into this loader or a central list. See CLAUDE.md
("Core vs. plugins") and docs/claude/production-editions.md.

  • A new OAuth/API integration → a new file in app/integrations/ exposing a
    TOOLS list (copy app/integrations/_TEMPLATE.py). It is gathered by
    inject_integration_tools() automatically.
  • A new event source / channel / connector / secrets vault / encryption
    method / payment processor / scheduler provider → its own file in the
    matching plugin folder with a FEATURE header.

Only edit this file to change CORE loop machinery (the irreducible built-in
tools, ability gating, tool-mode handling) — not to register a capability.
================================================================================
"""
import json
import logging
import os
from dataclasses import dataclass
from typing import Dict, Any, Callable, List, Optional
from app.db import get_db

logger = logging.getLogger(__name__)


# ── Built-in tool metadata ────────────────────────────────────────────────────
# Consumed by /admin/tools to provide a merged view of all tools (built-ins +
# user skills) with loop-stage annotations for the visualizer.
#
# stages      — which loop node(s) this tool can fire in
# destructive — writes, deletes, or has irreversible side-effects
# agent_types — which agent type names may use it; [] means all
BUILTIN_TOOL_METADATA: Dict[str, Dict[str, Any]] = {
    # ── Core discovery (always sent, locked) ──
    "load_tool":                     {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    # ── Web & browser ──
    "web_search":                    {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "browser_action":                {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "http_request":                  {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "maps_geocode":                  {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    # ── Image generation (gated by image_generation) ──
    "generate_image":                {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    # ── DB & context (gated by codebase_admin) ──
    "db_query":                      {"stages": ["execute_tools"],                                "destructive": True,  "agent_types": []},
    # ── Diagnostics (gated by the diagnostics ability) ──
    "read_diagnostics":              {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    # ── Agent management (gated by the agent_management ability) — in-process,
    #    user-scoped agent CRUD. Reads are non-destructive; writes mutate the
    #    user's own agents only (ownership enforced in the tool/DB layer). ──
    "list_agent_templates":          {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "list_my_agents":                {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "get_agent":                     {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "list_agent_tools":              {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "create_agent":                  {"stages": ["execute_tools"],                                "destructive": True,  "agent_types": []},
    "update_agent":                  {"stages": ["execute_tools"],                                "destructive": True,  "agent_types": []},
    "set_agent_tool":                {"stages": ["execute_tools"],                                "destructive": True,  "agent_types": []},
    "edit_agent_prompt":             {"stages": ["execute_tools"],                                "destructive": True,  "agent_types": []},
    "set_agent_ability":             {"stages": ["execute_tools"],                                "destructive": True,  "agent_types": []},
    "manage_agent_skills":           {"stages": ["execute_tools"],                                "destructive": True,  "agent_types": []},
    # ── Memory ──
    "memory":                        {"stages": ["memory_search", "memory_save", "execute_tools"], "destructive": False, "agent_types": []},
    "session_search":                {"stages": ["load_context", "execute_tools"],               "destructive": False, "agent_types": []},
    # ── Utilities ──
    "get_time":                      {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    "get_date":                      {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    "get_weather":                   {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    "calculate":                     {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    "read_attachment":               {"stages": ["load_context", "execute_tools"],               "destructive": False, "agent_types": []},
    # ── Tool creation ──
    "create_tool":                   {"stages": ["execute_tools"],                               "destructive": True,  "agent_types": []},
    "rate_skill":                    {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    # ── Webhooks ──
    "register_webhook":              {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    "list_webhooks":                 {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    "delete_webhook":                {"stages": ["execute_tools"],                               "destructive": True,  "agent_types": []},
    "get_webhook_log":               {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    # ── Optimizer ──
    "run_optimizer":                 {"stages": ["user_input", "execute_tools"],                 "destructive": False, "agent_types": ["default"]},
    "run_worker_trials":             {"stages": ["opt_validate"],                                "destructive": False, "agent_types": ["optimizer-planner"]},
    "handoff_to_closer":          {"stages": ["opt_propose"],                                 "destructive": False, "agent_types": ["optimizer-planner"]},
    "deploy_optimization":           {"stages": ["opt_apply"],                                   "destructive": True,  "agent_types": ["optimizer-closer"]},
    # ── Delegation ──
    "delegate_to_agent":             {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "list_delegatable_agents":       {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    # ── Terminal control (gated by terminal_control) — open/drive interactive
    #    terminal programs; open/send/close write, so they pass guardrails ──
    "terminal_open":                 {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": False, "agent_types": []},
    "terminal_read":                 {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "terminal_send":                 {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": False, "agent_types": []},
    "terminal_wait":                 {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "terminal_list":                 {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "terminal_close":                {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": False, "agent_types": []},
    # ── App control (gated by app_control) — rearrange the viewer's own screen,
    #    writes no data ──
    "set_app_view":                  {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    # ── Admin/source (privileged) — write/exec tools pass through guardrails ──
    "read_source":                   {"stages": ["execute_tools"],                               "destructive": False, "requires_confirmation": False, "agent_types": ["admin"]},
    "write_source":                  {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": False, "agent_types": ["admin"]},
    "edit_source":                   {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": False, "agent_types": ["admin"]},
    "delete_source":                 {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": False, "agent_types": ["admin"]},
    "resolve_conflict":              {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": False, "agent_types": ["admin"]},
    "commit_and_push":               {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": True,  "agent_types": ["admin"]},
    "run_command":                   {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": True,  "agent_types": ["admin"]},
    "restart_server":                {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": True,  "agent_types": ["admin"]},
    # ── Auth / comms ──
    "register_user":                 {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    # ── OAuth integration ──
    "check_oauth_connection":        {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
}


# ── Ability → built-in tools map ──────────────────────────────────────────────
# The authoritative reverse of the per-ability injection blocks in
# _inject_builtin_tools(). Drives (a) seeding "discoverable" tool modes the
# moment an ability is toggled on for an agent, and (b) labelling each tool with
# its ability in the agent Tools panel. Tools NOT listed here (always-on
# utilities, DB/custom tools, OAuth-integration tools) carry no ability and
# default to the "always" mode.
#
# DROP-IN — this map is now BUILT from the ability files in plugins/abilities/
# (each declares the tool names it gates). Do NOT add an ability here: drop a
# file in plugins/abilities/ instead. The literal below is only a fail-safe used
# if that scan is unavailable. See app/abilities/__init__.py and CLAUDE.md.
_FALLBACK_ABILITY_TOOLS: Dict[str, List[str]] = {
    "web_access":          ["web_search", "maps_geocode", "get_weather"],
    "diagnostics":         ["read_diagnostics"],
    "agent_management":    ["list_agent_templates", "list_my_agents", "get_agent",
                            "list_agent_tools", "create_agent", "update_agent",
                            "set_agent_tool", "edit_agent_prompt",
                            "set_agent_ability", "manage_agent_skills"],
    "browser_control":     ["browser_action", "http_request"],
    "image_generation":    ["generate_image"],
    "codebase_admin":      ["db_query", "read_source", "write_source", "edit_source",
                            "delete_source", "resolve_conflict", "commit_and_push",
                            "run_command", "restart_server"],
    "create_tools":        ["create_tool"],
    "agent_orchestration": ["run_optimizer", "delegate_to_agent",
                            "list_delegatable_agents"],
    "automation":          ["list_event_sources", "list_delivery_channels",
                            "event_subscribe"],
    "terminal_control":    ["terminal_open", "terminal_read", "terminal_send",
                            "terminal_wait", "terminal_list", "terminal_close"],
    "app_control":         ["set_app_view"],
    "wiki_control":        ["wiki_search", "wiki_list", "wiki_get",
                            "wiki_create", "wiki_update", "wiki_set_status",
                            "wiki_delete", "wiki_history", "wiki_get_revision",
                            "wiki_restore", "wiki_backlinks"],
}

# context_control gates loop behavior (context-fill signal + compaction), not a
# grantable, user-facing ability — so it stays a core residual rather than a
# drop-in file. It carries no statically-gated tool names.
_CORE_ABILITY_TOOLS: Dict[str, List[str]] = {
    "context_control": [],
}


def _build_ability_tools() -> Dict[str, List[str]]:
    """Build the ability→tools map from plugins/abilities/, with a fail-safe."""
    merged: Dict[str, List[str]] = {}
    try:
        from app.abilities import tools_map
        merged.update(tools_map())
    except Exception as e:
        logger.warning("Ability scan unavailable (%s); using fallback ability map", e)
        merged.update(_FALLBACK_ABILITY_TOOLS)
    if not merged:  # scan present but empty — don't strand every ability
        merged.update(_FALLBACK_ABILITY_TOOLS)
    merged.update(_CORE_ABILITY_TOOLS)
    return merged


ABILITY_TOOLS: Dict[str, List[str]] = _build_ability_tools()


def _merge_integration_metadata() -> None:
    """Pull tool metadata from `app/integrations/*` subpackages into BUILTIN_TOOL_METADATA.

    Runs at import time so /admin/tools and the loop diagram see Gmail / Calendar /
    Drive tools alongside the rest. Deleting `app/integrations/` makes this no-op.
    """
    try:
        from app.integrations import integration_tool_metadata
        BUILTIN_TOOL_METADATA.update(integration_tool_metadata())
    except Exception as e:
        logger.warning("could not merge integration tool metadata: %s", e)


_merge_integration_metadata()


def _get_webhook_base_url() -> str:
    """Get the configured public webhook base URL from the plugin registry."""
    try:
        from app.communications.manager import get_plugin_manager
        pm = get_plugin_manager()
        registry = getattr(pm, "_registry", {})
        return registry.get("webhook_base_url", "http://localhost:8080")
    except Exception:
        return "http://localhost:8080"


@dataclass
class ToolInfo:
    """Enriched tool descriptor returned by load_tools()."""
    name: str
    handler: Callable
    parameters: dict
    tool_id: str = ''
    requires_confirmation: bool = False  # True → treated as destructive (guardrail check)
    destructive: bool = False  # True → tool writes, deletes, or has side-effects


class ToolLoader:
    """Load tools dynamically from the database and compile them into Python functions."""

    def __init__(self):
        self._client = get_db().get_raw_client()

    async def load_tools(self, user_id: str, agent_id: str = "", agent_template_id: Optional[str] = None, session_id: str = "") -> Dict[str, 'ToolInfo']:
        """
        Load all active tools for a user from the tools table.
        Each tool's `code` field contains the full async function to execute.

        Args:
            user_id: The user ID to load tools for
            agent_template_id: Active agent template id.

        Returns:
            Dictionary mapping tool names to ToolInfo objects
        """
        tools: Dict[str, ToolInfo] = {}

        # Load all active tools for this user
        rows = await self._fetch_user_tools(user_id)
        for row in rows:
            name = row['name']
            handler = self._make_handler(row, user_id)
            params = row.get('parameters', {"type": "object", "properties": {}, "required": []})
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except (json.JSONDecodeError, TypeError):
                    params = {"type": "object", "properties": {}, "required": []}
            # Ensure params is a proper JSON Schema object
            if not isinstance(params, dict) or params.get("type") != "object":
                params = {"type": "object", "properties": {}, "required": []}
            tools[name] = ToolInfo(
                name=name,
                handler=handler,
                parameters=params,
                tool_id=row.get('id', ''),
                requires_confirmation=bool(row.get('requires_confirmation', 0)),
            )
            logger.debug(f"Loaded tool {name} for user {user_id}")

        # ── Resolve enabled+admin-configured integrations for this agent ──
        # Computed once so the same set gates both the integration tools and
        # the built-in OAuth helpers (check_oauth_connection).
        enabled_providers: set = set()
        try:
            from app.integrations import gather_enabled_providers
            enabled_providers = await gather_enabled_providers(agent_id, user_id) if agent_id else set()
        except ImportError:
            pass
        except Exception as e:
            logger.warning("Could not gather enabled providers for %s: %s", agent_id, e)

        # ── Edition gate ──
        # In a non-`full` build, drop abilities the active edition excludes (by
        # maturity). This is the single chokepoint that gates every built-in
        # ability's tools (web_access, browser_control, …). No-op for `full`;
        # unknown keys (integration provider connection-types) are kept as-is.
        try:
            from app.features.gating import ability_enabled
            enabled_providers = {p for p in enabled_providers if ability_enabled(p)}
        except Exception:
            pass

        # ── Inject built-in tools (override any DB versions) ──
        self._inject_builtin_tools(
            tools, user_id,
            agent_id=agent_id,
            agent_template_id=agent_template_id,
            enabled_providers=enabled_providers,
            session_id=session_id,
        )

        # ── Inject synthetic tools from per-agent attached data sources ──
        if agent_id:
            try:
                await self._inject_data_source_tools(tools, agent_id)
            except Exception as e:
                logger.warning("data source tool injection failed for agent %s: %s", agent_id, e)

        # ── Inject OAuth-integration tools (Gmail, Calendar, Drive, …) ──
        try:
            from app.integrations import inject_integration_tools
            inject_integration_tools(tools, user_id, agent_id, enabled_providers=enabled_providers, tool_info_cls=ToolInfo)
        except ImportError:
            pass  # app/integrations/ deleted — feature off.
        except Exception as e:
            logger.warning("integration tool injection failed for agent %s: %s", agent_id, e)

        return tools

    async def _inject_data_source_tools(self, tools: Dict[str, ToolInfo], agent_id: str) -> None:
        """Merge synthetic tools from all enabled data sources attached to this agent.

        Synthetic tools are rebuilt fresh on every load() so config edits take
        effect immediately. They are NOT persisted in the `tools` table.
        """
        from app.db import get_db
        from app.connectors import get_connector

        db = get_db()
        attachments = await db.agent_data_source_list(agent_id, enabled_only=True)
        if not attachments:
            return

        # Build the auth resolver once — caches lookups for the duration of load.
        auth_cache: Dict[str, Optional[dict]] = {}

        async def _lookup(aid: Optional[str]) -> Optional[dict]:
            if not aid:
                return None
            if aid in auth_cache:
                return auth_cache[aid]
            try:
                client = db.get_raw_client()
                res = client.table("auth_elements").select("*").eq("id", aid).limit(1).execute()
                row = res.data[0] if res.data else None
            except Exception:
                row = None
            auth_cache[aid] = row
            return row

        for att in attachments:
            ds = {
                "id": att.get("data_source_id"),
                "user_id": att.get("owner_user_id"),
                "name": att.get("name"),
                "type": att.get("type"),
                "config": att.get("config") or {},
                "auth_element_id": att.get("auth_element_id"),
                "schema_cache": att.get("schema_cache") or {},
                "safety_policy": att.get("safety_policy") or {},
                "status": att.get("status"),
            }
            try:
                connector = get_connector(ds["type"])
            except Exception as e:
                logger.warning("connector missing for type %s: %s", ds.get("type"), e)
                continue
            # Pre-resolve credential into the closure to avoid event-loop juggling.
            auth_row = await _lookup(ds.get("auth_element_id"))

            def _resolver(_aid, _cached=auth_row):
                return _cached

            try:
                generated = connector.generated_tools(ds, att, _resolver)
            except Exception as e:
                logger.warning("generated_tools failed for %s: %s", ds.get("name"), e)
                continue
            for gt in generated:
                if gt.name in tools:
                    logger.debug("data source tool %s overrides existing entry", gt.name)
                tools[gt.name] = ToolInfo(
                    name=gt.name,
                    handler=gt.handler,
                    parameters=gt.parameters,
                    tool_id=f"ds:{ds.get('id','')}:{gt.name}",
                    requires_confirmation=bool(gt.destructive),
                )

    def _inject_builtin_tools(self, tools: Dict[str, ToolInfo], user_id: str, agent_id: str = "", agent_template_id: Optional[str] = None, enabled_providers: Optional[set] = None, session_id: str = "") -> None:
        """Inject built-in tools that are always available regardless of DB state.

        `enabled_providers` is the set of integration connection_types enabled
        for this agent AND admin-configured. It gates the OAuth helper tools
        (check_oauth_connection) so the LLM never sees provider names it has
        no permission to use.

        ⚠ DROP-IN POLICY — do NOT add a new `if "<ability>" in enabled_providers:`
        block here to wire a new ability's tools. The per-ability blocks below
        (terminal_control, app_control, wiki_control, …) are LEGACY core-wired
        abilities. A NEW tool-bearing ability ships its own handlers in its plugin
        file via a `build_tools()` hook and is injected automatically by the ONE
        generic block further down (search "Self-contained ability tools"). Drop a
        file in plugins/abilities/ — wire nothing here. See CLAUDE.md "Core vs.
        plugins" and plugins/abilities/_TEMPLATE.py ("TWO FLAVOURS OF ABILITY").
        """
        if enabled_providers is None:
            enabled_providers = set()

        # ── Orchestration gating ─────────────────────────────────────────────
        # `_is_opt_agent`   → this run IS the optimizer's own Planner/Closer.
        #                     Only these get the internal optimizer-pipeline
        #                     tools (run_worker_trials / handoff_to_closer /
        #                     deploy_optimization). No normal agent ever sees
        #                     them — otherwise agents (e.g. admin) discover them
        #                     via list_tools/search_tools and loop on them.
        # `_orchestration_on` → the agent admin enabled the "agent_orchestration"
        #                     ability for this agent. Gates the opt-in tools that
        #                     let an agent reach OTHER agents/pipelines:
        #                     run_optimizer + delegate_to_agent. Off by default.
        _is_opt_agent = agent_template_id in ("opt_planner", "opt_closer")
        _orchestration_on = "agent_orchestration" in enabled_providers

        # create_tool (create_tools ability) → moved to plugins/abilities/create_tools.py build_tools.

        # ── rate_skill (record user feedback on tool executions) ──
        async def _rate_skill_wrapper(skill_name: str, feedback_type: str, message: Optional[str] = None):
            db = get_db()
            skill_id = await db.skill_get_id_by_name(user_id, skill_name)
            if not skill_id:
                return json.dumps({"status": "error", "message": f"Skill '{skill_name}' not found"})
            fid = await db.skill_add_feedback(
                skill_id=skill_id, user_id=user_id,
                feedback_type=feedback_type, message=message,
            )
            return json.dumps({"status": "ok", "feedback_id": fid})

        tools["rate_skill"] = ToolInfo(
            name="rate_skill",
            handler=_rate_skill_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "skill_name": {"type": "string", "description": "Name of the skill/tool to rate"},
                    "feedback_type": {
                        "type": "string",
                        "enum": ["positive", "negative", "correction"],
                        "description": "positive = user liked it, negative = user disliked it, correction = user provided a fix"
                    },
                    "message": {"type": "string", "description": "Optional user comment or correction"},
                },
                "required": ["skill_name", "feedback_type"],
            },
        )

        # ── run_optimizer (trigger interactive optimizer session) ──
        # Opt-in: only injected when the agent has the "agent_orchestration"
        # ability enabled. Skipped for optimizer sub-agents (recursion guard).
        if not user_id.startswith("opt_") and _orchestration_on:
            async def _run_optimizer_wrapper(feedback: str = "", skill_name: str = "", criteria: str = ""):
                """Start an interactive optimizer session. User chats with the Planner agent."""
                # Safety check: if we're already in an optimizer session, don't create another
                from app.db import get_db as _get_db
                _dbc = _get_db()._get_conn()
                _recent = _dbc.execute(
                    "SELECT metadata FROM sessions WHERE user_id=? AND id LIKE 'optimizer-%' ORDER BY created_at DESC LIMIT 1",
                    (user_id,)
                ).fetchone()
                _dbc.close()
                if _recent and _recent[0]:
                    import json as _jm
                    _meta = _jm.loads(_recent[0])
                    if _meta.get('opt_role'):
                        # We're being called from within an optimizer session — skip
                        return _jm.dumps({"status": "skipped", "message": "Already in an optimizer session."})
                import httpx
                try:
                    async with httpx.AsyncClient(timeout=15.0) as hclient:
                        resp = await hclient.post(
                            f"http://127.0.0.1:{os.environ.get('PORT', '8080')}/admin/settings/optimizer/run",
                            params={"user_id": user_id, "session_id": "", "feedback": feedback},
                        )
                        result = resp.json()
                    if result.get("status") == "session_created":
                        return json.dumps({
                            "status": "completed",
                            "optimizer_session_id": result["optimizer_session_id"],
                            "message": f"Optimization session ready. Go to the optimizer session to talk to the Planner."
                        })
                    return json.dumps({"status": "error", "message": result.get("message", "Unknown error")})
                except Exception as e:
                    from app.admin.settings import load_provider_for_user
                    import uuid, json as jmod
                    from app.db import get_db as _get_db
                    await load_provider_for_user(user_id)
                    opt_sid = f"optimizer-{user_id[:8]}-{str(uuid.uuid4())[:8]}"
                    db_conn = _get_db()._get_conn()
                    db_conn.execute(
                        "INSERT OR IGNORE INTO sessions (id,user_id,title,metadata,created_at,updated_at) VALUES (?,?,?,?,datetime('now'),datetime('now'))",
                        (opt_sid, user_id, f"Optimizer - {opt_sid[:12]}", jmod.dumps({"opt_role": "planner"}))
                    )
                    db_conn.execute(
                        "INSERT INTO interactions (id,session_id,role,content,source,channel,created_at) VALUES (?,?,'user',?,'optimizer:trigger','optimizer',datetime('now'))",
                        (str(uuid.uuid4()), opt_sid, f"I need help optimizing this session. Feedback: {feedback or 'General optimization'}.")
                    )
                    db_conn.commit()
                    db_conn.close()
                    return jmod.dumps({"status": "session_created", "optimizer_session_id": opt_sid,
                                       "message": f"Optimization session created. Go to the optimizer session to talk to the Planner."})

            tools["run_optimizer"] = ToolInfo(
                name="run_optimizer",
                handler=_run_optimizer_wrapper,
                parameters={
                    "type": "object",
                    "properties": {
                        "skill_name": {"type": "string", "description": "Optional: specific skill to optimize (e.g. 'send_email'). If blank, analyzes all skills."},
                        "feedback": {"type": "string", "description": "Optional: what to improve. E.g. 'make it use the API instead of scraping' or 'response was too verbose'"},
                        "criteria": {"type": "string", "description": "Optional: which metric to optimize. 'turns' (fewer back-and-forths), 'tokens' (cheaper), or 'time' (faster). If blank, balances all."},
                    },
                    "required": [],
                },
            )

        # ── read_attachment (always available) ──
        from app.tools.read_attachment import read_attachment as _builtin_read_attachment, TOOL_DEFINITION as _ATTACH_TOOL_DEF
        tools["read_attachment"] = ToolInfo(
            name="read_attachment",
            handler=_builtin_read_attachment,
            parameters=_ATTACH_TOOL_DEF["parameters"],
        )

        # ── Communication plugin tools (Telegram, WhatsApp, etc.) ──
        # Only inject a channel's outbound tools when that channel is actually
        # active for THIS agent — `enabled_providers` already encodes both gates
        # (app admin enabled the channel ∩ the agent has the connection on). A
        # disabled/coming-soon channel therefore injects nothing, so a stray bot
        # token can't expose send_telegram_message and have a call raise
        # "bot token not configured" — the source of the spurious Telegram errors.
        try:
            from app.communications.manager import get_plugin_manager
            pm = get_plugin_manager()
            for tool_def in pm.get_all_tools():
                _name = tool_def["name"]
                if _name in tools:
                    continue
                _plugin = next(
                    (p for p in pm.get_enabled_plugins()
                     if any(t["name"] == _name for t in p.get_tools())),
                    None
                )
                if _plugin is None:
                    continue
                # Channel must be active for this agent (admin-enabled + connected).
                if _plugin.name not in enabled_providers:
                    continue
                # Closure capture by value via default args
                def _build_handler(_p, _n):
                    async def _handler(**kw):
                        text = kw.get("text", "")
                        recipient = kw.get("chat_id") or kw.get("to") or kw.get("phone")
                        if not recipient:
                            return json.dumps({"error": f"missing recipient for {_n}"})
                        return await _p.send_message(str(recipient), text)
                    return _handler
                tools[_name] = ToolInfo(
                    name=_name,
                    handler=_build_handler(_plugin, _name),
                    parameters=tool_def["parameters"],
                )
        except Exception as e:
            logger.warning("Failed to inject communication plugin tools: %s", e)

        # ── register_user (auth tool, always available) ──
        async def _register_user_wrapper(name: str, source_channel: str = ""):
            """Register a user who just verified their channel identity."""
            import json
            from app.communications.auth import (
                get_identity, upgrade_to_verified,
                find_user_by_display_name, migrate_anonymous_to_user,
            )

            chan, ext_id = None, None
            if ":" in user_id:
                chan, ext_id = user_id.split(":", 1)
            else:
                try:
                    _raw = get_db().get_raw_client()
                    resp = (
                        _raw.table("channel_identities")
                        .select("channel, external_id")
                        .eq("user_id", user_id)
                        .limit(1)
                        .execute()
                    )
                    rows = resp.data if hasattr(resp, 'data') else (resp or [])
                    if rows:
                        chan, ext_id = rows[0]["channel"], rows[0]["external_id"]
                except Exception:
                    pass

            if not chan or not ext_id:
                return json.dumps({"error": "cannot determine channel identity"})

            identity = await get_identity(chan, ext_id)
            if identity is None:
                return json.dumps({"error": "identity not found"})

            if identity.user_tier == "full":
                return json.dumps({"status": "ok", "message": "Already fully registered."})

            existing_uid = await find_user_by_display_name(name)
            if existing_uid and existing_uid != user_id:
                moved = await migrate_anonymous_to_user(user_id, existing_uid)
                identity = await upgrade_to_verified(identity, display_name=name)
                return json.dumps({
                    "status": "ok",
                    "user_tier": identity.user_tier,
                    "display_name": identity.display_name,
                    "migrated_to": existing_uid,
                    "interactions_moved": moved,
                    "message": f"Welcome back, {name}! Your messages have been linked to your existing account.",
                })

            identity = await upgrade_to_verified(identity, display_name=name)
            return json.dumps({
                "status": "ok",
                "user_tier": identity.user_tier,
                "display_name": identity.display_name,
                "message": f"User registered as {name} on {chan}.",
            })

        tools["register_user"] = ToolInfo(
            name="register_user",
            handler=_register_user_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "User's display name"},
                    "source_channel": {"type": "string", "description": "Which channel they came from (telegram, whatsapp, etc.)"},
                },
                "required": ["name"],
            },
        )

        # codebase_admin source suite → moved to plugins/abilities/codebase_admin.py build_tools.
        # git_control → moved to plugins/abilities/git_control.py build_tools.
        # ui_admin → moved to plugins/abilities/ui_admin.py build_tools (uses enabled_providers).
        # visualizer → moved to plugins/abilities/visualizer.py build_tools.

        # ── Delegation tools — OPT-IN, non-pipeline agents only ──
        # Allows agents to hand off to each other mid-conversation. Gated by the
        # "agent_orchestration" ability so a normal agent can't auto-discover
        # and hand off to (or loop on) other agents/workers. Off by default.
        _is_pipeline = agent_template_id in ("opt_planner", "opt_closer")
        if not _is_pipeline and _orchestration_on:
            try:
                from app.tools.delegation import build_delegation_tools
                _delegation = build_delegation_tools(user_id)
                _delegation_schemas = {
                    "delegate_to_agent": {
                        "type": "object",
                        "properties": {
                            "agent_template_id": {"type": "string", "description": "Template ID of the agent to delegate to (e.g. 'admin-agent')."},
                            "context": {"type": "string", "description": "Context or reason for the delegation — passed to the new agent."},
                        },
                        "required": ["agent_template_id"],
                    },
                    "list_delegatable_agents": {
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                }
                for _dname, _dhandler in _delegation.items():
                    # NOTE: ToolInfo has no `description` field — the loop derives
                    # the description from the handler's docstring. Passing it
                    # here used to raise TypeError, silently disabling delegation.
                    tools[_dname] = ToolInfo(
                        name=_dname,
                        handler=_dhandler,
                        parameters=_delegation_schemas.get(_dname, {"type": "object", "properties": {}, "required": []}),
                    )
            except Exception as _de:
                logger.warning("Delegation tools unavailable: %s", _de)

        # terminal_control → moved to plugins/abilities/terminal_control.py build_tools.
        # app_control → moved to plugins/abilities/app_control.py build_tools.
        # wiki_control → moved to plugins/abilities/wiki_control.py build_tools.

        # ── Self-contained ability tools (generic drop-in discovery) ──────────
        # The blocks above wire specific abilities whose handlers live in core.
        # This ONE generic block covers the newer contract: an ability that ships
        # its OWN handlers in its plugin file (plugins/abilities/<id>.py) via a
        # module-level build_tools() hook — exactly like an integration carries
        # its TOOLS. For every ENABLED such ability we call its build_tools and
        # inject what it returns. Adding a tool-bearing ability therefore needs NO
        # edit here: drop the plugin file in and it is discovered. The ability
        # owns its own gating (it may return {} to inject nothing this call).
        try:
            from app import abilities as _abilities_mgr
            for _ab_id in list(enabled_providers or ()):
                _ab_mod = _abilities_mgr.ability_module(_ab_id)
                _build = getattr(_ab_mod, "build_tools", None) if _ab_mod else None
                if not callable(_build):
                    continue
                try:
                    _built = _build(
                        user_id=user_id, session_id=session_id,
                        agent_id=agent_id or "", agent_template_id=agent_template_id,
                        enabled_providers=enabled_providers,
                    ) or {}
                except Exception as _abe:
                    logger.warning("Ability %s build_tools failed: %s", _ab_id, _abe)
                    continue
                _ab_schemas = getattr(_ab_mod, "TOOL_SCHEMAS", {}) or {}
                _ab_destr = set(getattr(_ab_mod, "DESTRUCTIVE", ()) or ())
                for _abname, _abhandler in _built.items():
                    _abd = _abname in _ab_destr
                    tools[_abname] = ToolInfo(
                        name=_abname,
                        handler=_abhandler,
                        parameters=_ab_schemas.get(
                            _abname, {"type": "object", "properties": {}, "required": []}
                        ),
                        destructive=_abd,
                        requires_confirmation=_abd,
                    )
        except Exception as _abme:
            logger.warning("Drop-in ability tools unavailable: %s", _abme)

        # ═══════════════════════════════════════════════════════════════
        # Bootstrap core tools — always available from turn 1
        # These are the agent's discovery and essential utilities.
        # All other tools are discovered via list_tools / search_tools.
        # ═══════════════════════════════════════════════════════════════

        # web_search / db_query / get_weather / http_request used to be imported
        # here too; they now ship with their owning abilities' build_tools
        # (web_access, codebase_admin, browser_control) and are no longer wired
        # in core.
        from app.tools.core_tools import (
            memory as _core_memory,
            session_search as _core_session_search,
            get_time as _core_get_time,
            get_date as _core_get_date,
            calculate as _core_calculate,
        )

        # ── load_tool (core) — activate a discoverable tool ──────────────────
        # Discoverable tools appear by name + one-line description in the
        # generated # [TOOLS] index, but their full JSON schema is withheld to
        # keep context lean. load_tool returns the tool's full input schema AND
        # records it in the session's active-tools list, so the loop starts
        # sending its real schema on subsequent turns and it becomes callable.
        # Mirrors load_skill. Every loaded tool lives in `tools` regardless of
        # mode (mode only decides whether the schema is sent), so this closure —
        # capturing the fully-populated dict — can resolve any of them.
        async def _load_tool_wrapper(name: str):
            """Activate a tool listed under "Load on demand" in the [TOOLS] section. Returns its full input schema and keeps it callable for the rest of the conversation."""
            info = tools.get(name)
            if info is None:
                return json.dumps({
                    "status": "error",
                    "message": (
                        f"No tool named '{name}' is available to you. Only load "
                        f"tools listed in the [TOOLS] section of your prompt."
                    ),
                })
            if session_id:
                try:
                    from app.db import get_db as _gd
                    await _gd().set_session_active_tool(session_id, name, True)
                except Exception as e:
                    logger.debug("set_session_active_tool failed: %s", e)
            desc = (info.handler.__doc__ or "").strip()
            return json.dumps({
                "status": "ok",
                "tool": {
                    "name": name,
                    "description": desc,
                    "parameters": info.parameters,
                },
                "message": (
                    f"Tool '{name}' is now active. You can call it directly for "
                    f"the rest of this conversation."
                ),
            })

        tools["load_tool"] = ToolInfo(
            name="load_tool",
            handler=_load_tool_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact name of the tool to activate (from the [TOOLS] section).",
                    },
                },
                "required": ["name"],
            },
        )

        # ── On-demand skills (list_skills / load_skill) ──
        # Always available (mirrors the tool-discovery bootstrap). The agent
        # learns which skills exist from the `# [SKILLS]` prompt section and
        # uses load_skill to pull a selectable skill's full body into context.
        from app.tools.core_tools import (
            list_skills as _core_list_skills,
            load_skill as _core_load_skill,
        )

        async def _list_skills_wrapper():
            return await _core_list_skills(agent_id=agent_id, session_id=session_id)
        _list_skills_wrapper.__doc__ = _core_list_skills.__doc__

        tools["list_skills"] = ToolInfo(
            name="list_skills",
            handler=_list_skills_wrapper,
            parameters={"type": "object", "properties": {}, "required": []},
        )

        async def _load_skill_wrapper(name: str):
            return await _core_load_skill(name=name, agent_id=agent_id, session_id=session_id)
        _load_skill_wrapper.__doc__ = _core_load_skill.__doc__

        tools["load_skill"] = ToolInfo(
            name="load_skill",
            handler=_load_skill_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Exact name of the skill to load (from the [SKILLS] section).",
                    },
                },
                "required": ["name"],
            },
        )

        # web_access (web_search, maps_geocode, get_weather) → moved to plugins/abilities/web_access.py build_tools.

        # diagnostics (read_diagnostics) → moved to plugins/abilities/diagnostics.py build_tools.

        # agent_management (list/create/update agents, prompt + ability + skill edits)
        # → moved to plugins/abilities/agent_management.py build_tools.

        # browser_control (browser_action, http_request) -> moved to plugins/abilities/browser_control.py build_tools.
        # image_generation (generate_image) -> moved to plugins/abilities/image_generation.py build_tools.
        # codebase_admin db_query (context documents) -> moved to plugins/abilities/codebase_admin.py build_tools.

        # ── Memory (persistent knowledge pages) ──
        async def _memory_wrapper(
            action: str,
            slug: Optional[str] = None,
            query: Optional[str] = None,
            page_type: Optional[str] = None,
            title: Optional[str] = None,
            compiled_truth: Optional[str] = None,
            timeline: Optional[str] = None,
            limit: int = 10,
        ):
            # Block memory access for simulated/worker-trial sessions.
            # These use user IDs of the form "worker-test-<id>" and should
            # never read from or write to the real memory store.
            if user_id.startswith("worker-test-"):
                return json.dumps({
                    "status": "skipped",
                    "message": "Memory is disabled in simulated sessions.",
                })
            return await _core_memory(
                action=action,
                slug=slug,
                query=query,
                page_type=page_type,
                title=title,
                compiled_truth=compiled_truth,
                timeline=timeline,
                limit=limit,
                user_id=user_id,
            )

        tools["memory"] = ToolInfo(
            name="memory",
            handler=_memory_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["search", "list", "get", "upsert", "delete"],
                        "description": "Action: search, list, get, upsert, or delete",
                    },
                    "slug": {"type": "string", "description": "Unique page identifier — for get/upsert/delete actions"},
                    "query": {"type": "string", "description": "Search query — for search action"},
                    "page_type": {"type": "string", "description": "Page type (note, meeting, project, person) — for list/upsert actions"},
                    "title": {"type": "string", "description": "Page title — for upsert action"},
                    "compiled_truth": {"type": "string", "description": "Main content body — for upsert action"},
                    "timeline": {"type": "string", "description": "Optional timeline entry — for upsert action"},
                    "limit": {"type": "integer", "description": "Max results for search action (default 10)", "default": 10},
                },
                "required": ["action"],
            },
        )

        # ── Session search ──
        async def _session_search_wrapper(query: str, limit: int = 10):
            return await _core_session_search(query=query, limit=limit, user_id=user_id)

        tools["session_search"] = ToolInfo(
            name="session_search",
            handler=_session_search_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword to search for in past conversations"},
                    "limit": {"type": "integer", "description": "Max results (default 10)", "default": 10},
                },
                "required": ["query"],
            },
        )

        # ── Time & Date ──
        tools["get_time"] = ToolInfo(
            name="get_time",
            handler=_core_get_time,
            parameters={
                "type": "object",
                "properties": {
                    "timezone": {"type": "string", "description": "IANA timezone (e.g. 'America/New_York', 'Europe/London'). Defaults to UTC.", "default": "UTC"},
                },
                "required": [],
            },
        )

        tools["get_date"] = ToolInfo(
            name="get_date",
            handler=_core_get_date,
            parameters={
                "type": "object",
                "properties": {
                    "timezone": {"type": "string", "description": "IANA timezone (e.g. 'America/New_York'). Defaults to UTC.", "default": "UTC"},
                    "format": {"type": "string", "enum": ["full", "short", "iso"], "description": "Date format: full (Monday, May 8, 2026), short (2026-05-08), iso (ISO 8601)", "default": "full"},
                },
                "required": [],
            },
        )

        # web_access get_weather (same ability as web_search) → moved to plugins/abilities/web_access.py build_tools.

        # ── Calculator ──
        tools["calculate"] = ToolInfo(
            name="calculate",
            handler=_core_calculate,
            parameters={
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "Mathematical expression (e.g. '2 + 2', 'sin(pi/4)', 'sqrt(144)')"},
                },
                "required": ["expression"],
            },
        )

        # browser_control http_request (same ability as browser_action) → moved to plugins/abilities/browser_control.py build_tools.

        # ── Webhook management (generic inbound webhooks) ──
        async def _register_webhook_wrapper(name: str, instructions: str = ""):
            """Register a new generic inbound webhook endpoint."""
            from app.db import get_db
            db = get_db()
            result = await db.register_webhook(
                user_id=user_id,
                name=name,
                instructions=instructions,
            )
            webhook_url = _get_webhook_base_url() + f"/api/v1/webhooks/generic/{result['id']}"
            result["url"] = webhook_url
            return json.dumps({
                "status": "ok",
                "webhook": result,
                "message": f"Webhook '{name}' registered at {webhook_url}",
            })

        tools["register_webhook"] = ToolInfo(
            name="register_webhook",
            handler=_register_webhook_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Human-readable name for this webhook (e.g. 'GitHub push events')"},
                    "instructions": {"type": "string", "description": "Instructions for the agent when this webhook fires (e.g. 'Analyze the push payload and summarize changes')"},
                },
                "required": ["name"],
            },
        )

        async def _list_webhooks_wrapper():
            """List all registered webhooks for the current user."""
            from app.db import get_db
            db = get_db()
            hooks = await db.list_webhooks(user_id=user_id)
            base_url = _get_webhook_base_url()
            for h in hooks:
                h["url"] = base_url + f"/api/v1/webhooks/generic/{h['id']}"
            return json.dumps({"status": "ok", "webhooks": hooks, "count": len(hooks)})

        tools["list_webhooks"] = ToolInfo(
            name="list_webhooks",
            handler=_list_webhooks_wrapper,
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

        async def _delete_webhook_wrapper(webhook_id: str):
            """Delete a webhook registration by id."""
            from app.db import get_db
            db = get_db()
            ok = await db.delete_webhook(webhook_id=webhook_id, user_id=user_id)
            if ok:
                return json.dumps({"status": "ok", "message": f"Webhook {webhook_id} deleted"})
            return json.dumps({"status": "error", "message": f"Webhook {webhook_id} not found or not owned by user"})

        tools["delete_webhook"] = ToolInfo(
            name="delete_webhook",
            handler=_delete_webhook_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "webhook_id": {"type": "string", "description": "The webhook registration id to delete"},
                },
                "required": ["webhook_id"],
            },
        )

        async def _get_webhook_log_wrapper(webhook_id: str, limit: int = 10):
            """View recent events for a webhook registration."""
            from app.db import get_db
            db = get_db()
            # Verify ownership
            reg = await db.get_webhook(webhook_id=webhook_id)
            if not reg or reg.get("user_id") != user_id:
                return json.dumps({"status": "error", "message": "Webhook not found or not owned by user"})
            events = await db.get_webhook_logs(webhook_id=webhook_id, limit=limit)
            return json.dumps({"status": "ok", "events": events, "count": len(events)})

        tools["get_webhook_log"] = ToolInfo(
            name="get_webhook_log",
            handler=_get_webhook_log_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "webhook_id": {"type": "string", "description": "The webhook registration id"},
                    "limit": {"type": "integer", "description": "Max events to return", "default": 10},
                },
                "required": ["webhook_id"],
            },
        )

        # automation event subscriptions (list_event_sources, list_delivery_channels,
        # event_subscribe, list_event_subscriptions, event_unsubscribe)
        # -> moved to plugins/abilities/automation.py build_tools.


        # ── Optimizer tools (Planner / Closer subagents) ──
        from app.tools.optimizer_tools import run_worker_trials, handoff_to_closer, deploy_optimization

        async def _run_worker_trials_wrapper(changes_json: str = ""):
            import logging as _log
            import sqlite3, uuid as _uid, traceback as _tb
            _log.warning(f"_WRAPPER CALLED: user_id={user_id}")
            try:
                # Find latest optimizer session
                from app.db import get_db as _gdb; db = _gdb()._get_conn()
                row = db.execute(
                    "SELECT id FROM sessions WHERE id LIKE 'optimizer-%' ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                sid = row[0] if row else f"optimizer-{str(_uid.uuid4())[:8]}"
                db.close()
                _log.warning(f"_WRAPPER: sid={sid}")
                result = await run_worker_trials(changes_json=changes_json, user_id=user_id, session_id=sid)
                _log.warning(f"_WRAPPER SUCCESS: len={len(result)}")
                return result
            except Exception as e:
                tb_str = _tb.format_exc()
                _log.error(f"_WRAPPER EXCEPTION: {type(e).__name__}: {e}\n{tb_str[:500]}")
                return json.dumps({"status": "error", "message": f"{type(e).__name__}: {e}", "traceback": tb_str[:500]})
        tools["run_worker_trials"] = ToolInfo(
            name="run_worker_trials",
            handler=_run_worker_trials_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "changes_json": {"type": "string", "description": "JSON array of changes to test, where each change has element, element_type, change_type, old_excerpt, new_content, reasoning"},
                },
                "required": ["changes_json"],
            },
        )

        async def _handoff_to_closer_wrapper(summary: str = "", judging_criteria: str = "",
                                                  baseline_transcript: str = "", worker_results: str = ""):
            import sqlite3, uuid as _uid
            from app.db import get_db as _gdb; db = _gdb()._get_conn()
            row = db.execute(
                "SELECT id FROM sessions WHERE id LIKE 'optimizer-%' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            sid = row[0] if row else f"optimizer-{str(_uid.uuid4())[:8]}"
            db.close()
            return await handoff_to_closer(
                summary=summary, user_id=user_id, session_id=sid,
                judging_criteria=judging_criteria,
                baseline_transcript=baseline_transcript,
                worker_results=worker_results,
            )
        tools["handoff_to_closer"] = ToolInfo(
            name="handoff_to_closer",
            handler=_handoff_to_closer_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Summary of what was discussed and decided to pass to the Closer"},
                    "judging_criteria": {"type": "string", "description": "Criteria used to judge worker trial quality, set by Planner + user"},
                    "baseline_transcript": {"type": "string", "description": "Original user question + agent answer transcript before optimization"},
                    "worker_results": {"type": "string", "description": "Worker trial results and transcripts for each proposed change"},
                },
                "required": ["summary"],
            },
        )

        async def _deploy_optimization_wrapper(changes_json: str = ""):
            import sqlite3, uuid as _uid
            from app.db import get_db as _gdb; db = _gdb()._get_conn()
            row = db.execute(
                "SELECT id FROM sessions WHERE id LIKE 'optimizer-%' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            sid = row[0] if row else f"optimizer-{str(_uid.uuid4())[:8]}"
            db.close()
            return await deploy_optimization(changes_json=changes_json, user_id=user_id, session_id=sid)
        tools["deploy_optimization"] = ToolInfo(
            name="deploy_optimization",
            handler=_deploy_optimization_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "changes_json": {"type": "string", "description": "JSON array of approved changes to deploy to the target agent"},
                },
                "required": ["changes_json"],
            },
        )

        # ── Lock optimizer-pipeline tools to the Planner/Closer ──────────────
        # The wrappers above are defined unconditionally (cheap closures), but a
        # normal agent must never be able to CALL them. Remove them unless this
        # run is the optimizer's own sub-agent. This is the guard that stops the
        # admin agent (or any agent) from invoking run_worker_trials et al.
        if not _is_opt_agent:
            for _opt_only in ("run_worker_trials", "handoff_to_closer", "deploy_optimization"):
                tools.pop(_opt_only, None)

        # ── check_oauth_connection ──────────────────────────────────────────────
        # Only registered when at least one OAuth provider is enabled AND
        # admin-configured for this agent. Its provider enum is restricted
        # to those providers so the LLM never learns about capabilities the
        # agent isn't supposed to have.
        _CT_TO_OAUTH = {
            "google": "google", "microsoft": "microsoft", "yahoo": "yahoo",
            "dropbox": "dropbox", "facebook": "meta", "instagram": "meta",
            "twitter": "twitter", "linkedin": "linkedin", "tiktok": "tiktok",
            "pinterest": "pinterest", "reddit": "reddit", "snapchat": "snapchat",
            "twitch": "twitch",
        }
        _oauth_enabled = sorted({_CT_TO_OAUTH[ct] for ct in enabled_providers if ct in _CT_TO_OAUTH})
        if not _oauth_enabled:
            return

        _captured_agent_id = agent_id

        async def _check_oauth_connection_wrapper(provider: str) -> str:
            import json as _json
            from app.db import get_db as _get_db
            from app.admin.integrations import (
                get_google_creds, build_google_authorize_url,
                get_microsoft_creds, build_microsoft_authorize_url,
                get_yahoo_creds, build_yahoo_authorize_url,
                get_dropbox_creds, build_dropbox_authorize_url,
                get_meta_creds, build_meta_authorize_url,
                get_twitter_creds, build_twitter_authorize_url,
                get_linkedin_creds, build_linkedin_authorize_url,
                get_tiktok_creds, build_tiktok_authorize_url,
                get_pinterest_creds, build_pinterest_authorize_url,
                get_reddit_creds, build_reddit_authorize_url,
                get_snapchat_creds, build_snapchat_authorize_url,
                get_twitch_creds, build_twitch_authorize_url,
            )

            _aliases = {"facebook": "meta", "instagram": "meta", "x": "twitter", "gmail": "google", "drive": "google", "calendar": "google", "outlook": "microsoft"}
            provider = _aliases.get(provider.lower().strip(), provider.lower().strip())

            _supported = {
                "google":    (get_google_creds,    build_google_authorize_url,    "Google"),
                "microsoft": (get_microsoft_creds, build_microsoft_authorize_url, "Microsoft"),
                "yahoo":     (get_yahoo_creds,     build_yahoo_authorize_url,     "Yahoo"),
                "dropbox":   (get_dropbox_creds,   build_dropbox_authorize_url,   "Dropbox"),
                "meta":      (get_meta_creds,      build_meta_authorize_url,      "Facebook/Instagram"),
                "twitter":   (get_twitter_creds,   build_twitter_authorize_url,   "Twitter/X"),
                "linkedin":  (get_linkedin_creds,  build_linkedin_authorize_url,  "LinkedIn"),
                "tiktok":    (get_tiktok_creds,    build_tiktok_authorize_url,    "TikTok"),
                "pinterest": (get_pinterest_creds, build_pinterest_authorize_url, "Pinterest"),
                "reddit":    (get_reddit_creds,    build_reddit_authorize_url,    "Reddit"),
                "snapchat":  (get_snapchat_creds,  build_snapchat_authorize_url,  "Snapchat"),
                "twitch":    (get_twitch_creds,    build_twitch_authorize_url,    "Twitch"),
            }

            if provider not in _supported:
                return _json.dumps({
                    "status": "unsupported",
                    "message": f"Provider '{provider}' is not recognized. Supported: {', '.join(_supported)}.",
                })

            get_creds_fn, build_url_fn, display_name = _supported[provider]
            _db = _get_db()

            # Check if integration is enabled for this agent
            if _captured_agent_id:
                try:
                    rows = await _db.get_agent_connections(_captured_agent_id)
                    conn_row = next((r for r in rows if r["connection_type"] == provider), None)
                    if not conn_row or not conn_row.get("enabled"):
                        return _json.dumps({
                            "status": "not_enabled",
                            "message": f"{display_name} integration is not enabled for this agent. Ask your agent admin to enable it in the Integrations tab.",
                        })
                except Exception:
                    pass  # If we can't check, proceed to creds check

            # Check if admin has configured OAuth credentials
            try:
                client_id, _ = await get_creds_fn()
            except Exception:
                client_id = None
            if not client_id:
                return _json.dumps({
                    "status": "not_configured",
                    "message": f"{display_name} integration has not been configured. An admin must set up the OAuth credentials in App Config → Integrations.",
                })

            # Check if user already has a connected token (per-agent scope)
            try:
                from app.integrations.oauth_helper import oauth_label as _oauth_label
                elem = await _db.auth_element_get(user_id, provider, _oauth_label(_captured_agent_id))
                if elem and elem.get("secret_ref"):
                    config = elem.get("config") or {}
                    if isinstance(config, str):
                        try:
                            config = _json.loads(config)
                        except Exception:
                            config = {}
                    email = config.get("email") or config.get("name") or ""
                    display = f" as {email}" if email else ""
                    return _json.dumps({
                        "status": "connected",
                        "message": f"Your {display_name} account is already connected{display}.",
                        "email": email,
                    })
            except Exception:
                pass

            # Generate the connect URL
            try:
                result = await build_url_fn(user_id=user_id, agent_id=_captured_agent_id)
                authorize_url = result[0] if isinstance(result, tuple) else result
            except Exception as e:
                return _json.dumps({
                    "status": "error",
                    "message": f"Could not generate a connect link for {display_name}: {e}",
                })

            return _json.dumps({
                "status": "not_connected",
                "message": f"{display_name} integration is set up. Please connect your account: [{display_name}]({authorize_url})",
                "authorize_url": authorize_url,
            })

        tools["check_oauth_connection"] = ToolInfo(
            name="check_oauth_connection",
            handler=_check_oauth_connection_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "provider": {
                        "type": "string",
                        "enum": _oauth_enabled,
                        "description": (
                            "The OAuth provider to check. Call this when the user wants to do "
                            "something that requires a connected account and you are not sure if "
                            "they have connected it. Allowed values for this agent: "
                            + ", ".join(_oauth_enabled) + "."
                        ),
                    },
                },
                "required": ["provider"],
            },
        )


    def _make_handler(self, row: dict, user_id: str) -> Callable:
        """Compile tool code and wrap it with user context."""
        name = row['name']
        code = row['code']

        if name == 'create_tool':
            async def passthrough(**kwargs):
                from app.tools.registry import create_tool as ct
                return await ct(user_id=user_id, **kwargs)
            return passthrough

        handler = self._compile_tool(code, name)

        # Inject user_id into the handler's kwargs if the function accepts it
        import inspect
        try:
            sig = inspect.signature(handler)
            if 'user_id' in sig.parameters:
                original = handler
                async def wrapped(**kwargs):
                    return await original(user_id=user_id, **kwargs)
                return wrapped
        except (ValueError, TypeError):
            pass

        return handler

    async def _fetch_user_tools(self, user_id: str) -> List[Dict]:
        """Fetch all active tools for a user."""
        try:
            response = (
                self._client.table("tools")
                .select("*")
                .in_("created_by", [user_id, "__system__"])
                .eq("status", "active")
                .execute()
            )
            return response.data or []
        except Exception as e:
            logger.error(f"Error fetching tools for user {user_id}: {e}")
            return []

    def _compile_tool(self, code_string: str, tool_name: str) -> Any:
        """
        Compile a tool code string into a Python function.
        Uses a restricted namespace that strips dangerous builtins
        (open, exec, eval, compile, __import__) as defense-in-depth
        against filesystem/shell/DB access via create_tool.

        Args:
            code_string: Full Python function code
            tool_name: Name of the tool for error reporting

        Returns:
            Compiled Python function object
        """
        try:
            # Strip dangerous builtins — defense-in-depth for create_tool
            safe_builtins = dict(__builtins__)
            for _dangerous in ("open", "exec", "eval", "compile", "__import__"):
                safe_builtins.pop(_dangerous, None)

            compiled = compile(code_string, f"<tool:{tool_name}>", "exec")
            namespace = {"__builtins__": safe_builtins}
            exec(compiled, namespace)
            return namespace[tool_name]
        except Exception as e:
            logger.error(f"Error compiling tool {tool_name}: {e}")
            async def error_tool(*args, **kwargs):
                raise RuntimeError(f"Failed to compile tool {tool_name}: {e}")
            return error_tool


# Global instance
_tool_loader = ToolLoader()


async def load_tools(
    user_id: str,
    agent_id: str = "",
    agent_template_id: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
    custom_tool_ids: Optional[List[str]] = None,
    session_id: str = "",
) -> Dict[str, ToolInfo]:
    """
    Load all active tools for a user.

    Args:
        user_id: The user ID to load tools for.
        agent_template_id: Active agent template id - gates delegation tools;
            pipeline agents skip delegation tools.
        allowed_tools: List of Tier-2 tool names that are DISABLED for this
            agent. Empty list means all Tier-2 tools are enabled.
            Tier-0 (admin) and Tier-1 (always-on) tools are never filtered.
        custom_tool_ids: Reserved - DB tool IDs opted in (not yet enforced).

    Returns:
        Dictionary mapping tool names to ToolInfo objects.
    """
    tools = await _tool_loader.load_tools(user_id, agent_id=agent_id, agent_template_id=agent_template_id, session_id=session_id)

    # Propagate requires_confirmation and destructive from BUILTIN_TOOL_METADATA
    # to built-in ToolInfo entries. DB tools already have these set from their row;
    # built-ins need them applied from metadata.
    for name, info in tools.items():
        if name in BUILTIN_TOOL_METADATA:
            meta = BUILTIN_TOOL_METADATA[name]
            if not info.requires_confirmation and meta.get("requires_confirmation", False):
                info.requires_confirmation = True
            if meta.get("destructive", False):
                info.destructive = True

    # Phase 5: enforce allowed_tools filter.
    # Tier-1 tools are always-on and must never be filtered.
    TIER_1_ALWAYS_ON = {
        "load_tool",
        "list_skills", "load_skill",
        "get_time", "get_date", "calculate", "read_attachment",
        "register_user",
    }
    # NOTE: delegate_to_agent / list_delegatable_agents are deliberately NOT
    # always-on. They are opt-in via the "agent_orchestration" ability and are
    # only injected (in _inject_builtin_tools) when that ability is enabled.
    if allowed_tools:
        disabled = set(allowed_tools)
        for name in list(tools.keys()):
            if name in disabled and name not in TIER_1_ALWAYS_ON:
                del tools[name]

    # Phase 5b: enforce custom_tool_ids (Tier-3 opt-in DB tools).
    # When a non-empty list is provided, DB tools not in that list are removed.
    # Empty list / None = keep all DB tools (backward-compatible default).
    if custom_tool_ids:
        allowed_id_set = set(custom_tool_ids)
        for name in list(tools.keys()):
            ti = tools[name]
            # Only filter tools that came from the DB (have a tool_id)
            if ti.tool_id and ti.tool_id not in allowed_id_set:
                del tools[name]

    return tools

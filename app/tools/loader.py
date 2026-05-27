"""
Tool loader for dynamic tool loading from database.
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
    # ── Core discovery ──
    "list_tools":                    {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "search_tools":                  {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "get_tool_definition":           {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    # ── Web & browser ──
    "web_search":                    {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "browser_action":                {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "http_request":                  {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    "maps_geocode":                  {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    # ── Image generation (gated by image_generation) ──
    "generate_image":                {"stages": ["execute_tools"],                                "destructive": False, "agent_types": []},
    # ── DB & context (gated by codebase_admin) ──
    "db_query":                      {"stages": ["execute_tools"],                                "destructive": True,  "agent_types": []},
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
    # ── Admin/source (privileged) — write/exec tools pass through guardrails ──
    "read_source":                   {"stages": ["execute_tools"],                               "destructive": False, "requires_confirmation": False, "agent_types": ["admin"]},
    "write_source":                  {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": True,  "agent_types": ["admin"]},
    "edit_source":                   {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": True,  "agent_types": ["admin"]},
    "delete_source":                 {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": True,  "agent_types": ["admin"]},
    "run_command":                   {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": True,  "agent_types": ["admin"]},
    "restart_server":                {"stages": ["guardrails", "execute_tools"],                 "destructive": True,  "requires_confirmation": True,  "agent_types": ["admin"]},
    # ── Auth / comms ──
    "register_user":                 {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
    # ── OAuth integration ──
    "check_oauth_connection":        {"stages": ["execute_tools"],                               "destructive": False, "agent_types": []},
}


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
        """
        if enabled_providers is None:
            enabled_providers = set()

        # ── create_tool (gated by the "create_tools" ability) ──
        if "create_tools" in enabled_providers:
            from app.tools.registry import create_tool as _builtin_create_tool, VALID_NODE_IDS as _VALID_NODE_IDS

            async def _create_tool_wrapper(name, description, parameters, code, stages, destructive=False, agent_types=None):
                return await _builtin_create_tool(
                    name=name,
                    description=description,
                    parameters=parameters,
                    code=code,
                    stages=stages,
                    destructive=destructive,
                    agent_types=agent_types,
                    user_id=user_id,
                )

            tools["create_tool"] = ToolInfo(
                name="create_tool",
                handler=_create_tool_wrapper,
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Tool identifier (e.g. 'check_email')"},
                        "description": {"type": "string", "description": "What the tool does (shown to model)"},
                        "parameters": {"type": "object", "description": "JSON Schema describing tool inputs"},
                        "code": {"type": "string", "description": "Full Python async function code. Must contain an async function with the same name as the tool."},
                        "stages": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "REQUIRED. List of loop node IDs where this tool operates. "
                                "Most tools: ['execute_tools']. Memory tools: ['memory_search', 'memory_save']. "
                                f"Valid values: {', '.join(sorted(_VALID_NODE_IDS))}."
                            ),
                        },
                        "destructive": {
                            "type": "boolean",
                            "description": "True if this tool writes, deletes, or has irreversible side-effects.",
                            "default": False,
                        },
                        "agent_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Agent type names that may use this tool. Empty = all agent types.",
                            "default": [],
                        },
                    },
                    "required": ["name", "description", "parameters", "code", "stages"],
                },
            )

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
        # Skip registering for optimizer sub-agents to prevent recursion
        if not user_id.startswith("opt_"):
            async def _run_optimizer_wrapper(feedback: str = "", skill_name: str = "", criteria: str = ""):
                """Start an interactive optimizer session. User chats with the Planner agent."""
                # Safety check: if we're already in an optimizer session, don't create another
                import sqlite3 as _sq3
                _dbc = _sq3.connect("app/db/local.db")
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
                    import uuid, sqlite3, json as jmod
                    await load_provider_for_user(user_id)
                    opt_sid = f"optimizer-{user_id[:8]}-{str(uuid.uuid4())[:8]}"
                    db_conn = sqlite3.connect('app/db/local.db')
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

        # ── Source management tools — gated by the "codebase_admin" ability ──
        # Privileged tools (read/write/edit/delete files, run commands). The
        # app admin enables the ability in App Config → Agent Abilities, and
        # the agent admin turns it on per-agent in the Abilities tab.
        if "codebase_admin" in enabled_providers:
            try:
                from app.admin.source_tools import inject_source_tools
                inject_source_tools(tools, user_id)
            except ImportError:
                pass  # admin/source_tools.py not present — source editing disabled

        # ── Visualizer tools (p5.js creative coding) ──
        try:
            from app.visualizer import register_tools as _register_visualizer_tools
            _register_visualizer_tools(tools, user_id)
        except ImportError:
            pass  # app/visualizer/ not available — visual rendering disabled

        # ── Delegation tools — injected for non-pipeline agents ──
        # Allows agents to hand off to each other mid-conversation.
        _is_pipeline = agent_template_id in ("opt_planner", "opt_closer")
        if not _is_pipeline:
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
                    tools[_dname] = ToolInfo(
                        name=_dname,
                        handler=_dhandler,
                        description=_dhandler.__doc__ or "",
                        parameters=_delegation_schemas.get(_dname, {"type": "object", "properties": {}, "required": []}),
                    )
            except Exception as _de:
                logger.warning("Delegation tools unavailable: %s", _de)

        # ═══════════════════════════════════════════════════════════════
        # Bootstrap core tools — always available from turn 1
        # These are the agent's discovery and essential utilities.
        # All other tools are discovered via list_tools / search_tools.
        # ═══════════════════════════════════════════════════════════════

        from app.tools.core_tools import (
            list_tools as _core_list_tools,
            search_tools as _core_search_tools,
            get_tool_definition as _core_get_tool_definition,
            web_search as _core_web_search,
            db_query as _core_db_query,
            memory as _core_memory,
            session_search as _core_session_search,
            get_time as _core_get_time,
            get_date as _core_get_date,
            get_weather as _core_get_weather,
            calculate as _core_calculate,
            http_request as _core_http_request,
        )

        # ── Tool discovery ──
        BUILTIN_TOOLS = {
            "run_worker_trials": "Run isolated worker test agents to test proposed optimization changes. Each worker creates a test agent, sends the original user message, and returns the full transcript + metrics.",
            "handoff_to_closer": "Hand off optimization results to the Closer agent. Pass summary, judging_criteria, baseline_transcript, and worker_results.",
            "deploy_optimization": "Deploy approved optimization changes to the user's agent. Pass changes_json with element, element_type, and new_content.",
        }

        async def _list_tools_wrapper():
            result = json.loads(await _core_list_tools(user_id=user_id))
            for name, desc in BUILTIN_TOOLS.items():
                if name not in [t.get("name") for t in result.get("tools", [])]:
                    if "tools" not in result:
                        result["tools"] = []
                    result["tools"].append({"name": name, "description": desc})
                    result["count"] = len(result["tools"])
            return json.dumps(result)

        tools["list_tools"] = ToolInfo(
            name="list_tools",
            handler=_list_tools_wrapper,
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

        async def _search_tools_wrapper(query: str):
            result = json.loads(await _core_search_tools(query=query, user_id=user_id))
            matches = result.get("tools", [])
            q = query.lower()
            for name, desc in BUILTIN_TOOLS.items():
                if q in name.lower() or q in desc.lower():
                    if name not in [t.get("name") for t in matches]:
                        matches.append({"name": name, "description": desc})
            result["tools"] = matches
            result["count"] = len(matches)
            return json.dumps(result)

        tools["search_tools"] = ToolInfo(
            name="search_tools",
            handler=_search_tools_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword to search for in tool names and descriptions"},
                },
                "required": ["query"],
            },
        )

        async def _get_tool_definition_wrapper(tool_name: str):
            if tool_name in BUILTIN_TOOLS:
                return json.dumps({"status": "ok", "name": tool_name, "description": BUILTIN_TOOLS[tool_name]})
            return await _core_get_tool_definition(tool_name=tool_name, user_id=user_id)

        tools["get_tool_definition"] = ToolInfo(
            name="get_tool_definition",
            handler=_get_tool_definition_wrapper,
            parameters={
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string", "description": "Name of the tool to look up"},
                },
                "required": ["tool_name"],
            },
        )

        # ── Web Access ability (web_search, get_weather, maps_geocode) ──
        # Gated by the "web_access" toggle in App Config → Agent Abilities.
        # Bundles lightweight web lookup tools that don't drive a real browser.
        if "web_access" in enabled_providers:
            tools["web_search"] = ToolInfo(
                name="web_search",
                handler=_core_web_search,
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The search query"},
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum results to return (default 5, max 10)",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                },
            )

            # Maps & geocoding (Nominatim — free, no API key)
            from app.tools.maps import maps_geocode as _core_maps_geocode, TOOL_PARAMETERS as _MAPS_PARAMS
            tools["maps_geocode"] = ToolInfo(
                name="maps_geocode",
                handler=_core_maps_geocode,
                parameters=_MAPS_PARAMS,
            )

        # ── Browser Control ability (browser_action, http_request) ──
        # Gated by the "browser_control" toggle. Real browser automation +
        # arbitrary outbound HTTP — separate from the lighter Web Access set
        # because these can fetch private endpoints and drive headless Chromium.
        if "browser_control" in enabled_providers:
            from app.tools.browser import browser_action as _core_browser_action

            async def _browser_action_wrapper(
                action: str,
                selector: Optional[str] = None,
                text: Optional[str] = None,
                url: Optional[str] = None,
                js: Optional[str] = None,
                timeout_ms: int = 5000,
                full_page: bool = True,
            ):
                return await _core_browser_action(
                    user_id=user_id,
                    session_id=user_id,
                    action=action,
                    selector=selector,
                    text=text,
                    url=url,
                    js=js,
                    timeout_ms=timeout_ms,
                    full_page=full_page,
                )

            tools["browser_action"] = ToolInfo(
                name="browser_action",
                handler=_browser_action_wrapper,
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["navigate", "click", "type", "get_text", "get_html", "screenshot", "wait", "evaluate", "title", "url", "close"],
                            "description": "Browser action to perform",
                        },
                        "selector": {"type": "string", "description": "CSS selector (for click, type, get_text)"},
                        "text": {"type": "string", "description": "Text to type (for type action)"},
                        "url": {"type": "string", "description": "URL to navigate to (for navigate action)"},
                        "js": {"type": "string", "description": "JavaScript code (for evaluate action)"},
                        "timeout_ms": {"type": "integer", "description": "Wait timeout in ms (default 5000)", "default": 5000},
                        "full_page": {"type": "boolean", "description": "Full page screenshot", "default": True},
                    },
                    "required": ["action"],
                },
            )

        # ── Image Generation ability (generate_image) ──
        # Uses the admin-configured image-gen provider (App Config → Default LLM
        # → Image Generation). Outputs saved under visuals/users/{user_id}/.
        if "image_generation" in enabled_providers:
            from app.tools.image_generation import (
                generate_image as _core_generate_image,
                TOOL_PARAMETERS as _IMG_PARAMS,
            )

            async def _generate_image_wrapper(prompt: str, size: str = "1024x1024",
                                              n: int = 1, quality: Optional[str] = None,
                                              style: Optional[str] = None):
                return await _core_generate_image(
                    user_id=user_id, prompt=prompt, size=size, n=n,
                    quality=quality, style=style,
                )

            tools["generate_image"] = ToolInfo(
                name="generate_image",
                handler=_generate_image_wrapper,
                parameters=_IMG_PARAMS,
            )

        # ── DB query (context documents) — gated by codebase_admin ──
        # Lets an agent read/edit its own prompt slots. Powerful, so it lives
        # under the same ability that exposes file-edit/run-command tools.
        if "codebase_admin" in enabled_providers:
            async def _db_query_wrapper(
                action: str,
                context_type: Optional[str] = None,
                context_id: Optional[str] = None,
                title: Optional[str] = None,
                content: Optional[str] = None,
                tags: Optional[List[str]] = None,
            ):
                return await _core_db_query(
                    action=action,
                    context_type=context_type,
                    context_id=context_id,
                    title=title,
                    content=content,
                    tags=tags,
                    user_id=user_id,
                )

            tools["db_query"] = ToolInfo(
                name="db_query",
                handler=_db_query_wrapper,
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list", "get", "insert", "update", "delete"],
                            "description": "Action to perform: list, get, insert, update, or delete (delete clears content)",
                        },
                        "context_type": {"type": "string", "description": "Document type (agent, user, skills, tools, tasks, memory, project, jobs) — for list/insert actions"},
                        "context_id": {"type": "string", "description": "Document ID — for get/update/delete actions"},
                        "title": {"type": "string", "description": "Title — for insert action"},
                        "content": {"type": "string", "description": "Content body — for insert/update actions"},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags — for insert action"},
                    },
                    "required": ["action"],
                },
            )

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

        # ── Weather (gated by web_access; same ability as web_search) ──
        if "web_access" in enabled_providers:
            tools["get_weather"] = ToolInfo(
                name="get_weather",
                handler=_core_get_weather,
                parameters={
                    "type": "object",
                    "properties": {
                        "location": {"type": "string", "description": "City name or 'lat,lon' coordinates (e.g. 'London', '40.71,-74.01')"},
                        "units": {"type": "string", "enum": ["metric", "imperial"], "description": "metric = Celsius/km/h, imperial = Fahrenheit/mph", "default": "metric"},
                    },
                    "required": ["location"],
                },
            )

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

        # ── HTTP Request (gated by browser_control; same ability as browser_action) ──
        if "browser_control" in enabled_providers:
            tools["http_request"] = ToolInfo(
                name="http_request",
                handler=_core_http_request,
                parameters={
                    "type": "object",
                    "properties": {
                        "method": {
                            "type": "string",
                            "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
                            "description": "HTTP method",
                            "default": "GET",
                        },
                        "url": {"type": "string", "description": "Full URL including scheme (e.g. https://api.example.com/data)"},
                        "headers": {
                            "type": "object",
                            "description": "Optional dict of HTTP headers",
                            "additionalProperties": {"type": "string"},
                            "default": {},
                        },
                        "body": {
                            "type": "object",
                            "description": "Request body (dict for JSON/form, string for text)",
                            "default": {},
                        },
                        "body_type": {
                            "type": "string",
                            "enum": ["json", "form", "text"],
                            "description": "How to encode body",
                            "default": "json",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Request timeout in seconds",
                            "default": 30,
                        },
                    },
                    "required": ["url"],
                },
            )

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

        # ── Event subscriptions (real-time triggers from external sources) ──
        # Gated by the "automation" ability — same toggle that controls the
        # Automation tab. These tools let the chat agent create the same kind
        # of trigger the Automation tab creates: a row in
        # `agent_event_subscriptions` plus a provider-side watch
        # (Gmail Pub/Sub, Graph subscription, etc.).
        if "automation" in enabled_providers:
            async def _list_event_sources_wrapper():
                try:
                    from app.events import get_manager
                    mgr = get_manager()
                    return json.dumps({
                        "status": "ok",
                        "sources": mgr.manifest(),
                    })
                except Exception as e:
                    return json.dumps({"status": "error", "message": str(e)})

            tools["list_event_sources"] = ToolInfo(
                name="list_event_sources",
                handler=_list_event_sources_wrapper,
                parameters={"type": "object", "properties": {}, "required": []},
            )

            async def _list_delivery_channels_wrapper():
                try:
                    from app.events.channels import list_available_channels
                    from app.db import get_db as _gd
                    chans = await list_available_channels(
                        _gd(), user_id=user_id, agent_id=agent_id or "",
                        current_session_id=session_id or None,
                    )
                    return json.dumps({
                        "status": "ok",
                        "channels": chans,
                        "note": (
                            "ONLY suggest channels in this list. Channels NOT listed (Telegram, "
                            "SMS, Slack, Discord, email-out, etc. when absent) require admin "
                            "comms plugin enablement + agent connection setup + per-user recipient "
                            "config — none of which exist yet for those channels. Do not invent "
                            "channels or claim a channel is 'available' that isn't here."
                        ),
                    })
                except Exception as e:
                    return json.dumps({"status": "error", "message": str(e)})

            tools["list_delivery_channels"] = ToolInfo(
                name="list_delivery_channels",
                handler=_list_delivery_channels_wrapper,
                parameters={"type": "object", "properties": {}, "required": []},
            )

            async def _event_subscribe_wrapper(
                source: str,
                event_type: str,
                prompt: str,
                filter: Optional[Dict[str, Any]] = None,
                task_label: str = "",
                trigger_natural: str = "",
                channel: Optional[str] = None,
                channel_recipient: Optional[str] = None,
                silent: bool = False,
            ):
                if not agent_id:
                    return json.dumps({
                        "status": "error",
                        "message": "event_subscribe requires an agent context (no agent_id on this session)",
                    })
                from app.db import get_db
                from app.events import get_manager
                from app.automation.parser import ParsedEventSubscription
                from app.automation.sync import _event_sub_hash, _register_event_sub

                # Pre-flight: refuse to insert a row that the provider can't honor.
                # Avoids stale error rows and gives the agent a clear, actionable hint.
                mgr = get_manager()
                src_obj = mgr.get(source)
                if src_obj is None:
                    return json.dumps({
                        "status": "error",
                        "code": "unknown_source",
                        "message": (
                            f"Event source '{source}' is not registered. "
                            "Call list_event_sources to see what's available."
                        ),
                    })
                if not src_obj.enabled:
                    return json.dumps({
                        "status": "error",
                        "code": "source_not_enabled",
                        "source": source,
                        "message": (
                            f"Event source '{source}' is discovered but not enabled on this server "
                            "(provider-side config missing — e.g. for Gmail, EVENTS_GMAIL_PUBSUB_TOPIC "
                            "is not set, so push notifications can't be wired up). "
                            "Tell the user real-time push isn't available for this source; do not "
                            "create a subscription. Other options: poll-style sources (see "
                            "list_event_sources for which sources have supports_poll=true), or the "
                            "user can ask the admin to configure the provider env vars."
                        ),
                    })
                if event_type not in (src_obj.event_types or []):
                    return json.dumps({
                        "status": "error",
                        "code": "unknown_event_type",
                        "source": source,
                        "supported_event_types": list(src_obj.event_types or []),
                        "message": (
                            f"Event type '{event_type}' is not supported by source '{source}'. "
                            f"Supported: {', '.join(src_obj.event_types or []) or '(none)'}."
                        ),
                    })

                # ── Scope pre-flight ─────────────────────────────────────
                # Avoid inserting a doomed subscription when the user's OAuth
                # token lacks the scope the provider requires (most common
                # cause: user unticked a sensitive-scope box on consent).
                required_provider = getattr(src_obj, "required_provider", None)
                required_groups = src_obj.required_scopes(event_type, filter or {}) if hasattr(src_obj, "required_scopes") else []
                if required_provider and required_groups:
                    from app.integrations.oauth_helper import oauth_label as _oauth_label
                    from app.db import get_db as _get_db
                    _db = _get_db()
                    elem = await _db.auth_element_get(user_id, required_provider, _oauth_label(agent_id))
                    if not elem:
                        return json.dumps({
                            "status": "error",
                            "code": "provider_not_connected",
                            "provider": required_provider,
                            "message": (
                                f"Source '{source}' needs an active '{required_provider}' OAuth "
                                "connection on this agent. Ask the user to connect via the chat's "
                                "OAuth link before subscribing."
                            ),
                        })
                    try:
                        cfg = json.loads(elem.get("config") or "{}") if isinstance(elem.get("config"), str) else (elem.get("config") or {})
                    except Exception:
                        cfg = {}
                    granted = set(cfg.get("scopes") or [])
                    missing = [g for g in required_groups if not any(s in granted for s in g)]
                    if missing:
                        return json.dumps({
                            "status": "error",
                            "code": "scope_missing",
                            "provider": required_provider,
                            "source": source,
                            "mode": "push" if src_obj.supports_push else "poll",
                            "missing_groups": missing,
                            "granted_scopes": sorted(granted),
                            "message": (
                                f"The user's '{required_provider}' token does not have the scope(s) "
                                f"required for source '{source}' "
                                f"({'push' if src_obj.supports_push else 'poll'} mode). "
                                "Tell the user to RECONNECT the integration and TICK every checkbox "
                                "for this provider on the Google/Microsoft consent screen — they "
                                "almost certainly unchecked one previously. Do not insert this "
                                "subscription. "
                                f"Need at least one scope from each group: {missing}. "
                                f"Currently granted: {sorted(granted)}."
                            ),
                        })

                # Default delivery: the chat session the user is currently in.
                # `channel="webchat"` + `channel_recipient=<session_id>` tells
                # the event executor to post the agent's reply BACK into the
                # user's current chat instead of spawning an evt-* session
                # that lives in the sidebar but never lights up the chat
                # they're sitting in. Caller can override either field to
                # subscribe in a different session, fresh evt-* session, or
                # a non-webchat channel.
                effective_channel = channel
                effective_recipient = channel_recipient
                if effective_channel is None:
                    effective_channel = "webchat"
                if effective_channel == "webchat" and not effective_recipient and session_id:
                    effective_recipient = session_id

                parsed = ParsedEventSubscription(
                    task_label=task_label or f"{source}/{event_type}",
                    prompt=prompt,
                    source=source,
                    event_type=event_type,
                    filter_dict=filter or {},
                    trigger_natural=trigger_natural,
                    channel=effective_channel,
                    channel_recipient=effective_recipient,
                    silent=silent,
                )
                db = get_db()
                try:
                    row = await db.upsert_event_subscription(
                        agent_id=agent_id,
                        owner_user_id=user_id,
                        source_hash=_event_sub_hash(parsed),
                        source=parsed.source,
                        event_type=parsed.event_type,
                        filter_dict=parsed.filter_dict,
                        task_label=parsed.task_label,
                        prompt=parsed.prompt,
                        trigger_natural=parsed.trigger_natural,
                        channel=parsed.channel,
                        channel_recipient=parsed.channel_recipient,
                        silent=parsed.silent,
                        enabled=True,
                    )
                    await _register_event_sub(db, row, parsed)
                    row_after = (await db.list_event_subscriptions(
                        agent_id=agent_id, owner_user_id=user_id,
                    ))
                    fresh = next((r for r in row_after if r.get("id") == row.get("id")), row)
                    # Notify the user's open tabs so the Automation tab re-fetches
                    # without a manual refresh.
                    try:
                        from app.api.chat import _emit_to_user_listeners
                        await _emit_to_user_listeners(user_id, {
                            "type": "automation_updated",
                            "agent_id": agent_id,
                            "action": "subscribed",
                            "kind": "event_subscription",
                            "subscription_id": fresh.get("id"),
                        })
                    except Exception:
                        pass
                    return json.dumps({
                        "status": "ok",
                        "subscription": fresh,
                        "message": (
                            f"Subscribed: when {source}/{event_type} fires, run agent."
                            + (f" Provider-side: {fresh.get('last_status') or 'pending'}." if fresh.get('last_status') else "")
                            + (f" Error: {fresh.get('last_error')}" if fresh.get('last_error') else "")
                        ),
                    })
                except Exception as e:
                    logger.exception("event_subscribe failed")
                    return json.dumps({"status": "error", "message": str(e)})

            tools["event_subscribe"] = ToolInfo(
                name="event_subscribe",
                handler=_event_subscribe_wrapper,
                parameters={
                    "type": "object",
                    "properties": {
                        "source": {
                            "type": "string",
                            "description": "Event source name. Call list_event_sources to see what's enabled (e.g. 'gmail', 'slack', 'outlook_mail', 'google_drive', 'telegram').",
                        },
                        "event_type": {
                            "type": "string",
                            "description": "Event type on that source (e.g. 'message_received', 'file_added', 'mention'). See list_event_sources.",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "Instructions the agent will run when the event fires. Reference the event payload naturally — e.g. 'Summarize the new email and send it to me on Telegram.'",
                        },
                        "filter": {
                            "type": "object",
                            "description": "Source-specific filter. For Gmail: {'query': 'from:airlines.com'}. For Drive: {'parent_id': '<folder-id>'}. Empty = match all.",
                            "default": {},
                        },
                        "task_label": {
                            "type": "string",
                            "description": "Short human label for this trigger (shown in the Automation tab). Optional.",
                            "default": "",
                        },
                        "trigger_natural": {
                            "type": "string",
                            "description": "The original English the user wrote (for the Automation tab to display). Optional.",
                            "default": "",
                        },
                        "channel": {
                            "type": "string",
                            "description": (
                                "Where to deliver when the event fires. **DEFAULT: omit this argument** — "
                                "the tool will set channel='webchat' targeted at the user's current "
                                "session, so the agent's reply lands directly in the chat they're "
                                "sitting in. Only pass a non-webchat value (telegram, slack, sms, "
                                "discord, etc.) if you have FIRST called `list_delivery_channels` "
                                "AND that exact name appears in the returned `channels` array. "
                                "Never invent or assume a channel is available."
                            ),
                        },
                        "channel_recipient": {
                            "type": "string",
                            "description": (
                                "Channel-specific recipient. For webchat (default), this is the "
                                "session_id where the event reply should land — the tool auto-fills "
                                "it from the current session, so usually omit. For other channels, "
                                "this is the chat_id / phone / address."
                            ),
                        },
                        "silent": {
                            "type": "boolean",
                            "description": "If true, the agent processes the event without notifying the user.",
                            "default": False,
                        },
                    },
                    "required": ["source", "event_type", "prompt"],
                },
            )

            async def _list_event_subscriptions_wrapper():
                from app.db import get_db
                db = get_db()
                rows = await db.list_event_subscriptions(
                    agent_id=agent_id or None,
                    owner_user_id=user_id,
                )
                return json.dumps({"status": "ok", "subscriptions": rows, "count": len(rows)})

            tools["list_event_subscriptions"] = ToolInfo(
                name="list_event_subscriptions",
                handler=_list_event_subscriptions_wrapper,
                parameters={"type": "object", "properties": {}, "required": []},
            )

            async def _event_unsubscribe_wrapper(subscription_id: str):
                from app.db import get_db
                from app.automation.sync import _unregister_event_sub
                db = get_db()
                rows = await db.list_event_subscriptions(
                    agent_id=agent_id or None,
                    owner_user_id=user_id,
                )
                target = next((r for r in rows if r.get("id") == subscription_id), None)
                if not target:
                    return json.dumps({"status": "error", "message": f"Subscription {subscription_id} not found or not owned by user"})
                await _unregister_event_sub(target)
                ok = await db.delete_event_subscription(subscription_id)
                if ok:
                    try:
                        from app.api.chat import _emit_to_user_listeners
                        await _emit_to_user_listeners(user_id, {
                            "type": "automation_updated",
                            "agent_id": agent_id,
                            "action": "unsubscribed",
                            "kind": "event_subscription",
                            "subscription_id": subscription_id,
                        })
                    except Exception:
                        pass
                return json.dumps({
                    "status": "ok" if ok else "error",
                    "message": f"Subscription {subscription_id} {'removed' if ok else 'delete failed'}",
                })

            tools["event_unsubscribe"] = ToolInfo(
                name="event_unsubscribe",
                handler=_event_unsubscribe_wrapper,
                parameters={
                    "type": "object",
                    "properties": {
                        "subscription_id": {"type": "string", "description": "id of the row to remove (from list_event_subscriptions)"},
                    },
                    "required": ["subscription_id"],
                },
            )

        # ── Optimizer tools (Planner / Closer subagents) ──
        from app.tools.optimizer_tools import run_worker_trials, handoff_to_closer, deploy_optimization

        async def _run_worker_trials_wrapper(changes_json: str = ""):
            import logging as _log
            import sqlite3, uuid as _uid, traceback as _tb
            _log.warning(f"_WRAPPER CALLED: user_id={user_id}")
            try:
                # Find latest optimizer session
                db = sqlite3.connect("app/db/local.db")
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
            db = sqlite3.connect("app/db/local.db")
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
            db = sqlite3.connect("app/db/local.db")
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

    # Propagate requires_confirmation from BUILTIN_TOOL_METADATA to built-in ToolInfo entries.
    # DB tools already have this set from their row; built-ins need it applied from metadata.
    for name, info in tools.items():
        if not info.requires_confirmation and name in BUILTIN_TOOL_METADATA:
            meta = BUILTIN_TOOL_METADATA[name]
            if meta.get("requires_confirmation", False):
                info.requires_confirmation = True

    # Phase 5: enforce allowed_tools filter.
    # Tier-1 tools are always-on and must never be filtered.
    TIER_1_ALWAYS_ON = {
        "list_tools", "search_tools", "get_tool_definition",
        "get_time", "get_date", "calculate", "read_attachment",
        "delegate_to_agent", "list_delegatable_agents", "register_user",
    }
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

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
    # ── DB & context ──
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

    async def load_tools(self, user_id: str, agent_id: str = "", agent_template_id: Optional[str] = None, is_admin_agent: bool = False) -> Dict[str, 'ToolInfo']:
        """
        Load all active tools for a user from the tools table.
        Each tool's `code` field contains the full async function to execute.

        Args:
            user_id: The user ID to load tools for
            agent_template_id: Active agent template id — gates admin-only tools.

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

        # ── Inject built-in tools (override any DB versions) ──
        self._inject_builtin_tools(tools, user_id, agent_id=agent_id, agent_template_id=agent_template_id, is_admin_agent=is_admin_agent)

        return tools

    def _inject_builtin_tools(self, tools: Dict[str, ToolInfo], user_id: str, agent_id: str = "", agent_template_id: Optional[str] = None, is_admin_agent: bool = False) -> None:
        """Inject built-in tools that are always available regardless of DB state."""

        # ── create_tool (always available) ──
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

        # ── Source management tools — only injected for admin-agent sessions ──
        # These are privileged tools (read/write/edit/delete files, run commands).
        # They are scoped exclusively to sessions running the 'admin-agent' template.
        if is_admin_agent or agent_template_id == "admin-agent":  # is_admin_agent is preferred; string fallback for legacy
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

        # ── Web search ──
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

        # ── Browser action (persistent Chromium) ──
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

        # ── DB query (context documents) ──
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

        # ── Weather ──
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

        # ── HTTP Request tool (outbound GET/POST/PUT/DELETE/PATCH) ──
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

        # ── Optimizer tools (Planner / Finalizer subagents) ──
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
                    "summary": {"type": "string", "description": "Summary of what was discussed and decided to pass to the Finalizer"},
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

            # Check if user already has a connected token
            try:
                elem = await _db.auth_element_get(user_id, provider, "oauth")
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
                        "description": (
                            "The OAuth provider to check. Call this whenever the user wants to do something "
                            "that requires a connected account and you are not sure if they have connected it. "
                            "Examples: 'check my email' or 'read my Gmail' → google; 'post to Twitter' → twitter; "
                            "'access my Drive' or 'check my calendar' → google; 'read Outlook' → microsoft; "
                            "'upload to Dropbox' → dropbox; 'post to LinkedIn' → linkedin; "
                            "'post to Facebook/Instagram' → meta. "
                            "Supported values: google, microsoft, yahoo, dropbox, meta, twitter, linkedin, "
                            "tiktok, pinterest, reddit, snapchat, twitch."
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
    is_admin_agent: bool = False,
    allowed_tools: Optional[List[str]] = None,
    custom_tool_ids: Optional[List[str]] = None,
) -> Dict[str, ToolInfo]:
    """
    Load all active tools for a user.

    Args:
        user_id: The user ID to load tools for.
        agent_template_id: Active agent template id - gates admin-only and
            delegation tools; pipeline agents skip delegation tools.
        allowed_tools: List of Tier-2 tool names that are DISABLED for this
            agent. Empty list means all Tier-2 tools are enabled.
            Tier-0 (admin) and Tier-1 (always-on) tools are never filtered.
        custom_tool_ids: Reserved - DB tool IDs opted in (not yet enforced).

    Returns:
        Dictionary mapping tool names to ToolInfo objects.
    """
    tools = await _tool_loader.load_tools(user_id, agent_id=agent_id, agent_template_id=agent_template_id, is_admin_agent=is_admin_agent)

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

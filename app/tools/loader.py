"""
Tool loader for dynamic tool loading from database.
"""
import json
import logging
from dataclasses import dataclass
from typing import Dict, Any, Callable, List, Optional
from app.db import get_db

logger = logging.getLogger(__name__)


@dataclass
class ToolInfo:
    """Enriched tool descriptor returned by load_tools()."""
    name: str
    handler: Callable
    parameters: dict


class ToolLoader:
    """Load tools dynamically from the database and compile them into Python functions."""

    def __init__(self):
        self._client = get_db().get_raw_client()

    async def load_tools(self, user_id: str) -> Dict[str, 'ToolInfo']:
        """
        Load all active tools for a user from the tools table.
        Each tool's `code` field contains the full async function to execute.

        Args:
            user_id: The user ID to load tools for

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
            tools[name] = ToolInfo(name=name, handler=handler, parameters=params)
            logger.debug(f"Loaded tool {name} for user {user_id}")

        # ── Inject built-in tools (override any DB versions) ──
        self._inject_builtin_tools(tools, user_id)

        return tools

    def _inject_builtin_tools(self, tools: Dict[str, ToolInfo], user_id: str) -> None:
        """Inject built-in tools that are always available regardless of DB state."""

        # ── create_tool (always available) ──
        from app.tools.registry import create_tool as _builtin_create_tool

        async def _create_tool_wrapper(name, description, parameters, code):
            return await _builtin_create_tool(
                name=name,
                description=description,
                parameters=parameters,
                code=code,
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
                },
                "required": ["name", "description", "parameters", "code"],
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

        # ── read_attachment (always available) ──
        from app.tools.read_attachment import read_attachment as _builtin_read_attachment, TOOL_DEFINITION as _ATTACH_TOOL_DEF
        tools["read_attachment"] = ToolInfo(
            name="read_attachment",
            handler=_builtin_read_attachment,
            parameters=_ATTACH_TOOL_DEF["parameters"],
        )

        # ── Agent context documents (body text read/write; scoped to this user's assigned agent) ──
        async def _list_agent_context_documents(context_types: Optional[List[str]] = None):
            """List persisted context documents for the user's assigned agent."""
            db = get_db()
            agent = await db.get_agent_for_user(user_id)
            if not agent:
                return json.dumps({"status": "error", "message": "No agent assigned for this user."})
            filt = context_types if context_types else None
            docs = await db.fetch_context_documents(agent["id"], filt)
            return json.dumps({"status": "ok", "count": len(docs), "documents": docs})

        tools["list_agent_context_documents"] = ToolInfo(
            name="list_agent_context_documents",
            handler=_list_agent_context_documents,
            parameters={
                "type": "object",
                "properties": {
                    "context_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional filter by context_type (e.g. agent, skills, tools). Omit to return all.",
                    },
                },
                "required": [],
            },
        )

        async def _get_agent_context_document(context_id: str):
            """Load one context document by id if owned by this user's agent."""
            db = get_db()
            agent = await db.get_agent_for_user(user_id)
            if not agent:
                return json.dumps({"status": "error", "message": "No agent assigned for this user."})
            doc = await db.get_context_document(agent["id"], context_id)
            if not doc:
                return json.dumps({"status": "error", "message": "Document not found or not accessible to this agent."})
            return json.dumps({"status": "ok", "document": doc})

        tools["get_agent_context_document"] = ToolInfo(
            name="get_agent_context_document",
            handler=_get_agent_context_document,
            parameters={
                "type": "object",
                "properties": {
                    "context_id": {"type": "string", "description": "Document id from list_agent_context_documents."},
                },
                "required": ["context_id"],
            },
        )

        async def _update_agent_context_document(context_id: str, content: str):
            """Replace the body (content) of a context document owned by this user's agent."""
            db = get_db()
            agent = await db.get_agent_for_user(user_id)
            if not agent:
                return json.dumps({"status": "error", "message": "No agent assigned for this user."})
            try:
                await db.update_context_document_content(agent["id"], context_id, content)
            except PermissionError as e:
                return json.dumps({"status": "error", "message": str(e)})
            return json.dumps({"status": "ok", "context_id": context_id})

        tools["update_agent_context_document"] = ToolInfo(
            name="update_agent_context_document",
            handler=_update_agent_context_document,
            parameters={
                "type": "object",
                "properties": {
                    "context_id": {"type": "string"},
                    "content": {"type": "string", "description": "Full replacement text for the document body."},
                },
                "required": ["context_id", "content"],
            },
        )

        async def _insert_agent_context_document(
            context_type: str, title: str, content: str, tags: Optional[List[str]] = None,
        ):
            """Insert a new context document row for this user's agent."""
            db = get_db()
            agent = await db.get_agent_for_user(user_id)
            if not agent:
                return json.dumps({"status": "error", "message": "No agent assigned for this user."})
            try:
                doc_id = await db.insert_document(
                    agent["id"], context_type, title, content, tags=tags,
                )
            except PermissionError as e:
                return json.dumps({"status": "error", "message": str(e)})
            return json.dumps({"status": "ok", "id": doc_id})

        tools["insert_agent_context_document"] = ToolInfo(
            name="insert_agent_context_document",
            handler=_insert_agent_context_document,
            parameters={
                "type": "object",
                "properties": {
                    "context_type": {"type": "string", "description": "Section type e.g. skills, tools, memory."},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags."},
                },
                "required": ["context_type", "title", "content"],
            },
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
            from app.communications.auth import get_identity, upgrade_to_verified, ChannelIdentity

            # The user_id passed to load_tools tells us who the agent is talking about.
            # But registration happens via the channel, so we need to find the identity.
            # The user_id format is "channel:external_id"
            if ":" in user_id:
                chan, ext_id = user_id.split(":", 1)
            else:
                return json.dumps({"error": "cannot determine channel identity"})

            identity = await get_identity(chan, ext_id)
            if identity is None:
                return json.dumps({"error": "identity not found"})

            if identity.user_tier == "full":
                return json.dumps({"status": "ok", "message": "Already fully registered."})

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

        # ── Source management tools (in admin/ — delete to lock down) ──
        try:
            from app.admin.source_tools import inject_source_tools
            inject_source_tools(tools, user_id)
        except ImportError:
            pass  # admin/source_tools.py not available — source editing disabled

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
        )

        # ── Tool discovery ──
        async def _list_tools_wrapper():
            return await _core_list_tools(user_id=user_id)

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
            return await _core_search_tools(query=query, user_id=user_id)

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


async def load_tools(user_id: str) -> Dict[str, ToolInfo]:
    """
    Load all active tools for a user.

    Args:
        user_id: The user ID to load tools for

    Returns:
        Dictionary mapping tool names to ToolInfo objects
    """
    return await _tool_loader.load_tools(user_id)

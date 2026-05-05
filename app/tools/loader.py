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
    rating: Optional[dict] = None  # skill performance rating
    skill_id: Optional[str] = None  # links to skills table


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

        # ── Annotate tools with skill ratings ──
        await self._annotate_ratings(tools, user_id)

        return tools

    async def _annotate_ratings(self, tools: Dict[str, ToolInfo], user_id: str) -> None:
        """Annotate each tool with its skill rating from execution history."""
        db = get_db()
        for name, info in tools.items():
            try:
                skill_id = await db.skill_get_id_by_name(user_id, name)
                if skill_id:
                    rating = await db.skill_get_rating(skill_id, user_id)
                    info.rating = rating
                    info.skill_id = skill_id
            except Exception:
                pass

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

        Args:
            code_string: Full Python function code
            tool_name: Name of the tool for error reporting

        Returns:
            Compiled Python function object
        """
        try:
            compiled = compile(code_string, f"<tool:{tool_name}>", "exec")
            namespace = {}
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

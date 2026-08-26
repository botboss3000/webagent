"""Tool resolution and execution glue for the MCP bridge.

resolve_tools_for_engine() loads every tool the app's registry resolves for this
agent — the exact same set a native agent gets — and returns {name: (handler, ToolInfo)}.
execute_mcp_tool() runs a single call through the handler and returns an MCP
content block — with the same ToolExecutionContext the native loop wraps.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple

from app.tools.loader import load_tools
from app.tools.execution_context import ToolExecutionContext, tool_execution_scope

logger = logging.getLogger(__name__)

# Re-export for server.py
ToolPer = Tuple[Callable, Dict[str, Any]]  # (handler, ToolInfo-as-dict)
ToolMap = Dict[str, ToolPer]


async def resolve_tools_for_engine(
    user_id: str,
    agent_id: str,
    session_id: str,
    execution_mode: str,
    allowed_tools: Optional[list] = None,
    agent_template_id: Optional[str] = None,
) -> ToolMap:
    """Load every tool available to this agent, returning name → (handler, info).

    Calls the same ``load_tools()`` the native agent loop uses, so ability-
    gated tools, integration tools, ability tools, and custom DB tools
    are all included — with no per-engine wiring.

    Returns an empty dict when loading fails (the MCP server will report zero
    tools rather than crashing).
    """
    try:
        raw = await load_tools(
            user_id=user_id,
            agent_id=agent_id,
            agent_template_id=agent_template_id,
            allowed_tools=allowed_tools,
            session_id=session_id,
            gate_caller_access=True,
        )
    except Exception as exc:
        logger.warning("MCP tool load failed for agent %s: %s", agent_id, exc)
        return {}

    out: ToolMap = {}
    for name, ti in raw.items():
        out[name] = (
            ti.handler,
            {
                "name": name,
                "parameters": ti.parameters,
                "destructive": bool(ti.destructive),
                "requires_confirmation": bool(ti.requires_confirmation),
                "tool_id": ti.tool_id,
            },
        )
    return out


async def execute_mcp_tool(
    name: str,
    arguments: dict,
    tools: ToolMap,
    user_id: str,
    session_id: str,
    execution_mode: str = "auto",
) -> dict:
    """Execute a single tool call and return an MCP content block.

    Returns:
        ``{"content": [{"type": "text", "text": "..."}], "isError": false}``
    """
    entry = tools.get(name)
    if entry is None:
        return _error(f"Tool '{name}' is not available to this agent.")

    handler, info = entry

    # ── Permission gate (mirrors the native loop's guardrail) ──
    if execution_mode == "plan" and info.get("destructive"):
        return _error(
            f"Tool '{name}' is blocked in Plan mode (it writes or changes state). "
            "Switch to a write-capable mode such as Auto to use it."
        )
    if execution_mode == "ask" and info.get("destructive"):
        return _error(
            f"Tool '{name}' is blocked in Ask mode (it writes or changes state). "
            "Ask mode ends with a proposal; switch to a write-capable mode such as Auto to execute it."
        )

    start = time.time()
    try:
        ctx = ToolExecutionContext(
            user_id=user_id,
            session_id=session_id,
            turn_key="",            # MCP calls are outside the native turn loop
            tool_name=name,
            tool_call_id="mcp:" + name,
            authority_mode="server",
            idempotency_key=None,
            side_effecting=bool(info.get("destructive")),
        )
        with tool_execution_scope(ctx):
            result = await handler(**arguments)
        duration_ms = int((time.time() - start) * 1000)
        text = _serialise(result)
        return {
            "content": [{"type": "text", "text": text}],
            "isError": False,
            "duration_ms": duration_ms,
        }
    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        logger.warning("MCP tool %s failed: %s", name, exc)
        return _error(f"Tool '{name}' failed: {exc}", duration_ms=duration_ms)


# ── helpers ────────────────────────────────────────────────────────────────────

def _serialise(value: Any) -> str:
    """Return a string the CLI's model can read — JSON for dicts/lists, str()
    for primitives."""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _error(message: str, duration_ms: int = 0) -> dict:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
        "duration_ms": duration_ms,
    }

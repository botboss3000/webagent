"""
Streaming agent loop — yields events instead of returning a single string.

Events:
  {"type": "stream", "content": "..."}       — token-by-token LLM output
  {"type": "tool_call", "tool": "...", "args": {...}}     — agent invoked a tool
  {"type": "tool_result", "tool": "...", "result": "...", "duration_ms": N}  — tool returned
  {"type": "response", "content": "..."}     — final answer (no more tool calls)
  {"type": "error", "message": "..."}        — something went wrong
"""

import asyncio
import difflib
import json
import logging
import os
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

from app.agent.error_classifier import classify_tool_error, ToolError
from app.tools.registry import get_tool_rating
from app.db import get_db

logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        try:
            from openai import AsyncOpenAI
        except ImportError:
            from app.openai_compat import AsyncOpenAI

        _client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ.get("OPENROUTER_API_KEY"),
            timeout=60.0,
        )
    return _client


async def _check_interrupt(interrupt_event: Optional[asyncio.Event]):
    """Checks the interrupt_event and raises CancelledError if it's set."""
    if interrupt_event and interrupt_event.is_set():
        raise asyncio.CancelledError("Agent interrupted by new user message.")


async def validate_tool_call(name: str, args: dict, tools: Dict[str, Any]) -> Optional[dict]:
    """
    Validate a tool call before execution.
    Returns a structured error dict if invalid, or None if valid.
    """
    from app.tools.loader import ToolInfo

    if name not in tools:
        closest = difflib.get_close_matches(name, tools.keys(), n=1, cutoff=0.8)
        hint = f"Did you mean '{closest[0]}'?" if closest else "Check the tool name and retry."
        return {
            "status": "validation_error",
            "tool": name,
            "message": f"Tool '{name}' not found. {hint}",
            "recoverable": True,
            "hint": hint,
        }

    tool_info: ToolInfo = tools[name]
    schema = tool_info.parameters if hasattr(tool_info, 'parameters') else {}

    if not isinstance(args, dict):
        return {"status": "validation_error", "tool": name, "message": "Arguments must be a JSON object", "recoverable": True, "hint": "Send arguments as a JSON object."}

    for param in schema.get("required", []):
        if param not in args:
            return {"status": "validation_error", "tool": name, "message": f"Missing required parameter '{param}'", "recoverable": True, "hint": f"Provide the '{param}' parameter."}

    return None


async def stream_agent_events(
    user_id: str,
    session_id: str,
    user_message: str,
    system_prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
    parent_interaction_id: str | None = None,
    interrupt_event: Optional[asyncio.Event] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Run the agent loop and yield structured events.
    The caller receives events and can route them as needed
    (chat responses → side panel, tool calls → terminal).
    This function is interruptible via interrupt_event.
    """
    from app.tools.loader import load_tools

    tools = await load_tools(user_id)

    # Build message list
    messages: List[Dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    turn_count = 0
    max_turns = 10

    try:
        while turn_count < max_turns:
            await _check_interrupt(interrupt_event)
            turn_count += 1

            # Build tool definitions from loaded tools
            tool_definitions = []
            for name, info in tools.items():
                description = info.handler.__doc__ if hasattr(info, 'handler') and info.handler.__doc__ else f"Execute {name}"
                description = description.split("\n")[0]
                tool_definitions.append({
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": description,
                        "parameters": info.parameters if hasattr(info, 'parameters') else {"type": "object", "properties": {}, "required": []},
                    },
                })

            # Helper to build metadata (lightweight)
            llm_start_time = time.time()

            def _build_meta(role: str) -> str:
                return json.dumps({
                    "model": os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v3.2"),
                    "turn": turn_count,
                    "duration_ms": int((time.time() - llm_start_time) * 1000),
                    "role": role,
                    "streaming": True,
                })

            def _build_input() -> str:
                return json.dumps(messages)

            await _check_interrupt(interrupt_event)

            # ── Stream the LLM response ──
            model = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v3.2")
            try:
                stream = await _get_client().chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tool_definitions if tool_definitions else None,
                    tool_choice="auto" if tool_definitions else None,
                    temperature=0.0,
                    max_tokens=4096,
                    stream=True,
                )
            except Exception as e:
                yield {"type": "error", "message": f"LLM call failed: {e}"}
                return

            collected_content = ""
            collected_tool_calls: Dict[int, Any] = {}

            async for chunk in stream:
                await _check_interrupt(interrupt_event)
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if not delta:
                    continue

                if delta.content:
                    collected_content += delta.content
                    yield {"type": "stream", "content": delta.content}

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in collected_tool_calls:
                            collected_tool_calls[idx] = tc
                        else:
                            existing = collected_tool_calls[idx]
                            if tc.function:
                                if tc.function.name:
                                    existing.function.name = tc.function.name
                                if tc.function.arguments:
                                    existing.function.arguments = (existing.function.arguments or "") + tc.function.arguments

            # ── Handle tool calls ──
            if collected_tool_calls:
                # Build the assistant message for the message list
                full_tool_calls = []
                for tc in collected_tool_calls.values():
                    full_tool_calls.append({
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
                    })

                messages.append({
                    "role": "assistant",
                    "content": collected_content or None,
                    "tool_calls": full_tool_calls,
                })

                # Persist: save the intermediate assistant message (with tool calls) to DB
                assistant_content = collected_content or ""
                if full_tool_calls:
                    tool_calls_summary = json.dumps([
                        {"name": tc["function"]["name"], "args": tc["function"]["arguments"]}
                        for tc in full_tool_calls
                    ])
                    assistant_content += f"\n\n[Tool calls: {tool_calls_summary}]"
                meta_asst = _build_meta("assistant")
                inp = _build_input()
                asst_id = await get_db().insert_interaction(
                    user_id, session_id, role="assistant", content=assistant_content,
                    parent_id=parent_interaction_id,
                    metadata=meta_asst,
                    input_data=inp,
                )

                # 1. Validate ALL tool calls before any execution
                valid_calls: List[Any] = []
                for idx, tc in sorted(collected_tool_calls.items()):
                    await _check_interrupt(interrupt_event)
                    tool_name = tc.function.name
                    try:
                        tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except json.JSONDecodeError:
                        tool_args = {}

                    validation_error = await validate_tool_call(tool_name, tool_args, tools)
                    if validation_error:
                        yield {"type": "tool_call", "tool": tool_name, "args": tool_args}
                        error_json = json.dumps(validation_error)
                        yield {
                            "type": "tool_result",
                            "tool": tool_name,
                            "result": error_json[:2000],
                            "error": True,
                            "error_type": "validation_error",
                            "recoverable": True,
                        }
                        tool_msg = {"role": "tool", "content": error_json[:10000], "tool_call_id": tc.id}
                        messages.append(tool_msg)
                        # Persist: save validation error to DB
                        inp = _build_input()
                        await get_db().insert_interaction(
                            user_id, session_id, role="tool", content=tool_msg["content"],
                            parent_id=asst_id,
                            tool_call_id=tc.id,
                            metadata=json.dumps({"success": False, "duration_ms": 0, "input_params": tool_args, "error_message": "Validation failed"}),
                            input_data=inp,
                        )
                    else:
                        valid_calls.append((idx, tc, tool_name, tool_args))

                await _check_interrupt(interrupt_event)

                # 2. Execute valid tools concurrently
                if valid_calls:
                    async def execute_one(name: str, args: dict, tc_id: str) -> dict:
                        start = time.time()
                        try:
                            handler = tools[name].handler if hasattr(tools[name], 'handler') else tools[name]
                            result = await handler(**args)
                            result_str = str(result)
                            duration_ms = int((time.time() - start) * 1000)
                            return {"tool_call_id": tc_id, "tool": name, "content": result_str, "duration_ms": duration_ms, "success": True, "error": None, "input_params": args}
                        except Exception as e:
                            duration_ms = int((time.time() - start) * 1000)
                            te: ToolError = classify_tool_error(e, name, args)
                            result_str = json.dumps({
                                "status": "error", "error_type": te.error_type, "tool": te.tool_name,
                                "message": te.message, "recoverable": te.recoverable, "hint": te.retry_hint,
                            })
                            return {"tool_call_id": tc_id, "tool": name, "content": result_str, "duration_ms": duration_ms, "success": False, "error": te, "input_params": args}

                    tasks = [execute_one(name, args, tc.id) for _, tc, name, args in valid_calls]
                    results = await asyncio.gather(*tasks)

                    # 3. Emit results in original order
                    for (idx, tc, tool_name, tool_args), result in zip(valid_calls, results):
                        await _check_interrupt(interrupt_event)
                        success = result["success"]
                        te = result.get("error")

                        yield {
                            "type": "tool_result",
                            "tool": tool_name,
                            "result": result["content"][:2000],
                            "duration_ms": result["duration_ms"],
                            "error": not success,
                            "error_type": te.error_type if te else None,
                            "recoverable": te.recoverable if te else None,
                        }

                        tool_exec_meta = json.dumps({
                            "success": success,
                            "duration_ms": result["duration_ms"],
                            "input_params": tool_args,
                            "error_message": None if success else result["content"][:500],
                        })

                        tool_msg = {"role": "tool", "content": result["content"][:10000], "tool_call_id": tc.id}
                        messages.append(tool_msg)
                        # Persist: save tool result to DB
                        inp = _build_input()
                        inter_id = await get_db().insert_interaction(
                            user_id, session_id, role="tool", content=tool_msg["content"],
                            parent_id=asst_id,
                            tool_call_id=tc.id,
                            tool_name=tool_name,
                            metadata=tool_exec_meta,
                            input_data=inp,
                        )

                        # Track skill execution
                        try:
                            db = get_db()
                            skill_id = await db.skill_get_id_by_name(user_id, tool_name)
                            if skill_id:
                                await db.skill_track_execution(
                                    skill_id=skill_id,
                                    user_id=user_id,
                                    session_id=session_id,
                                    success=success,
                                    duration_ms=result["duration_ms"],
                                    interaction_id=inter_id,
                                    error_message=None if success else result["content"][:500],
                                    input_params=tool_args,
                                    output_summary=result["content"][:200],
                                )
                            else:
                                # Skill not registered yet — skip silently
                                pass
                        except Exception as track_err:
                            logger.debug(f"Skill tracking skipped for {tool_name}: {track_err}")

                continue

            # ── No tool calls → final response ──
            messages.append({
                "role": "assistant",
                "content": collected_content,
            })

            # Save to database
            meta_final = _build_meta("assistant")
            inp = _build_input()
            await get_db().insert_interaction(
                user_id, session_id, role="assistant", content=collected_content,
                parent_id=parent_interaction_id,
                metadata=meta_final,
                input_data=inp,
            )

            yield {"type": "response", "content": collected_content}
            return

        yield {
            "type": "response",
            "content": "I've reached the maximum number of turns. What would you like to do next?",
        }
    except asyncio.CancelledError:
        logger.info(f"stream_agent_events for session {session_id} cancelled by interrupt.")
        return
    except Exception as e:
        logger.error(f"stream_agent_events error: {e}", exc_info=True)
        yield {"type": "error", "message": f"Unexpected error in agent loop: {e}"}
        return

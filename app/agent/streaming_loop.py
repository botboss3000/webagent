"""
Streaming agent loop — yields events instead of returning a single string.

Events:
  {"type": "stream", "level": "agent", "content": "..."}       — token-by-token LLM output
  {"type": "tool_call", "level": "agent", "tool": "...", "args": {...}}     — agent invoked a tool
  {"type": "tool_result", "level": "agent", "tool": "...", "result": "...", "duration_ms": N}  — tool returned
  {"type": "response", "level": "agent", "content": "..."}     — final answer (no more tool calls)
  {"type": "error", "level": "agent", "message": "..."}        — something went wrong
  {"type": "pipeline", "level": "pipeline", "step": "...", ...} — internal agent logic
  {"type": "db", "level": "db", "op": "...", ...}              — database operations
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

# ── Destructive tools that require confirmation ──
DESTRUCTIVE_TOOLS = {"edit_source", "write_source", "delete_source",
                     "run_command", "restart_server", "create_tool"}


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


def _check_user_confirmed(messages: List[Dict[str, Any]], tool_name: str) -> bool:
    """
    Check if the user confirmed a destructive tool call.
    Scans recent conversation for confirmation-seeking language from the model
    followed by user approval.
    """
    # Look at the last assistant message + last user message
    last_assistant_content = ""
    last_user_content = ""
    for msg in reversed(messages):
        if msg.get("role") == "user" and not last_user_content:
            last_user_content = (msg.get("content") or "").lower()
        if msg.get("role") == "assistant" and not last_assistant_content:
            last_assistant_content = (msg.get("content") or "").lower()

    # Check if the model asked for confirmation
    ask_keywords = ["would you like me to", "should i", "shall i",
                     "let me know if", "do you want me to",
                     "confirm", "approve", "ok to", "okay to",
                     "proceed", "go ahead and"]
    model_asked = any(kw in last_assistant_content for kw in ask_keywords)

    if not model_asked:
        # Model didn't explicitly ask — check if user proactively confirmed
        confirm_keywords = ["yes", "go ahead", "proceed", "approved", "ok", "okay",
                            "sure", "do it", "confirm", "go for it", "please do"]
        return any(kw in last_user_content for kw in confirm_keywords)

    # Model asked — check if user approved
    confirm_keywords = ["yes", "go ahead", "proceed", "approved", "ok", "okay",
                        "sure", "do it", "confirm", "go for it", "please do"]
    return any(kw in last_user_content for kw in confirm_keywords)


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

    model_name = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v4-flash")

    load_start = time.time()
    tools = await load_tools(user_id)
    load_duration = int((time.time() - load_start) * 1000)

    # ── Pipeline: tools loaded ──
    yield {"type": "pipeline", "level": "pipeline",
           "step": "load_tools", "count": len(tools),
           "names": list(tools.keys()),
           "duration_ms": load_duration}

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

            # ── Pipeline: turn start ──
            yield {"type": "pipeline", "level": "pipeline",
                   "step": "turn_start", "turn": turn_count, "max_turns": max_turns}

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

            # ── Pipeline: tool definitions built ──
            yield {"type": "pipeline", "level": "pipeline",
                   "step": "tool_defs_built", "count": len(tool_definitions)}

            # Helper to build metadata (lightweight)
            llm_start_time = time.time()

            def _build_meta(role: str) -> str:
                return json.dumps({
                    "model": model_name,
                    "turn": turn_count,
                    "duration_ms": int((time.time() - llm_start_time) * 1000),
                    "role": role,
                    "streaming": True,
                })

            def _build_input() -> str:
                return json.dumps(messages)

            await _check_interrupt(interrupt_event)

            # ── Pipeline: LLM call start ──
            yield {"type": "pipeline", "level": "pipeline",
                   "step": "llm_call_start", "model": model_name,
                   "message_count": len(messages),
                   "turn": turn_count}

            # ── Stream the LLM response ──
            llm_start = time.time()
            try:
                stream = await _get_client().chat.completions.create(
                    model=model_name,
                    messages=messages,
                    tools=tool_definitions if tool_definitions else None,
                    tool_choice="auto" if tool_definitions else None,
                    temperature=0.0,
                    max_tokens=4096,
                    stream=True,
                )
            except Exception as e:
                yield {"type": "error", "level": "agent", "message": f"LLM call failed: {e}"}
                return

            collected_content = ""
            collected_tool_calls: Dict[int, Any] = {}
            input_tokens = None
            output_tokens = None

            async for chunk in stream:
                await _check_interrupt(interrupt_event)
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if not delta:
                    continue

                # Capture token usage if available
                if chunk.usage:
                    input_tokens = chunk.usage.prompt_tokens
                    output_tokens = chunk.usage.completion_tokens

                if delta.content:
                    collected_content += delta.content
                    yield {"type": "stream", "level": "agent", "content": delta.content}

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

            llm_duration = int((time.time() - llm_start) * 1000)

            # ── Pipeline: LLM call end ──
            tool_calls_data = list(collected_tool_calls.values()) if collected_tool_calls else None
            yield {"type": "pipeline", "level": "pipeline",
                   "step": "llm_call_end", "duration_ms": llm_duration,
                   "input_tokens": input_tokens, "output_tokens": output_tokens,
                   "has_tool_calls": bool(tool_calls_data)}

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

                # Emit tool_call events
                for tc in collected_tool_calls.values():
                    tool_name = tc.function.name
                    try:
                        tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except json.JSONDecodeError:
                        tool_args = {}
                    yield {"type": "tool_call", "level": "agent", "tool": tool_name, "args": tool_args}

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
                db_start = time.time()
                asst_id = await get_db().insert_interaction(
                    user_id, session_id, role="assistant", content=assistant_content,
                    parent_id=parent_interaction_id,
                    metadata=meta_asst,
                    input_data=inp,
                )
                db_dur = int((time.time() - db_start) * 1000)
                yield {"type": "db", "level": "db",
                       "op": "insert_interaction", "role": "assistant",
                       "tool_name": None, "id": asst_id, "ms": db_dur}

                # ── Pipeline: validation start ──
                yield {"type": "pipeline", "level": "pipeline",
                       "step": "validate_start", "tool_count": len(collected_tool_calls)}

                # 1. Validate ALL tool calls before any execution
                valid_calls: List[Any] = []
                blocked_calls: List[Any] = []  # guardrail-blocked tools
                for idx, tc in sorted(collected_tool_calls.items()):
                    await _check_interrupt(interrupt_event)
                    tool_name = tc.function.name
                    try:
                        tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except json.JSONDecodeError:
                        tool_args = {}

                    validation_error = await validate_tool_call(tool_name, tool_args, tools)

                    # ── Pipeline: validation result ──
                    yield {"type": "pipeline", "level": "pipeline",
                           "step": "validate_result", "tool": tool_name,
                           "passed": validation_error is None,
                           "error": str(validation_error) if validation_error else None}

                    if validation_error:
                        error_json = json.dumps(validation_error)
                        yield {
                            "type": "tool_result", "level": "agent",
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
                        db_start = time.time()
                        inter_id = await get_db().insert_interaction(
                            user_id, session_id, role="tool", content=tool_msg["content"],
                            parent_id=asst_id,
                            tool_call_id=tc.id,
                            metadata=json.dumps({"success": False, "duration_ms": 0, "input_params": tool_args, "error_message": "Validation failed"}),
                            input_data=inp,
                        )
                        db_dur = int((time.time() - db_start) * 1000)
                        yield {"type": "db", "level": "db",
                               "op": "insert_interaction", "role": "tool",
                               "tool_name": tool_name, "id": inter_id, "ms": db_dur}
                    else:
                        # ── Guardrail check for destructive tools ──
                        if tool_name in DESTRUCTIVE_TOOLS:
                            yield {"type": "pipeline", "level": "pipeline",
                                   "step": "guardrail_check", "tool": tool_name,
                                   "status": "requires_confirmation",
                                   "message": f"Tool '{tool_name}' requires user confirmation per system prompt"}

                            user_confirmed = _check_user_confirmed(messages, tool_name)

                            if not user_confirmed:
                                yield {"type": "pipeline", "level": "pipeline",
                                       "step": "guardrail_blocked", "tool": tool_name,
                                       "status": "blocked",
                                       "message": f"Tool '{tool_name}' BLOCKED: user confirmation not detected in conversation"}
                                blocked_calls.append((idx, tc, tool_name, tool_args))
                                # Emit blocked tool result
                                yield {
                                    "type": "tool_result", "level": "agent",
                                    "tool": tool_name,
                                    "result": json.dumps({"status": "blocked", "message": f"Tool '{tool_name}' requires user confirmation before execution."}),
                                    "duration_ms": 0,
                                    "error": True,
                                    "error_type": "guardrail_blocked",
                                    "recoverable": True,
                                }
                                tool_msg = {"role": "tool", "content": f"Tool '{tool_name}' was blocked because user confirmation is required for destructive operations.", "tool_call_id": tc.id}
                                messages.append(tool_msg)
                                # Persist blocked tool
                                inp = _build_input()
                                db_start = time.time()
                                inter_id = await get_db().insert_interaction(
                                    user_id, session_id, role="tool", content=tool_msg["content"],
                                    parent_id=asst_id,
                                    tool_call_id=tc.id,
                                    tool_name=tool_name,
                                    metadata=json.dumps({"success": False, "duration_ms": 0, "input_params": tool_args, "error_message": "Guardrail blocked — requires confirmation"}),
                                    input_data=inp,
                                )
                                db_dur = int((time.time() - db_start) * 1000)
                                yield {"type": "db", "level": "db",
                                       "op": "insert_interaction", "role": "tool",
                                       "tool_name": tool_name, "id": inter_id, "ms": db_dur}
                            else:
                                yield {"type": "pipeline", "level": "pipeline",
                                       "step": "guardrail_override", "tool": tool_name,
                                       "status": "confirmed", "by": "user"}
                                valid_calls.append((idx, tc, tool_name, tool_args))
                        else:
                            valid_calls.append((idx, tc, tool_name, tool_args))

                await _check_interrupt(interrupt_event)

                # 2. Execute valid tools concurrently
                if valid_calls:
                    # ── Pipeline: execute batch start ──
                    yield {"type": "pipeline", "level": "pipeline",
                           "step": "execute_batch_start", "tool_count": len(valid_calls),
                           "tools": [name for _, _, name, _ in valid_calls]}

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

                    # Emit execute_start events before execution
                    for _, _, tool_name, _ in valid_calls:
                        yield {"type": "pipeline", "level": "pipeline",
                               "step": "execute_start", "tool": tool_name}

                    tasks = [execute_one(name, args, tc.id) for _, tc, name, args in valid_calls]
                    results = await asyncio.gather(*tasks)

                    # 3. Emit results in original order
                    for (idx, tc, tool_name, tool_args), result in zip(valid_calls, results):
                        await _check_interrupt(interrupt_event)
                        success = result["success"]
                        te = result.get("error")

                        yield {
                            "type": "tool_result", "level": "agent",
                            "tool": tool_name,
                            "result": result["content"][:2000],
                            "duration_ms": result["duration_ms"],
                            "error": not success,
                            "error_type": te.error_type if te else None,
                            "recoverable": te.recoverable if te else None,
                        }

                        # ── Pipeline: execute end ──
                        yield {"type": "pipeline", "level": "pipeline",
                               "step": "execute_end", "tool": tool_name,
                               "duration_ms": result["duration_ms"],
                               "success": success}

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
                        db_start = time.time()
                        inter_id = await get_db().insert_interaction(
                            user_id, session_id, role="tool", content=tool_msg["content"],
                            parent_id=asst_id,
                            tool_call_id=tc.id,
                            tool_name=tool_name,
                            metadata=tool_exec_meta,
                            input_data=inp,
                        )
                        db_dur = int((time.time() - db_start) * 1000)
                        yield {"type": "db", "level": "db",
                               "op": "insert_interaction", "role": "tool",
                               "tool_name": tool_name, "id": inter_id, "ms": db_dur}

                        # Track skill execution
                        try:
                            db = get_db()
                            skill_id = await db.skill_get_id_by_name(user_id, tool_name)
                            if skill_id:
                                exec_id = await db.skill_track_execution(
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
                                # ── DB: skill track ──
                                rating_info = await db.skill_get_rating(skill_id)
                                new_rating = rating_info.get("score") if rating_info else None
                                yield {"type": "db", "level": "db",
                                       "op": "skill_track", "tool": tool_name,
                                       "success": success, "new_rating": new_rating}
                            else:
                                pass
                        except Exception as track_err:
                            logger.debug(f"Skill tracking skipped for {tool_name}: {track_err}")

                # ── Pipeline: check continue ──
                will_continue = turn_count < max_turns
                yield {"type": "pipeline", "level": "pipeline",
                       "step": "check_continue", "turn": turn_count,
                       "max_turns": max_turns, "will_continue": will_continue}
                continue

            # ── No tool calls → final response ──
            messages.append({
                "role": "assistant",
                "content": collected_content,
            })

            # Save to database
            meta_final = _build_meta("assistant")
            inp = _build_input()
            db_start = time.time()
            inter_id = await get_db().insert_interaction(
                user_id, session_id, role="assistant", content=collected_content,
                parent_id=parent_interaction_id,
                metadata=meta_final,
                input_data=inp,
            )
            db_dur = int((time.time() - db_start) * 1000)
            yield {"type": "db", "level": "db",
                   "op": "insert_interaction", "role": "assistant",
                   "tool_name": None, "id": inter_id, "ms": db_dur}

            yield {"type": "response", "level": "agent", "content": collected_content}
            return

        # ── Max turns reached ──
        yield {"type": "pipeline", "level": "pipeline",
               "step": "max_turns_reached", "turn": turn_count,
               "max_turns": max_turns,
               "message": f"Reached maximum {max_turns} turns"}
        yield {
            "type": "response", "level": "agent",
            "content": "I've reached the maximum number of turns. What would you like to do next?",
        }
    except asyncio.CancelledError:
        logger.info(f"stream_agent_events for session {session_id} cancelled by interrupt.")
        return
    except Exception as e:
        logger.error(f"stream_agent_events error: {e}", exc_info=True)
        yield {"type": "error", "level": "agent", "message": f"Unexpected error in agent loop: {e}"}
        return

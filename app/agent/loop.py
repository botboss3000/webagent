"""
Simple agent loop implementation.
"""
import asyncio
import difflib
import json
import logging
import os
import time
from typing import Dict, Any, List, Optional, Callable, Awaitable

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


def _check_user_confirmed(messages: List[Dict[str, Any]], tool_name: str) -> bool:
    """Check if the user confirmed a destructive tool call."""
    last_assistant_content = ""
    last_user_content = ""
    for msg in reversed(messages):
        if msg.get("role") == "user" and not last_user_content:
            last_user_content = (msg.get("content") or "").lower()
        if msg.get("role") == "assistant" and not last_assistant_content:
            last_assistant_content = (msg.get("content") or "").lower()

    ask_keywords = ["would you like me to", "should i", "shall i",
                     "let me know if", "do you want me to",
                     "confirm", "approve", "ok to", "okay to",
                     "proceed", "go ahead and"]
    model_asked = any(kw in last_assistant_content for kw in ask_keywords)

    confirm_keywords = ["yes", "go ahead", "proceed", "approved", "ok", "okay",
                        "sure", "do it", "confirm", "go for it", "please do"]

    if not model_asked:
        return any(kw in last_user_content for kw in confirm_keywords)
    return any(kw in last_user_content for kw in confirm_keywords)


async def run_agent_loop(
    user_id: str,
    session_id: str,
    user_message: str,
    system_prompt: str,
    history: Optional[List[Dict[str, str]]] = None,
    parent_interaction_id: str | None = None,
    event_callback: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> str:
    """
    Run the agent loop with tool calling capability.

    Args:
        event_callback: Optional async callback receiving pipeline/db events for visualizer.
    """
    from app.tools.loader import load_tools

    async def _emit(event: Dict[str, Any]):
        if event_callback:
            try:
                await event_callback(event)
            except Exception:
                pass

    load_start = time.time()
    tools = await load_tools(user_id)
    load_duration = int((time.time() - load_start) * 1000)

    await _emit({"type": "pipeline", "level": "pipeline",
                 "step": "load_tools", "count": len(tools),
                 "names": list(tools.keys()),
                 "duration_ms": load_duration})

    # Prepare messages
    messages: List[Dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    turn_count = 0
    max_turns = 20
    permission_granted = False
    model_name = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v3.2")

    while turn_count < max_turns:
        turn_count += 1

        await _emit({"type": "pipeline", "level": "pipeline",
                     "step": "turn_start", "turn": turn_count, "max_turns": max_turns})

        # Ask for permission to continue after 10 turns
        if turn_count == 11 and not permission_granted:
            permission_message = "I've reached 10 conversation turns. Would you like me to continue? I can go up to 20 turns total."
            messages.append({"role": "system", "content": permission_message})
        
        # Check if user has granted permission to continue (in their last message)
        if turn_count >= 11 and not permission_granted:
            last_user_msg = next((msg for msg in reversed(messages) if msg.get("role") == "user"), None)
            if last_user_msg:
                user_content = last_user_msg.get("content", "").lower()
                permission_keywords = ["keep going", "continue", "yes", "sure", "ok", "okay", "proceed", "go ahead", "permission granted"]
                if any(keyword in user_content for keyword in permission_keywords):
                    permission_granted = True
                    max_turns = turn_count + 10
                    messages.append({"role": "system", "content": f"Permission granted. I'll continue for {max_turns - turn_count} more turns."})

        # Build tool definitions
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

        await _emit({"type": "pipeline", "level": "pipeline",
                     "step": "tool_defs_built", "count": len(tool_definitions)})

        # Call OpenAI API
        await _emit({"type": "pipeline", "level": "pipeline",
                     "step": "llm_call_start", "model": model_name,
                     "message_count": len(messages),
                     "turn": turn_count})

        start_time = time.time()
        try:
            response = await _get_client().chat.completions.create(
                model=model_name,
                messages=messages,
                tools=tool_definitions if tool_definitions else None,
                tool_choice="auto" if tool_definitions else None,
                temperature=0.0,
                max_tokens=4096,
            )
            duration_ms = int((time.time() - start_time) * 1000)
        except Exception as e:
            logger.error(f"Error calling OpenAI API: {e}")
            await _emit({"type": "error", "level": "agent", "message": str(e)})
            return f"I encountered an error while processing your request: {str(e)}"

        # Token usage
        usage = response.usage
        input_tokens = usage.prompt_tokens if usage else None
        output_tokens = usage.completion_tokens if usage else None

        await _emit({"type": "pipeline", "level": "pipeline",
                     "step": "llm_call_end", "duration_ms": duration_ms,
                     "input_tokens": input_tokens, "output_tokens": output_tokens,
                     "has_tool_calls": bool(response.choices[0].message.tool_calls)})

        # Build metadata (lightweight: model, tokens, timing)
        def _build_meta(role: str) -> str:
            return json.dumps({
                "model": model_name,
                "turn": turn_count,
                "duration_ms": duration_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "role": role,
            })

        # Build input: the exact messages array sent to the LLM
        def _build_input() -> str:
            return json.dumps(messages)

        # Process response
        choice = response.choices[0]
        assistant_message = choice.message
        content = assistant_message.content or ""
        tool_calls_data = assistant_message.tool_calls

        # Build persisted content with tool call info embedded
        persisted_content = content
        if tool_calls_data:
            tc_summary = json.dumps([
                {"name": tc.function.name, "args": tc.function.arguments}
                for tc in tool_calls_data
            ])
            persisted_content += f"\n\n[Tool calls: {tc_summary}]"

        # Emit tool_call events
        if tool_calls_data:
            for tc in tool_calls_data:
                try:
                    tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    tool_args = {}
                await _emit({"type": "tool_call", "level": "agent",
                             "tool": tc.function.name, "args": tool_args})

        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls_data})

        # Save assistant message (parent = user message or previous assistant)
        meta = _build_meta("assistant")
        inp = _build_input()
        db_start = time.time()
        asst_id = await get_db().insert_interaction(
            user_id, session_id, role="assistant", content=persisted_content if tool_calls_data else content,
            parent_id=parent_interaction_id,
            metadata=meta,
            input_data=inp,
        )
        db_dur = int((time.time() - db_start) * 1000)
        await _emit({"type": "db", "level": "db",
                     "op": "insert_interaction", "role": "assistant",
                     "tool_name": None, "id": asst_id, "ms": db_dur})

        if not tool_calls_data:
            await _emit({"type": "response", "level": "agent", "content": content or "I've completed my analysis."})
            return content or "I've completed my analysis."

        # Execute tools
        async def execute_one(tool_call):
            tool_name = tool_call.function.name
            try:
                tool_args = json.loads(tool_call.function.arguments) if tool_call.function.arguments else {}
            except json.JSONDecodeError:
                tool_args = {}
            tool_call_id = tool_call.id

            validation_error = await validate_tool_call(tool_name, tool_args, tools)
            if validation_error:
                content = json.dumps(validation_error)
                return {"role": "tool", "content": content, "tool_call_id": tool_call_id, "tool_name": tool_name,
                        "success": False, "duration_ms": 0, "input_params": tool_args, "validation_error": True}

            # Guardrail check for destructive tools
            if tool_name in DESTRUCTIVE_TOOLS:
                if not _check_user_confirmed(messages, tool_name):
                    return {"role": "tool", "content": json.dumps({"status": "blocked", "message": f"Tool '{tool_name}' requires user confirmation."}),
                            "tool_call_id": tool_call_id, "tool_name": tool_name,
                            "success": False, "duration_ms": 0, "input_params": tool_args, "guardrail_blocked": True}

            tool_start_time = time.time()
            try:
                handler = tools[tool_name].handler if hasattr(tools[tool_name], 'handler') else tools[tool_name]
                logger.info(f"Executing tool: {tool_name} args={tool_args}")
                result = await handler(**tool_args)
                result_str = str(result)
                success = True
            except Exception as e:
                result_str = f"Error: {e}"
                success = False

            exec_duration = int((time.time() - tool_start_time) * 1000)
            return {
                "role": "tool", "content": result_str,
                "tool_call_id": tool_call_id, "tool_name": tool_name,
                "success": success, "duration_ms": exec_duration,
                "error_message": None if success else result_str[:500],
                "input_params": tool_args,
            }

        # Emit validation + guardrail events before execution
        await _emit({"type": "pipeline", "level": "pipeline",
                     "step": "validate_start", "tool_count": len(tool_calls_data)})

        for tc in tool_calls_data:
            tool_name = tc.function.name
            try:
                tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                tool_args = {}
            validation_error = await validate_tool_call(tool_name, tool_args, tools)
            await _emit({"type": "pipeline", "level": "pipeline",
                         "step": "validate_result", "tool": tool_name,
                         "passed": validation_error is None,
                         "error": str(validation_error) if validation_error else None})

            if validation_error is None and tool_name in DESTRUCTIVE_TOOLS:
                await _emit({"type": "pipeline", "level": "pipeline",
                             "step": "guardrail_check", "tool": tool_name,
                             "status": "requires_confirmation",
                             "message": f"Tool '{tool_name}' requires user confirmation per system prompt"})
                if not _check_user_confirmed(messages, tool_name):
                    await _emit({"type": "pipeline", "level": "pipeline",
                                 "step": "guardrail_blocked", "tool": tool_name,
                                 "status": "blocked",
                                 "message": f"Tool '{tool_name}' BLOCKED: user confirmation not detected"})
                else:
                    await _emit({"type": "pipeline", "level": "pipeline",
                                 "step": "guardrail_override", "tool": tool_name,
                                 "status": "confirmed", "by": "user"})

        # Emit execute starts
        batch_tools = []
        for tc in tool_calls_data:
            await _emit({"type": "pipeline", "level": "pipeline",
                         "step": "execute_start", "tool": tc.function.name})
            batch_tools.append(tc.function.name)

        await _emit({"type": "pipeline", "level": "pipeline",
                     "step": "execute_batch_start", "tool_count": len(tool_calls_data),
                     "tools": batch_tools})

        tool_responses = await asyncio.gather(*[execute_one(tc) for tc in tool_calls_data])
        messages.extend(tool_responses)

        for tr in tool_responses:
            meta = {
                "success": tr.get("success"),
                "duration_ms": tr.get("duration_ms"),
                "input_params": tr.get("input_params"),
            }
            if tr.get("error_message"):
                meta["error_message"] = tr["error_message"]

            db_start = time.time()
            inter_id = await get_db().insert_interaction(
                user_id, session_id, role="tool", content=tr["content"],
                parent_id=asst_id,
                tool_call_id=tr.get("tool_call_id"),
                tool_name=tr.get("tool_name"),
                metadata=json.dumps(meta),
            )
            db_dur = int((time.time() - db_start) * 1000)
            await _emit({"type": "db", "level": "db",
                         "op": "insert_interaction", "role": "tool",
                         "tool_name": tr.get("tool_name"), "id": inter_id, "ms": db_dur})

            # Emit tool_result
            await _emit({"type": "tool_result", "level": "agent",
                         "tool": tr.get("tool_name"),
                         "result": tr["content"][:2000],
                         "duration_ms": tr.get("duration_ms"),
                         "error": not tr.get("success"),
                         "error_type": "guardrail_blocked" if tr.get("guardrail_blocked") else ("execution_error" if not tr.get("success") else None),
                         "recoverable": True})

            # Emit execute_end
            await _emit({"type": "pipeline", "level": "pipeline",
                         "step": "execute_end", "tool": tr.get("tool_name"),
                         "duration_ms": tr.get("duration_ms"),
                         "success": tr.get("success")})

            # Track skill execution
            try:
                tool_name = tr.get("tool_name")
                if tool_name:
                    db = get_db()
                    skill_id = await db.skill_get_id_by_name(user_id, tool_name)
                    if skill_id:
                        await db.skill_track_execution(
                            skill_id=skill_id,
                            user_id=user_id,
                            session_id=session_id,
                            success=tr.get("success", False),
                            duration_ms=tr.get("duration_ms", 0),
                            interaction_id=inter_id,
                            error_message=tr.get("error_message"),
                            input_params=tr.get("input_params"),
                            output_summary=tr["content"][:200],
                        )
                        rating_info = await db.skill_get_rating(skill_id)
                        new_rating = rating_info.get("score") if rating_info else None
                        await _emit({"type": "db", "level": "db",
                                     "op": "skill_track", "tool": tool_name,
                                     "success": tr.get("success"), "new_rating": new_rating})
            except Exception as track_err:
                logger.debug(f"Skill tracking skipped: {track_err}")

        # Check continue
        will_continue = turn_count < max_turns
        await _emit({"type": "pipeline", "level": "pipeline",
                     "step": "check_continue", "turn": turn_count,
                     "max_turns": max_turns, "will_continue": will_continue})

    # Max turns reached
    await _emit({"type": "pipeline", "level": "pipeline",
                 "step": "max_turns_reached", "turn": turn_count,
                 "max_turns": max_turns,
                 "message": f"Reached maximum {max_turns} turns"})
    return f"I've reached the maximum number of conversation turns ({max_turns}). Let me know if you need anything else."


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


async def call_agent(
    user_id: str,
    session_id: str,
    message: str,
    system_prompt: str = "",
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    """
    Call the agent with user input and get a response.

    Args:
        user_id: User identifier
        session_id: Session identifier
        message: User message
        system_prompt: System prompt (optional)
        history: Message history (optional)

    Returns:
        Agent response
    """
    return await run_agent_loop(user_id, session_id, message, system_prompt, history)

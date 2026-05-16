"""
Unified Agent loop engine.

Handles both non-streaming (buffered) and streaming (token-by-token) logic,
while keeping a single source of truth for validations, guardrails, and DB persistence.

Events yielded:
  {"type": "stream", "level": "agent", "content": "..."}       — token-by-token LLM output
  {"type": "tool_call", "level": "agent", "tool": "...", "args": {...}}     — agent invoked a tool
  {"type": "tool_result", "level": "agent", "tool": "...", "result": "...", "duration_ms": N}  — tool returned
  {"type": "response", "level": "agent", "content": "..."}     — final answer (no more tool calls)
  {"type": "error", "level": "agent", "message": "..."}        — something went wrong
  {"type": "pipeline", "level": "pipeline", "step": "...", ...} — internal agent logic
  {"type": "db", "level": "db", "op": "...", ...}              — database operations
  {"type": "interrupted", "level": "agent", "message": "..."}   — interrupted between turns
"""

import asyncio
import difflib
import json
import logging
import os
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

from app.agent.error_classifier import classify_tool_error, ToolError
from app.agent.loop_executor import LoopConfig
from app.db import get_db
from app.db.system_prompt_fragments import get_prompt_fragments
from app.optimizer.runner import run_optimizer_async


def _fire_optimizer(user_id: str, session_id: str, channel: Optional[str] = None) -> None:
    """Fire-and-forget optimizer task with error trapping.
    Only fires if optimizer config mode is 'live'.
    """
    try:
        from app.optimizer.config import load_config
        cfg = load_config()
        mode = cfg.get("mode", "")
        if mode != "live":
            logger.debug("Optimizer: skipped for session %s (mode=%s)", session_id, mode)
            return
    except Exception:
        logger.debug("Optimizer: skipped for session %s (load_config failed)", session_id)
        return
    async def _run():
        try:
            logger.info("Optimizer: triggering for session %s (user=%s, channel=%s)", session_id, user_id, channel)
            result = await run_optimizer_async(user_id, session_id, channel)
            logger.info("Optimizer: completed for session %s -> %s", session_id, result)
        except Exception as e:
            logger.error("Optimizer: crashed for session %s: %s", session_id, e, exc_info=True)
    asyncio.create_task(_run())

logger = logging.getLogger(__name__)

_client = None
_current_base_url = None
_current_api_key = None

# ── Destructive tools that require confirmation ──
DESTRUCTIVE_TOOLS = {"edit_source", "write_source", "delete_source",
                     "run_command", "restart_server"}


def _get_client():
    global _client, _current_base_url, _current_api_key
    
    base_url = os.environ.get("LLM_BASE_URL") or os.environ.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1"
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or ""

    # Re-initialize if env changed or first time
    if _client is None or base_url != _current_base_url or api_key != _current_api_key:
        try:
            from openai import AsyncOpenAI
        except ImportError:
            from app.openai_compat import AsyncOpenAI

        _client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=60.0,
        )
        _current_base_url = base_url
        _current_api_key = api_key

    return _client


_active_race_tasks = set()

async def _race_llm_calls(
    messages: list,
    tool_definitions: list,
    multi_providers: list,
    user_id: str,
    save_loser_callback: Optional[Any] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Race N provider LLM calls in parallel.
    Yields live stream events from the fastest provider that emits a content chunk.
    All losers are cancelled. On all-fail, yields an error event.

    The caller must collect content and tool_calls from yielded events:
      - {"type": "stream", ...} — content chunk from winner
      - {"type": "pipeline", "step": "parallel_winner", ...} — winner announcement
      - {"type": "pipeline", "step": "parallel_complete", "content": str,
         "tool_calls": dict, "provider": str, "model": str,
         "input_tokens": int, "output_tokens": int, "cost": float} — final result
      - {"type": "error", ...} — all providers failed
    """
    import json as _json

    queue: asyncio.Queue = asyncio.Queue()

    winner_idx: Optional[int] = None
    winner_provider_name = ""
    winner_model_name = ""
    collected_content = ""
    collected_tool_calls: Dict[int, Any] = {}
    final_input_tokens: Optional[int] = None
    final_output_tokens: Optional[int] = None
    final_cost: Optional[float] = None

    async def _stream_one(pid: int, cfg: dict) -> None:
        """Stream from one provider, push events to queue."""
        try:
            base_url = cfg.get("base_url", "")
            api_key = cfg.get("api_key", "")
            model = cfg.get("model", "")
            prov_name = cfg.get("provider", "unknown")

            # Create an isolated client per provider
            try:
                from openai import AsyncOpenAI
            except ImportError:
                from app.openai_compat import AsyncOpenAI

            client = AsyncOpenAI(
                base_url=base_url,
                api_key=api_key,
                timeout=30.0,
            )

            # Debug log to verify task starts
            logger.info(f"[RACE] Provider {prov_name} starting call")

            stream = await client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tool_definitions if tool_definitions else None,
                tool_choice="auto" if tool_definitions else None,
                temperature=0.0,
                max_tokens=4096,
                stream=True,
                stream_options={"include_usage": True},
            )

            local_content = ""
            local_tool_calls: Dict[int, Any] = {}
            local_in_tok = None
            local_out_tok = None
            local_cost = None

            async for chunk in stream:
                if chunk.usage:
                    local_in_tok = chunk.usage.prompt_tokens
                    local_out_tok = chunk.usage.completion_tokens
                    extra = getattr(chunk.usage, 'model_extra', None)
                    if extra and 'total_cost' in extra:
                        local_cost = extra['total_cost']

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                if not delta:
                    continue

                if delta.content:
                    local_content += delta.content
                    await queue.put(("chunk", pid, prov_name, model,
                                     delta.content))

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in local_tool_calls:
                            local_tool_calls[idx] = tc
                        else:
                            existing = local_tool_calls[idx]
                            if tc.function:
                                if tc.function.name:
                                    existing.function.name = tc.function.name
                                if tc.function.arguments:
                                    existing.function.arguments = (
                                        (existing.function.arguments or "")
                                        + tc.function.arguments
                                    )

            # Stream complete
            await queue.put(("done", pid, prov_name, model,
                             local_content, local_tool_calls,
                             local_in_tok, local_out_tok, local_cost))
            
            # If we are a loser, save to DB in the background
            logger.info(f"[RACE] Provider {prov_name} finished. winner_idx={winner_idx}")
            if winner_idx is not None and pid != winner_idx:
                logger.info(f"[RACE] Provider {prov_name} saving as loser.")
                if save_loser_callback:
                    await save_loser_callback(
                        prov_name, model, local_content, local_tool_calls,
                        local_in_tok, local_out_tok, local_cost,
                        int((time.time() - start_time) * 1000)
                    )

        except asyncio.CancelledError:
            with open("loser_fatal.log", "a") as f: f.write(f"Cancelled {prov_name}\n")
            # Save partial results on cancellation
            if save_loser_callback and (local_content or local_in_tok is not None):
                await save_loser_callback(
                    prov_name, model, local_content, local_tool_calls,
                    local_in_tok, local_out_tok, local_cost,
                    int((time.time() - start_time) * 1000),
                    cancelled=True
                )
        except Exception as e:
            with open("loser_fatal.log", "a") as f: f.write(f"Error {prov_name}: {e}\n")
            logger.error(f"[RACE] Provider {prov_name} error: {e}")
            await queue.put(("error", pid, str(e)))
            try:
                from app.admin.settings import update_multi_provider_rating
                await update_multi_provider_rating(user_id, prov_name, model, -1)
            except Exception as rating_err:
                logger.warning(f"Failed to lower rating for {prov_name}: {rating_err}")

    start_time = time.time()
    async def _safe_stream_one(pid: int, cfg: dict):
        try:
            await asyncio.shield(_stream_one(pid, cfg))
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    # ── Launch all provider tasks ──
    tasks = []
    for i, prov in enumerate(multi_providers):
        t = asyncio.create_task(_safe_stream_one(i, prov))
        _active_race_tasks.add(t)
        t.add_done_callback(_active_race_tasks.discard)
        tasks.append(t)

    # ── Consume queue until winner finishes or all fail ──
    remaining = len(tasks)

    while True:
        item = await queue.get()
        etype = item[0]
        pid = item[1]

        if etype == "chunk":
            _prov_name = item[2]
            _model_name = item[3]
            _chunk_text = item[4]

            if winner_idx is None:
                # First chunk — this provider wins
                winner_idx = pid
                winner_provider_name = _prov_name
                winner_model_name = _model_name
                yield {"type": "pipeline", "level": "pipeline",
                       "step": "parallel_winner",
                       "provider": winner_provider_name,
                       "model": winner_model_name}

            if pid == winner_idx:
                collected_content += _chunk_text
                yield {"type": "stream", "level": "agent",
                       "content": _chunk_text}

        elif etype == "done":
            _prov_name = item[2]
            _model_name = item[3]
            _content = item[4]
            _tcs = item[5]
            _in_tok = item[6]
            _out_tok = item[7]
            _cost = item[8]

            if winner_idx is None:
                # First to finish without any chunks
                winner_idx = pid
                winner_provider_name = _prov_name
                winner_model_name = _model_name
                collected_content = _content
                collected_tool_calls = _tcs
                final_input_tokens = _in_tok
                final_output_tokens = _out_tok
                final_cost = _cost
                yield {"type": "pipeline", "level": "pipeline",
                       "step": "parallel_winner",
                       "provider": winner_provider_name,
                       "model": winner_model_name}
                break

            if pid == winner_idx:
                collected_content = _content
                collected_tool_calls = _tcs
                final_input_tokens = _in_tok
                final_output_tokens = _out_tok
                final_cost = _cost
                break
            # else: a loser finished, ignore. Background save handled in _stream_one.

        elif etype == "error":
            remaining -= 1
            if remaining <= 0:
                # All failed
                err_msgs = []
                # Collect err messages from finished tasks
                for t in tasks:
                    if t.done() and not t.cancelled():
                        try:
                            t.result()
                        except Exception as exc:
                            err_msgs.append(str(exc)[:200])
                # Also include the immediate error
                err_msgs.append(item[2][:200])
                unique = list(dict.fromkeys(err_msgs))  # dedup, preserve order
                joined = "; ".join(unique[:3])
                yield {"type": "error", "level": "agent",
                       "message": f"All {len(multi_providers)} providers failed: {joined}"}
                # (No cancellation, tasks mostly failed already)
                await asyncio.gather(*tasks, return_exceptions=True)
                return

    # ── Winner found — we do NOT cancel losers, let them finish and save ──
    # (Loser tasks will simply run their course and fire save_loser_callback)

    # ── Emit final result for the caller ──
    yield {
        "type": "pipeline",
        "level": "pipeline",
        "step": "parallel_complete",
        "content": collected_content,
        "tool_calls": collected_tool_calls,
        "provider": winner_provider_name,
        "model": winner_model_name,
        "input_tokens": final_input_tokens,
        "output_tokens": final_output_tokens,
        "cost": final_cost,
    }


async def _check_interrupt(session_id: str, interrupt_event: Optional[asyncio.Event]):
    """
    Checks the local event and the DB interrupt flag.
    Raises CancelledError if interrupted.
    """
    if interrupt_event and interrupt_event.is_set():
        raise asyncio.CancelledError("Agent interrupted by new user message (local event).")

    db = get_db()
    if await db.check_interrupt(session_id):
        # Clear the flag so next runs are clean
        await db.clear_interrupt(session_id)
        raise asyncio.CancelledError("Agent interrupted by new user message (db flag).")


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


async def stream_agent_events(
    user_id: str,
    session_id: str,
    user_message: str,
    system_prompt: str,
    agent_id: str,
    history: Optional[List[Dict[str, Any]]] = None,
    parent_interaction_id: Optional[str] = None,
    interrupt_event: Optional[asyncio.Event] = None,
    max_turns: int = 10,
    channel: Optional[str] = None,
    db: Optional[Any] = None,
    agent_template_id: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
    loop_config: Optional[LoopConfig] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Run the unified agent loop and yield structured events.
    agent_template_id is used to gate admin-only tools (e.g. 'admin-agent').
    allowed_tools is the list of Tier-2 tool names DISABLED for this agent.
    """
    from app.tools.loader import load_tools
    from app.admin.settings import load_provider_for_user

    # Load THIS user's provider config (not shared with any other user)
    await load_provider_for_user(user_id)

    model_name = os.environ.get("LLM_MODEL") or os.environ.get("OPENROUTER_MODEL") or "deepseek/deepseek-v4-flash"
    provider_name = os.environ.get("LLM_PROVIDER", "openrouter")

    load_start = time.time()
    tools = await load_tools(user_id, agent_template_id=agent_template_id, allowed_tools=allowed_tools)
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
    original_max_turns = max_turns  # the configured block size; used to rearm at each ceiling
    last_extension_at = 0           # ceiling turn at which we last extended (0 = not yet)

    try:
        from app.db import get_db
        if db is None:
            db = db or get_db()

        # Fetch agent name for prefixing all outputs
        agent_name = "Agent"
        _agent_rec: Optional[Dict[str, Any]] = None
        if agent_id:
            _agent_rec = await db.get_agent_by_id(agent_id)
            if _agent_rec and _agent_rec.get("name"):
                agent_name = _agent_rec["name"]

        # Build loop config — drives per-node enable/disable at runtime.
        # If caller supplied one (e.g. tests), use it directly; otherwise
        # parse the agent's stored loop_logic (backward-compat: flat array
        # = all nodes enabled, preserving current behavior exactly).
        if loop_config is None:
            loop_config = LoopConfig.from_agent(_agent_rec)

        def prefix_content(content: str) -> str:
            """Prefix agent name to content."""
            if not content:
                return content
            return f"{agent_name}: {content}"

        # Use list to track state across function boundaries
        first_stream_chunk_state = [True]  # [is_first_chunk]

        while turn_count < max_turns:
            if loop_config.is_enabled("interrupt_chk"):
                await _check_interrupt(session_id, interrupt_event)

            turn_count += 1
            if agent_id:
                await db.increment_agent_turn_count(agent_id)

            # Reset stream chunk tracker for new turn
            first_stream_chunk_state[0] = True

            # ── Pipeline: turn start ──
            yield {"type": "pipeline", "level": "pipeline",
                   "step": "turn_start", "turn": turn_count, "max_turns": max_turns}

            # Ask for permission to continue when the agent reaches the configured turn ceiling.
            # Rearms automatically after each granted extension (last_extension_at tracks the
            # ceiling at which we last extended, so asking fires again at each new ceiling).
            if loop_config.is_enabled("permission_chk") and turn_count == max_turns and last_extension_at != max_turns:
                fr = get_prompt_fragments()
                permission_message = (fr.get("turn_permission_request") or "").strip()
                if permission_message:
                    messages.append({"role": "system", "content": permission_message})

            # Check if user has granted permission (looks at their most recent message).
            # Only active at the current ceiling, before we have already extended at that ceiling.
            if loop_config.is_enabled("permission_chk") and turn_count >= max_turns and last_extension_at < max_turns:
                last_user_msg = next((msg for msg in reversed(messages) if msg.get("role") == "user"), None)
                if last_user_msg:
                    user_content = last_user_msg.get("content", "").lower()
                    permission_keywords = ["keep going", "continue", "yes", "sure", "ok", "okay", "proceed", "go ahead", "permission granted"]
                    if any(keyword in user_content for keyword in permission_keywords):
                        last_extension_at = max_turns       # mark this ceiling as extended
                        max_turns += original_max_turns     # grant one full block more
                        remaining_turns = original_max_turns
                        tpl = (get_prompt_fragments().get("turn_permission_granted_template") or "").strip()
                        if tpl:
                            try:
                                granted_text = tpl.format(remaining_turns=remaining_turns)
                            except (KeyError, ValueError):
                                granted_text = (
                                    f"Permission granted. I'll continue for {remaining_turns} more turns."
                                )
                            messages.append({"role": "system", "content": granted_text})

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

            # ── Pipeline + log: tool definitions built ──
            logger.debug("LLM TOOL DEFINITIONS (%d): %s", len(tool_definitions),
                         json.dumps([{"name": td["function"]["name"], "desc": td["function"]["description"][:80]}
                                     for td in tool_definitions], indent=2))
            yield {"type": "pipeline", "level": "pipeline",
                   "step": "tool_defs_built", "count": len(tool_definitions),
                   "tool_definitions": [{"name": td["function"]["name"],
                                          "description": td["function"]["description"][:120]}
                                         for td in tool_definitions]}

            llm_start_time = time.time()

            def _build_meta(role: str, in_tok: int=None, out_tok: int=None, cost: float=None) -> str:
                meta = {
                    "provider": provider_name,
                    "model": model_name,
                    "turn": turn_count,
                    "duration_ms": int((time.time() - llm_start_time) * 1000),
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "role": role,
                    "streaming": True,
                }
                if cost is not None:
                    meta["cost"] = cost
                return json.dumps(meta)

            def _build_input() -> str:
                return json.dumps(messages)

            if loop_config.is_enabled("interrupt_chk"):
                await _check_interrupt(session_id, interrupt_event)

            # ── Pipeline: LLM call start ──
            yield {"type": "pipeline", "level": "pipeline",
                   "step": "llm_call_start", "model": model_name,
                   "message_count": len(messages),
                   "turn": turn_count}

            # ── Stream the LLM response ──
            llm_start = time.time()

            async def _save_loser(p_name, m_name, l_content, l_tcs, l_in, l_out, l_cost, ms, cancelled=False):
                with open("loser_trace_db.log", "a") as f:
                    f.write(f"Triggered _save_loser for {p_name} {m_name}\n")
                # Build an openai-style tool calls list
                loser_tcs = []
                if l_tcs:
                    for tc in l_tcs.values():
                        loser_tcs.append({
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
                        })
                
                # Suffix tool calls to content just like normal
                save_content = l_content or ""
                if loser_tcs:
                    tc_summary = json.dumps([
                        {"name": tc["function"]["name"], "args": tc["function"]["arguments"]}
                        for tc in loser_tcs
                    ])
                    save_content += f"\n\n[Tool calls: {tc_summary}]"
                
                meta_dict = {
                    "provider": p_name,
                    "model": m_name,
                    "turn": turn_count,
                    "duration_ms": ms,
                    "input_tokens": l_in or 0,
                    "output_tokens": l_out or 0,
                    "role": "assistant",
                    "streaming": True,
                    "parallel_loser": True,
                    "cancelled": cancelled,
                }
                if l_cost is not None:
                    meta_dict["cost"] = l_cost

                inp = json.dumps(messages)
                outp = json.dumps({"role": "assistant", "content": l_content, "tool_calls": loser_tcs})
                
                from app.db import get_db
                try:
                    await db.insert_interaction(
                        user_id, session_id, role="assistant", content=save_content,
                        parent_id=parent_interaction_id,
                        channel=channel,
                        metadata=json.dumps(meta_dict),
                        input_data=inp,
                        output_data=outp,
                        sender_id=agent_id,
                        receiver_id=user_id,
                    )
                    with open("loser_trace_db.log", "a") as f:
                        f.write(f"DB insert SUCCESS for {p_name}\n")
                except Exception as e:
                    with open("loser_trace_db.log", "a") as f:
                        f.write(f"DB insert Exception: {e}\n")
                    logger.warning("Failed to save parallel loser response: %s", e)
                except BaseException as be:
                    with open("loser_trace_db.log", "a") as f:
                        f.write(f"DB insert BaseException (Cancelled?): {be}\n")

            # ── Check for parallel multi-provider mode ──
            _parallel_mode = os.environ.get("PARALLEL_MODE", "").lower() == "true"
            _multi_providers_raw = os.environ.get("MULTI_PROVIDERS", "")

            collected_content = ""
            collected_tool_calls: Dict[int, Any] = {}
            input_tokens = None
            output_tokens = None
            llm_cost = None
            _used_parallel = False

            if _parallel_mode and _multi_providers_raw:
                try:
                    _multi_providers_list = json.loads(_multi_providers_raw)
                except (json.JSONDecodeError, TypeError):
                    _multi_providers_list = []

                if len(_multi_providers_list) >= 2:
                    _used_parallel = True
                    _parallel_had_error = False

                    async for _pe in _race_llm_calls(
                        messages, tool_definitions, _multi_providers_list, user_id=user_id, save_loser_callback=_save_loser
                    ):
                        if _pe["type"] == "stream":
                            collected_content += _pe["content"]
                            # Prefix only the first stream chunk of the turn
                            if first_stream_chunk_state[0]:
                                _pe = dict(_pe)  # shallow copy to avoid mutating original
                                _pe["content"] = prefix_content(_pe["content"])
                                first_stream_chunk_state[0] = False
                            yield _pe
                        elif _pe["type"] == "pipeline":
                            if _pe["step"] == "parallel_winner":
                                # Update model_name and provider_name for metadata
                                model_name = _pe.get("model", model_name)
                                provider_name = _pe.get("provider", provider_name)
                                yield _pe
                            elif _pe["step"] == "parallel_complete":
                                # Final result from race engine
                                collected_content = _pe.get("content", collected_content)
                                collected_tool_calls = _pe.get("tool_calls", collected_tool_calls)
                                input_tokens = _pe.get("input_tokens")
                                output_tokens = _pe.get("output_tokens")
                                llm_cost = _pe.get("cost")
                                model_name = _pe.get("model", model_name)
                                provider_name = _pe.get("provider", provider_name)
                                yield _pe
                            else:
                                yield _pe
                        elif _pe["type"] == "error":
                            yield _pe
                            _parallel_had_error = True
                            break
                        # Forward other event types directly
                        elif _pe["type"] in ("tool_call", "tool_result"):
                            yield _pe

                    if _parallel_had_error:
                        return

            if not _used_parallel:
                # ── Single-provider path (original) ──
                try:
                    stream = await _get_client().chat.completions.create(
                        model=model_name,
                        messages=messages,
                        tools=tool_definitions if tool_definitions else None,
                        tool_choice="auto" if tool_definitions else None,
                        temperature=0.0,
                        max_tokens=4096,
                        stream=True,
                        stream_options={"include_usage": True},
                    )
                except Exception as e:
                    yield {"type": "error", "level": "agent", "message": f"LLM call failed: {e}"}
                    return

                async for chunk in stream:

                    if chunk.usage:
                        input_tokens = chunk.usage.prompt_tokens
                        output_tokens = chunk.usage.completion_tokens
                        extra = getattr(chunk.usage, 'model_extra', None)
                        if extra and 'total_cost' in extra:
                            llm_cost = extra['total_cost']

                    if not chunk.choices:
                        continue

                    delta = chunk.choices[0].delta
                    if not delta:
                        continue

                    if delta.content:
                        collected_content += delta.content
                        # Prefix only the first stream chunk of the turn
                        stream_content = delta.content
                        if first_stream_chunk_state[0]:
                            stream_content = prefix_content(stream_content)
                            first_stream_chunk_state[0] = False
                        yield {"type": "stream", "level": "agent", "content": stream_content}

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

                # Persist intermediate assistant message (with tool calls)
                assistant_content = collected_content or ""
                if full_tool_calls:
                    tool_calls_summary = json.dumps([
                        {"name": tc["function"]["name"], "args": tc["function"]["arguments"]}
                        for tc in full_tool_calls
                    ])
                    assistant_content += f"\n\n[Tool calls: {tool_calls_summary}]"
                meta_asst = _build_meta("assistant", input_tokens, output_tokens, llm_cost)
                inp = _build_input()
                outp = json.dumps({"role": "assistant", "content": collected_content, "tool_calls": full_tool_calls})
                db_start = time.time()
                asst_id = await db.insert_interaction(
                    user_id, session_id, role="assistant", content=assistant_content,
                    parent_id=parent_interaction_id,
                    channel=channel,
                    metadata=meta_asst,
                    input_data=inp,
                    output_data=outp,
                    sender_id=agent_id,
                    receiver_id=user_id,
                )
                db_dur = int((time.time() - db_start) * 1000)
                yield {"type": "db", "level": "db",
                       "op": "insert_interaction", "role": "assistant",
                       "tool_name": None, "id": asst_id, "ms": db_dur}

                # ── Pipeline: validation start ──
                yield {"type": "pipeline", "level": "pipeline",
                       "step": "validate_start", "tool_count": len(collected_tool_calls)}

                valid_calls: List[Any] = []
                blocked_calls: List[Any] = []
                for idx, tc in sorted(collected_tool_calls.items()):
                    await _check_interrupt(session_id, interrupt_event)
                    
                    tool_name = tc.function.name
                    try:
                        tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except json.JSONDecodeError:
                        tool_args = {}

                    validation_error = await validate_tool_call(tool_name, tool_args, tools)

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
                        
                        inp = _build_input()
                        outp = json.dumps({"role": "tool", "content": tool_msg["content"], "tool_call_id": tc.id, "name": tool_name, "success": False})
                        db_start = time.time()
                        inter_id = await db.insert_interaction(
                            user_id, session_id, role="tool", content=tool_msg["content"],
                            parent_id=asst_id,
                            tool_call_id=tc.id,
                            channel=channel,
                            metadata=json.dumps({"success": False, "duration_ms": 0, "input_params": tool_args, "error_message": "Validation failed"}),
                            input_data=inp,
                            output_data=outp,
                            sender_id=agent_id,
                            receiver_id=agent_id,
                        )
                        db_dur = int((time.time() - db_start) * 1000)
                        yield {"type": "db", "level": "db",
                               "op": "insert_interaction", "role": "tool",
                               "tool_name": tool_name, "id": inter_id, "ms": db_dur}
                    else:
                        if loop_config.is_enabled("guardrails") and tool_name in DESTRUCTIVE_TOOLS:
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
                                inp = _build_input()
                                outp = json.dumps({"role": "tool", "content": tool_msg["content"], "tool_call_id": tc.id, "name": tool_name, "success": False})
                                db_start = time.time()
                                inter_id = await db.insert_interaction(
                                    user_id, session_id, role="tool", content=tool_msg["content"],
                                    parent_id=asst_id,
                                    tool_call_id=tc.id,
                                    tool_name=tool_name,
                                    channel=channel,
                                    metadata=json.dumps({"success": False, "duration_ms": 0, "input_params": tool_args, "error_message": "Guardrail blocked — requires confirmation"}),
                                    input_data=inp,
                                    output_data=outp,
                                    sender_id=agent_id,
                                    receiver_id=agent_id,
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

                if loop_config.is_enabled("interrupt_chk"):
                    await _check_interrupt(session_id, interrupt_event)

                if valid_calls:
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

                    for _, _, tool_name, _ in valid_calls:
                        yield {"type": "pipeline", "level": "pipeline",
                               "step": "execute_start", "tool": tool_name}

                    tasks = [execute_one(name, args, tc.id) for _, tc, name, args in valid_calls]
                    results = await asyncio.gather(*tasks)

                    for (idx, tc, tool_name, tool_args), result in zip(valid_calls, results):
                        if loop_config.is_enabled("interrupt_chk"):
                            await _check_interrupt(session_id, interrupt_event)
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
                        
                        inp = _build_input()
                        outp = json.dumps({"role": "tool", "content": result["content"][:10000], "tool_call_id": tc.id, "name": tool_name, "success": success, "duration_ms": result["duration_ms"]})
                        db_start = time.time()
                        inter_id = await db.insert_interaction(
                            user_id, session_id, role="tool", content=tool_msg["content"],
                            parent_id=asst_id,
                            tool_call_id=tc.id,
                            tool_name=tool_name,
                            channel=channel,
                            metadata=tool_exec_meta,
                            input_data=inp,
                            output_data=outp,
                            sender_id=agent_id,
                            receiver_id=agent_id,
                        )
                        db_dur = int((time.time() - db_start) * 1000)
                        yield {"type": "db", "level": "db",
                               "op": "insert_interaction", "role": "tool",
                               "tool_name": tool_name, "id": inter_id, "ms": db_dur}

                        # ── Delegation check ──────────────────────────────
                        try:
                            import json as _json
                            # Skip delegation detection when node is disabled (result is None → inner if is False)
                            _res_obj = _json.loads(result["content"]) if loop_config.is_enabled("delegation_chk") else None
                            if isinstance(_res_obj, dict) and _res_obj.get("__delegate__"):
                                _tpl_id  = _res_obj["target_template_id"]
                                _ag_id   = _res_obj["target_agent_id"]
                                _ag_name = _res_obj.get("target_name", _tpl_id)
                                _ctx     = _res_obj.get("context", "")

                                # Rebind session to new agent
                                await db.bind_session_to_agent(session_id, _ag_id)

                                # Emit delegation pipeline event
                                yield {
                                    "type": "pipeline", "level": "pipeline",
                                    "step": "agent_delegation",
                                    "from_agent_id":       agent_id,
                                    "from_template_id":    agent_template_id,
                                    "to_agent_id":         _ag_id,
                                    "to_template_id":      _tpl_id,
                                    "to_agent_name":       _ag_name,
                                    "context":             _ctx,
                                }

                                # Switch loop state to new agent
                                agent_id         = _ag_id
                                agent_template_id = _tpl_id
                                agent_name = _ag_name  # Update prefix for new agent

                                # Reload tools for the new template
                                from app.tools.loader import load_tools as _load_tools
                                tools = await _load_tools(user_id, agent_template_id=_tpl_id)

                                # Inject new agent's system prompt as a system message
                                try:
                                    _new_agents = await db.list_agents_for_user(user_id, include_admin=True)
                                    _new_agent  = next((a for a in _new_agents if a.get("id") == _ag_id), None)
                                    if _new_agent:
                                        _new_sp = (_new_agent.get("system_prompt") or "").strip()
                                        if _new_sp:
                                            _switch_msg = "[AGENT SWITCH] You are now acting as " + _ag_name + ".\n\n" + _new_sp
                                            messages.append({"role": "system", "content": _switch_msg})
                                        if _ctx:
                                            messages.append({"role": "system", "content": f"Delegation context: {_ctx}"})
                                except Exception as _spe:
                                    logger.warning("Could not inject new agent system prompt: %s", _spe)
                        except (ValueError, KeyError, TypeError):
                            pass  # not a delegation signal

                        try:
                            db = db or get_db()
                            # Skip skill tracking when node is disabled (None → if skill_id is False)
                            skill_id = await db.skill_get_id_by_name(user_id, tool_name) if loop_config.is_enabled("skill_track") else None
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
                                rating_info = await db.skill_get_rating(skill_id)
                                new_rating = rating_info.get("score") if rating_info else None
                                yield {"type": "db", "level": "db",
                                       "op": "skill_track", "tool": tool_name,
                                       "success": success, "new_rating": new_rating}
                        except Exception as track_err:
                            logger.debug(f"Skill tracking skipped for {tool_name}: {track_err}")

                yield {"type": "pipeline", "level": "pipeline",
                       "step": "check_continue", "turn": turn_count,
                       "max_turns": max_turns, "will_continue": turn_count < max_turns}
                continue

            # ── No tool calls → final response ──
            messages.append({
                "role": "assistant",
                "content": collected_content,
            })

            meta_final = _build_meta("assistant", input_tokens, output_tokens, llm_cost)
            inp = _build_input()
            outp = json.dumps({"role": "assistant", "content": collected_content})
            db_start = time.time()
            inter_id = await db.insert_interaction(
                user_id, session_id, role="assistant", content=collected_content,
                parent_id=parent_interaction_id,
                channel=channel,
                metadata=meta_final,
                input_data=inp,
                output_data=outp,
                sender_id=agent_id,
                receiver_id=user_id,
            )
            db_dur = int((time.time() - db_start) * 1000)
            yield {"type": "db", "level": "db",
                   "op": "insert_interaction", "role": "assistant",
                   "tool_name": None, "id": inter_id, "ms": db_dur}

            yield {"type": "response", "level": "agent", "content": prefix_content(collected_content)}
            # Fire-and-forget optimizer after successful completion
            _fire_optimizer(user_id, session_id, channel)
            return

        # ── Max turns reached ──
        yield {"type": "pipeline", "level": "pipeline",
               "step": "max_turns_reached", "turn": turn_count,
               "max_turns": max_turns,
               "message": f"Reached maximum {max_turns} turns"}
        yield {
            "type": "response", "level": "agent",
            "content": prefix_content("I've reached the maximum number of turns. What would you like to do next?"),
        }
        # Fire-and-forget optimizer after max turns
        _fire_optimizer(user_id, session_id, channel)
    except asyncio.CancelledError as e:
        logger.info(f"Agent loop for session {session_id} cancelled: {e}")
        yield {"type": "interrupted", "level": "agent", "message": str(e)}
        _fire_optimizer(user_id, session_id, channel)
        return
    except Exception as e:
        logger.error(f"Agent loop error: {e}", exc_info=True)
        yield {"type": "error", "level": "agent", "message": f"Unexpected error in agent loop: {e}"}
        # Fire-and-forget optimizer even on error — may learn from failure
        _fire_optimizer(user_id, session_id, channel)
        return


async def run_agent_loop_buffered(
    user_id: str,
    session_id: str,
    user_message: str,
    system_prompt: str,
    agent_id: str,
    history: Optional[List[Dict[str, Any]]] = None,
    parent_interaction_id: Optional[str] = None,
    max_turns: int = 10,
    event_callback: Optional[Any] = None,
    channel: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    db: Optional[Any] = None,
    agent_template_id: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
) -> str:
    """
    Compatibility wrapper that runs the streaming loop internally,
    discards intermediate string chunks (stream events), and returns the final string.
    Sends all other events to event_callback.

    If timeout_seconds is set, raises asyncio.TimeoutError if the agent loop
    does not complete within that time.
    """
    final_response = ""

    async def _run():
        nonlocal final_response
        async for event in stream_agent_events(
            user_id=user_id,
            session_id=session_id,
            user_message=user_message,
            system_prompt=system_prompt,
            agent_id=agent_id,
            history=history,
            parent_interaction_id=parent_interaction_id,
            max_turns=max_turns,
            channel=channel,
            db=db,
            agent_template_id=agent_template_id,
            allowed_tools=allowed_tools,
        ):
            if event_callback:
                try:
                    await event_callback(event)
                except Exception:
                    pass
                    
            if event["type"] == "response":
                final_response = event["content"]
            elif event["type"] == "error":
                if not final_response:
                    final_response = f"I encountered an error: {event['message']}"
            elif event["type"] == "interrupted":
                if not final_response:
                    final_response = f"I was interrupted: {event['message']}"
                    
        if not final_response:
            final_response = "I completed the analysis but produced no output."

    if timeout_seconds is not None:
        try:
            await asyncio.wait_for(_run(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning(
                "Agent loop timed out after %ds for session %s",
                timeout_seconds, session_id,
            )
            if not final_response:
                final_response = (
                    f"I'm sorry, the request timed out after {timeout_seconds} seconds. "
                    f"The analysis was taking too long and had to be stopped. "
                    f"Please try a more specific request or use the stream endpoint for long-running tasks."
                )
    else:
        await _run()
        
    return final_response

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
import re
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

from app.agent.error_classifier import classify_tool_error, ToolError
from app.agent.loop_executor import LoopConfig
from app.db import get_db
from app.db.system_prompt_fragments import get_prompt_fragments
from app.optimizer.runner import run_optimizer_async
from app.billing import pricing as _billing_pricing
from app.billing import wallet as _billing_wallet


# ── Turn-limit & safety-cap defaults ─────────────────────────────────────────
# The turn limit uses 0 = unlimited. When turns are unlimited, this wall-clock
# cap is the real backstop so a tool-looping agent can never hang forever — the
# streaming chat path has no request timeout of its own. Graceful: at the cap
# the loop finalizes with a message instead of being hard-killed. Overridable
# globally via the AGENT_MAX_WALL_SECONDS env var and per-agent via the agent's
# max_wall_seconds field (set an agent's value to 0 to opt out of the cap).
DEFAULT_MAX_WALL_SECONDS = 600.0


def _fire_optimizer(user_id: str, session_id: str, channel: Optional[str] = None,
                    agent_template_id: Optional[str] = None) -> None:
    """Fire-and-forget optimizer task with error trapping.
    Only fires if optimizer config mode is 'live'.
    Never fires for the admin agent — it edits the codebase, it is not a
    user-facing chat agent whose prompt the optimizer should rewrite, and
    auto-spawning the Planner on its sessions is exactly the "calling on
    optimizer agents" behavior we want to stop.
    """
    if agent_template_id == "admin-agent":
        logger.debug("Optimizer: skipped for session %s (admin agent)", session_id)
        return
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

# ── Destructive tools that require confirmation (hardcoded baseline) ──
# These are always treated as destructive regardless of agent safety_policy.
# Per-agent additions live in agents.safety_policy.destructive_tools.
# Per-tool overrides live in tools.requires_confirmation.
# `write_source`, `edit_source`, and `delete_source` are NOT in this set —
# the agent is trusted to execute explicit user commands directly.
# The permission nuance (direct command vs. autonomous decision) is handled
# by the agent's system prompt, which tells the LLM:
#   "If the user directly commands a delete, just do it.
#    If you decide to delete on your own initiative, ask first."
# What remains gated: arbitrary shell commands (with read-only exemption)
# and server restart.
DESTRUCTIVE_TOOLS = frozenset({"run_command", "restart_server"})

# ── run_command per-arg exemption: read-only shell commands skip the gate ──
# `run_command` is destructive by default (it can do anything), but inspect-only
# invocations like `git status`, `ls`, `cat`, `grep` are routine and pointless
# to gate on. The guardrail node bypasses confirmation when the command:
#   1. contains no output redirection or subshell expansion, AND
#   2. every piece (split on safe chain separators `&&`, `||`, `;`, `|`) starts
#      with one of these allow-listed prefixes.
# This lets `cd /app && git status`, `git log | head`, `git status; git diff`
# pass while still blocking `git status; rm -rf /` or `cat x > y`.
SAFE_RUN_COMMAND_PREFIXES = (
    # shell movement (no output, no mutation)
    "cd",
    # git inspection
    "git status", "git log", "git diff", "git show", "git branch",
    "git ls-files", "git rev-parse", "git remote", "git stash list",
    "git config --get", "git config --list",
    # filesystem inspect
    "ls", "dir", "pwd", "tree", "stat", "file",
    "cat", "head", "tail", "type", "more",
    "find", "grep", "rg", "where", "which",
    "wc", "du", "df",
    # system info
    "whoami", "hostname", "date", "uname", "id", "groups", "uptime",
    "ps", "env", "printenv", "echo",
    # toolchain version probes
    "python --version", "python -V", "python3 --version", "python3 -V",
    "pip list", "pip show", "pip --version",
    "node --version", "node -v",
    "npm list", "npm ls", "npm --version", "npm -v",
)

# These ALWAYS make a command unsafe (output redirection or subshell capture).
_HARD_UNSAFE_TOKENS = (">", "<", "`", "$(", "|&")

# Regex that splits on chain separators. `&&` and `||` are matched before `|`
# and `;` so single-pipe / semicolon don't swallow them.
_CHAIN_SEPARATOR_RE = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")


def _is_safe_shell_command(command: Any) -> bool:
    """
    Return True for read-only shell commands that don't need user confirmation.

    Allows safe-prefix commands chained with `&&`, `||`, `;`, `|`, but rejects
    anything containing redirection (`>`, `<`) or subshell capture (backtick,
    `$(...)`). Each chained piece must independently be a safe prefix, so
    `cd /app && git status` passes but `git status; rm -rf /` does not.
    """
    if not isinstance(command, str):
        return False
    stripped = command.strip()
    if not stripped:
        return False
    if any(tok in stripped for tok in _HARD_UNSAFE_TOKENS):
        return False
    for piece in _CHAIN_SEPARATOR_RE.split(stripped):
        piece = piece.strip()
        if not piece:
            return False
        low = piece.lower()
        if not any(low == p or low.startswith(p + " ") for p in SAFE_RUN_COMMAND_PREFIXES):
            return False
    return True


def _parse_safety_policy(agent_rec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Parse an agent record's safety_policy JSON column into a dict."""
    if not agent_rec:
        return {}
    raw = agent_rec.get("safety_policy") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _build_effective_destructive_set(
    agent_rec: Optional[Dict[str, Any]],
    tools: Dict[str, Any],
) -> frozenset:
    """
    Build the effective set of tool names that require user confirmation before running.

    Merges three sources (union, never subtract):
      1. DESTRUCTIVE_TOOLS  — hardcoded baseline (admin tools always require confirmation).
      2. safety_policy.destructive_tools — per-agent additions saved by the user.
      3. requires_confirmation flag on each ToolInfo — per-tool DB flag.
    """
    result = set(DESTRUCTIVE_TOOLS)

    sp = _parse_safety_policy(agent_rec)
    extra = sp.get("destructive_tools") or []
    if isinstance(extra, list):
        result.update(extra)

    for name, info in tools.items():
        if getattr(info, "requires_confirmation", False):
            result.add(name)

    return frozenset(result)


def _is_auto_confirm(agent_rec: Optional[Dict[str, Any]]) -> bool:
    """Return True when safety_policy.auto_confirm is set — skips the confirmation gate."""
    sp = _parse_safety_policy(agent_rec)
    return bool(sp.get("auto_confirm", False))


def _max_concurrent_tools(agent_rec: Optional[Dict[str, Any]]) -> Optional[int]:
    """Return the maximum number of tools to run in parallel (None = unlimited)."""
    sp = _parse_safety_policy(agent_rec)
    val = sp.get("max_concurrent_tools")
    if val and isinstance(val, int) and val > 0:
        return val
    return None


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


def _stream_stall_seconds() -> float:
    """Max seconds to wait for the *next* streaming chunk before treating the
    LLM stream as stalled.

    A provider can hold the HTTP stream open while emitting nothing (the read
    timeout only fires on a dead socket, not a silent-but-alive one). When that
    happens the per-token heartbeat ``_beat()`` never fires, so the turn would
    hang until the liveness watchdog's frozen threshold (~60s+). Bounding each
    chunk read turns a silent stall into a fast, *resumable* error instead.

    0 disables the guard. Kept below the typical client request timeout."""
    try:
        return float(os.environ.get("AGENT_STREAM_STALL_SECONDS", "45"))
    except (TypeError, ValueError):
        return 45.0


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
            logger.debug("[RACE] Provider %s cancelled — saving partial", prov_name)
            # Save partial results on cancellation
            if save_loser_callback and (local_content or local_in_tok is not None):
                await save_loser_callback(
                    prov_name, model, local_content, local_tool_calls,
                    local_in_tok, local_out_tok, local_cost,
                    int((time.time() - start_time) * 1000),
                    cancelled=True
                )
        except Exception as e:
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


async def _record_billing_usage(
    db: Any,
    agent_id: str,
    user_id: str,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    llm_cost: Optional[float],
    model_name: Optional[str] = None,
    provider_name: Optional[str] = None,
    interaction_id: Optional[str] = None,
) -> Optional[dict]:
    """Compute the per-call charge, write a usage_events row, and debit the
    user's credit wallet when applicable.

    Returns the ChargeResult as a dict for callers that want to emit it to
    the event stream, or None on failure / when billing tables don't exist.
    Silent-failure-friendly: any exception is logged and swallowed so the
    chat continues working even if billing is misconfigured."""
    if not agent_id or not user_id:
        return None
    try:
        agent = await db.get_agent_by_id(agent_id)
        if not agent:
            return None
        provider_cents = int(round((llm_cost or 0) * 100)) if llm_cost else 0
        usage = _billing_pricing.Usage(
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            provider_cost_cents=provider_cents,
            message_count=1,
        )
        result = await _billing_pricing.resolve_charge(agent, user_id, usage, db)

        # Insert usage_events row (always, even free/exempt, for visibility)
        import uuid as _uuid
        try:
            if hasattr(db, "_get_conn"):
                conn = db._get_conn()
                try:
                    conn.execute(
                        "INSERT INTO usage_events ("
                        "id, agent_id, user_id, interaction_id, input_tokens, output_tokens, "
                        "provider_cost_cents, end_user_charge_cents, platform_fee_cents, "
                        "agent_admin_earnings_cents, strategy, is_byo_llm, is_trial, is_exempt, "
                        "model, provider"
                        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            str(_uuid.uuid4()), agent_id, user_id, interaction_id,
                            usage.input_tokens, usage.output_tokens, provider_cents,
                            result.end_user_charge_cents, result.platform_fee_cents,
                            result.agent_admin_earnings_cents, result.strategy,
                            1 if result.is_byo_llm else 0,
                            1 if result.is_trial else 0,
                            1 if result.is_exempt else 0,
                            model_name, provider_name,
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
            elif hasattr(db, "get_raw_client"):
                db.get_raw_client().table("usage_events").insert({
                    "id": str(_uuid.uuid4()),
                    "agent_id": agent_id,
                    "user_id": user_id,
                    "interaction_id": interaction_id,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "provider_cost_cents": provider_cents,
                    "end_user_charge_cents": result.end_user_charge_cents,
                    "platform_fee_cents": result.platform_fee_cents,
                    "agent_admin_earnings_cents": result.agent_admin_earnings_cents,
                    "strategy": result.strategy,
                    "is_byo_llm": 1 if result.is_byo_llm else 0,
                    "is_trial": 1 if result.is_trial else 0,
                    "is_exempt": 1 if result.is_exempt else 0,
                    "model": model_name,
                    "provider": provider_name,
                }).execute()
        except Exception as e:
            logger.debug("usage_events insert skipped: %s", e)

        # Debit the user's wallet for credit-based strategies
        if (
            result.end_user_charge_cents > 0
            and not result.is_exempt
            and not result.is_trial
            and result.strategy in ("credits", "per_message", "per_token")
        ):
            try:
                await _billing_wallet.credit(
                    db,
                    owner_type="user",
                    owner_id=user_id,
                    amount_cents=-result.end_user_charge_cents,
                    kind="usage",
                    ref_id=interaction_id,
                    note=f"agent:{agent_id}",
                )
            except Exception as e:
                logger.debug("wallet debit skipped: %s", e)

        # Decrement trial counters if applicable
        if result.is_trial:
            try:
                if hasattr(db, "_get_conn"):
                    conn = db._get_conn()
                    try:
                        conn.execute(
                            "UPDATE trials SET messages_remaining = "
                            "CASE WHEN messages_remaining IS NULL THEN NULL ELSE messages_remaining - 1 END, "
                            "tokens_remaining = CASE WHEN tokens_remaining IS NULL THEN NULL "
                            "ELSE tokens_remaining - ? END "
                            "WHERE user_id=? AND agent_id=?",
                            (usage.input_tokens + usage.output_tokens, user_id, agent_id),
                        )
                        conn.commit()
                    finally:
                        conn.close()
            except Exception as e:
                logger.debug("trial decrement skipped: %s", e)

        # Credit the agent admin's earnings wallet (informational mirror)
        if result.agent_admin_earnings_cents > 0:
            try:
                roles = await db.get_agent_roles(agent_id)
                admins = roles.get("admin_users") or []
                if admins:
                    await _billing_wallet.credit(
                        db,
                        owner_type="agent_admin",
                        owner_id=admins[0],
                        amount_cents=result.agent_admin_earnings_cents,
                        kind="earnings",
                        ref_id=interaction_id,
                        note=f"agent:{agent_id}",
                    )
            except Exception as e:
                logger.debug("earnings credit skipped: %s", e)

        return {
            "end_user_charge_cents": result.end_user_charge_cents,
            "platform_fee_cents": result.platform_fee_cents,
            "agent_admin_earnings_cents": result.agent_admin_earnings_cents,
            "strategy": result.strategy,
            "is_byo_llm": result.is_byo_llm,
            "is_trial": result.is_trial,
            "is_exempt": result.is_exempt,
        }
    except Exception as e:
        logger.debug("billing usage recording skipped: %s", e)
        return None


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
                        "sure", "do it", "confirm", "go for it", "please do",
                        "continue"]

    if not model_asked:
        return any(kw in last_user_content for kw in confirm_keywords)

    return any(kw in last_user_content for kw in confirm_keywords)


async def stream_agent_events(
    user_id: str,
    session_id: str,
    user_message: Any,
    system_prompt: str,
    agent_id: str,
    history: Optional[List[Dict[str, Any]]] = None,
    parent_interaction_id: Optional[str] = None,
    interrupt_event: Optional[asyncio.Event] = None,
    max_turns: int = 0,
    channel: Optional[str] = None,
    db: Optional[Any] = None,
    agent_template_id: Optional[str] = None,
    allowed_tools: Optional[List[str]] = None,
    loop_config: Optional[LoopConfig] = None,
    execution_mode: str = 'write',
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Run the unified agent loop and yield structured events.
    agent_template_id is used to gate admin-only tools (e.g. 'admin-agent').
    allowed_tools is the list of Tier-2 tool names DISABLED for this agent.
    execution_mode controls tool execution permission:
      'read'  — only read-only tools allowed, all write tools blocked
      'write' — write tools require user confirmation (default guardrail behavior)
      'auto'  — all tools allowed without confirmation
    """
    # Normalize the turn ceiling up front: 0 means UNLIMITED. Guard against a
    # NULL in the DB (which makes agent.get("max_turn_count", 0) return None, not
    # 0), negatives, or a stray string — any of which would otherwise crash the
    # "turn_count < max_turns" comparison or silently refuse to run. Anything
    # that isn't a positive integer collapses to 0 (= unlimited).
    try:
        max_turns = int(max_turns)
        if max_turns < 0:
            max_turns = 0
    except (TypeError, ValueError):
        max_turns = 0

    from app.tools.loader import load_tools
    from app.admin.settings import apply_provider_for_run

    from app.db import get_db
    if db is None:
        db = get_db()

    # Load the agent record first so a per-agent LLM override can be applied.
    agent_name = "Agent"
    _agent_rec: Optional[Dict[str, Any]] = None
    if agent_id:
        _agent_rec = await db.get_agent_by_id(agent_id)
        if _agent_rec and _agent_rec.get("name"):
            agent_name = _agent_rec["name"]

    # Apply the effective provider config for THIS run (not shared with any other
    # user): the user's default with any per-agent LLM override (custom model)
    # layered on top, so the agent runs on ITS configured model — not just the
    # global default.
    await apply_provider_for_run(user_id, _agent_rec)

    model_name = os.environ.get("LLM_MODEL") or os.environ.get("OPENROUTER_MODEL") or "deepseek/deepseek-v4-flash"
    provider_name = os.environ.get("LLM_PROVIDER", "openrouter")

    load_start = time.time()
    tools = await load_tools(user_id, agent_id=agent_id, agent_template_id=agent_template_id,
                              allowed_tools=allowed_tools, session_id=session_id)
    load_duration = int((time.time() - load_start) * 1000)

    # ── Pipeline: tools loaded ──
    yield {"type": "pipeline", "level": "pipeline",
           "step": "load_tools", "count": len(tools),
           "names": list(tools.keys()),
           "duration_ms": load_duration}

    # ── Integration status: inject available OAuth integrations into system prompt ──
    _OAUTH_PROVIDER_TYPES = {"google", "microsoft", "yahoo", "dropbox", "meta",
                              "twitter", "linkedin", "tiktok", "pinterest",
                              "reddit", "snapchat", "twitch",
                              "scraper", "browser_session"}
    _int_summary: list = []
    if agent_id:
        try:
            from app.admin.integrations import get_admin_configured_providers as _gcp
            _configured = await _gcp(user_id)
            _int_rows = await db.get_agent_connections(agent_id)
            _enabled_oauth = [r for r in _int_rows
                              if r.get("enabled")
                              and r.get("connection_type") in _OAUTH_PROVIDER_TYPES
                              and r.get("connection_type") in _configured]
            for _r in _enabled_oauth:
                _ct = _r["connection_type"]
                # Generic, non-OAuth providers live in different auth_elements rows.
                if _ct == "scraper":
                    try:
                        from app.admin.integrations import get_scraper_creds as _gsc
                        _scfg = await _gsc()
                    except Exception:
                        _scfg = None
                    if _scfg:
                        _int_summary.append({"provider": _ct, "connected": True,
                                             "account": _scfg.get("provider", "")})
                    else:
                        _int_summary.append({"provider": _ct, "connected": False})
                    continue
                if _ct == "browser_session":
                    try:
                        from app.admin.integrations import get_browser_session_creds as _gbs
                        _bsess = await _gbs(user_id)
                    except Exception:
                        _bsess = None
                    if _bsess:
                        _int_summary.append({"provider": _ct, "connected": True,
                                             "account": _bsess.get("domain", "")})
                    else:
                        _int_summary.append({"provider": _ct, "connected": False})
                    continue
                try:
                    from app.integrations.oauth_helper import oauth_label as _oauth_label
                    _elem = await db.auth_element_get(user_id, _ct, _oauth_label(agent_id))
                except Exception:
                    _elem = None
                if _elem and _elem.get("secret_ref"):
                    import json as _jmod
                    _cfg = _elem.get("config") or {}
                    if isinstance(_cfg, str):
                        try:
                            _cfg = _jmod.loads(_cfg)
                        except Exception:
                            _cfg = {}
                    _email = _cfg.get("email") or _cfg.get("name") or ""
                    _int_summary.append({
                        "provider": _ct,
                        "connected": True,
                        "account": _email,
                    })
                else:
                    _int_summary.append({
                        "provider": _ct,
                        "connected": False,
                    })
        except Exception:
            pass

    # Curated tool hints — surface to the LLM what it can DO once an integration is connected.
    _PROVIDER_TOOL_HINTS = {
        "google":    "gmail_list_messages, gmail_get_message, gmail_send, gcal_list_events, gcal_create_event, drive_list_files, drive_get_file",
        "microsoft": "outlook_list_messages, outlook_get_message, outlook_send, outlook_calendar_list_events, outlook_calendar_create_event, onedrive_list_files, onedrive_search, onedrive_get_file",
        "yahoo":     "yahoo_userinfo (REST Mail API not available — mail requires IMAP/SMTP)",
        "dropbox":   "dropbox_list_files, dropbox_search, dropbox_download",
        "twitter":   "twitter_me, twitter_post_tweet, twitter_list_my_tweets",
        "linkedin":  "linkedin_me, linkedin_post",
        "meta":      "facebook_me, facebook_list_pages, facebook_post_to_page, instagram_list_accounts, instagram_recent_media",
        "reddit":    "reddit_me, reddit_listing, reddit_submit, reddit_comment",
        "pinterest": "pinterest_list_boards, pinterest_list_pins, pinterest_create_pin",
        "snapchat":  "snapchat_userinfo (Snap Kit limited to identity)",
        "tiktok":    "tiktok_userinfo, tiktok_list_videos",
        "twitch":    "twitch_me, twitch_get_streams, twitch_followed_channels",
        "scraper":   "web_scrape_search (query the configured scraper), web_scrape_url (fetch one URL through the scraper)",
        "browser_session": "web_session_status, web_session_fetch, web_session_graphql (HTTP requests using the user's stored cookies — for sites the user is already logged into)",
    }
    if _int_summary:
        _int_lines = []
        for _s in _int_summary:
            if _s["connected"]:
                _acc = f' as {_s["account"]}' if _s.get("account") else ""
                _hint = _PROVIDER_TOOL_HINTS.get(_s["provider"])
                _tools_str = f' Use: {_hint}.' if _hint else \
                             f' Use oauth_api_call(provider="{_s["provider"]}", ...) for arbitrary endpoints.'
                _int_lines.append(f'- {_s["provider"].title()}: connected{_acc}.{_tools_str}')
            else:
                _int_lines.append(
                    f'- {_s["provider"].title()}: not connected'
                    f' — call check_oauth_connection("{_s["provider"]}") to get a connect link for the user'
                )
        system_prompt = (system_prompt or "") + "\n\n## Available Integrations\n" + "\n".join(_int_lines)

        # If a generic web tool is connected, also inject the site-recipe
        # fragment so the agent knows the common URL templates / GraphQL
        # doc-ids without us hardcoding them in the integration itself.
        _has_generic_web = any(
            s.get("connected") and s.get("provider") in ("scraper", "browser_session")
            for s in _int_summary
        )
        if _has_generic_web:
            try:
                from app.db.system_prompt_fragments import get_prompt_fragments as _gpf
                _recipes = (_gpf().get("web_automation_recipes") or "").strip()
            except Exception:
                _recipes = ""
            if _recipes:
                system_prompt = (system_prompt or "") + "\n\n## Web automation recipes\n" + _recipes

    yield {"type": "pipeline", "level": "pipeline",
           "step": "integration_status",
           "count": len(_int_summary),
           "integrations": _int_summary}

    # ── Tool exposure index (# [TOOLS]) ──────────────────────────────────────
    # Generate the tool catalog from the ACTUAL loaded tool set: tools sent in
    # full this turn ("ready to use") and tools whose schema is withheld until
    # load_tool pulls them in ("load on demand"). This replaces the hand-written
    # bootstrap-tool prompt slots (which could name tools the agent didn't have).
    # `_agent_tool_modes` is stable for the run; the active set grows as the
    # model loads discoverable tools.
    _agent_tool_modes: Dict[str, str] = {}
    _active_tool_names: List[str] = []
    try:
        from app.tools.tool_modes import resolve_mode as _tm_resolve, render_index as _tm_render
        if agent_id:
            _agent_tool_modes = await db.get_agent_tool_modes(agent_id)
        if session_id:
            _active_tool_names = await db.get_session_active_tools(session_id)
        _active_set = set(_active_tool_names)
        _idx_entries = []
        for _tn, _ti in tools.items():
            _d = (_ti.handler.__doc__ or "").strip() if hasattr(_ti, "handler") else ""
            _idx_entries.append({
                "name": _tn,
                "desc": _d,
                "mode": _tm_resolve(_tn, _agent_tool_modes),
                "active": _tn in _active_set,
            })
        _tools_index = _tm_render(_idx_entries)
        if _tools_index:
            system_prompt = (system_prompt or "") + "\n\n" + _tools_index
    except Exception as _tie:
        logger.warning("tools index build failed: %s", _tie)

    # Build message list
    messages: List[Dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    # ── Context Control: surface the live context-fill signal to the agent ──────
    # If the agent has the context_control ability enabled, estimate how full the
    # assembled context is against its configured token limit (default 200K) and
    # inject a one-block gauge into the system message so the agent can feel itself
    # filling up. Also emit a pipeline event so the UI can show the same number.
    # Disabled ability → this is a no-op. Never let a failure here break the run.
    try:
        from app.agent.context_control import (
            get_context_settings as _cc_settings,
            estimate_tokens as _cc_estimate,
            status_line as _cc_status_line,
            context_pct as _cc_pct,
        )
        _cc = await _cc_settings(db, agent_id)
        if _cc["enabled"]:
            _cc_tokens = _cc_estimate(messages)
            _cc_limit = _cc["token_limit"]
            _cc_line = _cc_status_line(_cc_tokens, _cc_limit)
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] = (messages[0].get("content") or "") + "\n\n" + _cc_line
            else:
                messages.insert(0, {"role": "system", "content": _cc_line})
            yield {"type": "pipeline", "level": "pipeline", "step": "context_status",
                   "tokens": _cc_tokens, "limit": _cc_limit,
                   "pct": _cc_pct(_cc_tokens, _cc_limit), "enabled": True}
    except Exception as _cce:
        logger.warning("context_control signal skipped: %s", _cce)

    turn_count = 0
    original_max_turns = max_turns  # the configured block size; used to rearm at each ceiling
    last_extension_at = 0           # ceiling turn at which we last extended (0 = not yet)
    empty_retry_used = False        # safety net: one retry per session for an empty LLM reply
    _tool_name_streak = 0        # consecutive same-tool-name calls (diff args)
    _last_tool_streak_name = ""  # which tool the streak is counting

    try:
        # Build loop config — drives per-node enable/disable at runtime.
        # If caller supplied one (e.g. tests), use it directly; otherwise
        # parse the agent's stored loop_logic (backward-compat: flat array
        # = all nodes enabled, preserving current behavior exactly).
        if loop_config is None:
            loop_config = LoopConfig.from_agent(_agent_rec)

        # Build the effective destructive-tool set from the agent's safety_policy
        # (merged with the hardcoded baseline and per-tool requires_confirmation flags).
        # This is computed once per session after tools are loaded.
        effective_destructive = _build_effective_destructive_set(_agent_rec, tools)
        auto_confirm = _is_auto_confirm(_agent_rec)
        concurrent_limit = _max_concurrent_tools(_agent_rec)

        def prefix_content(content: str) -> str:
            return content

        # Use list to track state across function boundaries
        first_stream_chunk_state = [True]  # [is_first_chunk]

        # ── Stall / runaway guard ────────────────────────────────────────────
        # Safety nets so an agent can't go "rogue": loop on the same tool call,
        # or run until the request times out. They only trip on pathological
        # behavior, never on normal multi-step work. All thresholds env-tunable.
        import collections as _collections
        _loop_start_ts = time.time()
        _tool_call_counts = _collections.Counter()   # signature -> times seen
        stall_strikes = 0
        stall_stop_msg = None     # when set, break out of the loop and finalize
        input_tokens = output_tokens = llm_cost = None   # pre-init for finalize
        # ── Streaming-answer persistence ──
        # The assistant row for the current step is created in the DB as soon as
        # the first token arrives (status='streaming') and updated as more text
        # streams in, so the partial answer is durable: any device can render it
        # from a plain DB read, and it survives RunBuffer eviction / restart.
        # Reset to None at the start of each step (each LLM call).
        streaming_asst_id = None
        collected_content = ""       # pre-init so except handlers are safe
        _last_stream_persist = 0.0   # monotonic ts of last throttled DB write
        try:
            _STREAM_PERSIST_INTERVAL = float(os.environ.get("AGENT_STREAM_PERSIST_INTERVAL", "0.6"))
        except (ValueError, TypeError):
            _STREAM_PERSIST_INTERVAL = 0.6
        # Global override from app-settings.json (supersedes env vars, subsumed by per-agent).
        # Stored as a single dict to avoid cell-variable scoping issues on Python 3.14+.
        _gs = None
        try:
            from app.admin.settings import _load_app_settings as _get_gs
            _gs = _get_gs()
        except Exception:
            pass
        # Per-agent identical-tool-calls limit (0 = disabled/infinite).
        # Falls back to global app-settings > AGENT_MAX_IDENTICAL_TOOL_CALLS env var, then 0 (off).
        _raw_identical = _agent_rec.get("max_identical_tool_calls", 0) if _agent_rec else 0
        if _raw_identical is not None and int(_raw_identical) > 0:
            _MAX_IDENTICAL_CALLS = max(2, int(_raw_identical))
        elif _gs and _gs.get("max_identical_tool_calls") and int(_gs["max_identical_tool_calls"]) > 0:
            _MAX_IDENTICAL_CALLS = max(2, int(_gs["max_identical_tool_calls"]))
        else:
            try:
                _MAX_IDENTICAL_CALLS = int(os.environ.get("AGENT_MAX_IDENTICAL_TOOL_CALLS", "0"))
            except (ValueError, TypeError):
                _MAX_IDENTICAL_CALLS = 0
        # Per-agent stall-strikes limit (0 = disabled/infinite).
        # Falls back to AGENT_MAX_STALL_STRIKES env var, then 0 (off).
        _raw_stall = _agent_rec.get("max_stall_strikes", 0) if _agent_rec else 0
        if _raw_stall is not None and int(_raw_stall) > 0:
            _MAX_STALL_STRIKES = max(1, int(_raw_stall))
        else:
            try:
                _MAX_STALL_STRIKES = int(os.environ.get("AGENT_MAX_STALL_STRIKES", "0"))
            except (ValueError, TypeError):
                _MAX_STALL_STRIKES = 0

        try:
            _MAX_WALL_SECONDS = float(os.environ.get("AGENT_MAX_WALL_SECONDS", str(DEFAULT_MAX_WALL_SECONDS)))
        except (ValueError, TypeError):
            _MAX_WALL_SECONDS = DEFAULT_MAX_WALL_SECONDS
        # Global app-settings override
        if _gs and _gs.get("max_wall_seconds") and _gs["max_wall_seconds"] > 0:
            _MAX_WALL_SECONDS = float(_gs["max_wall_seconds"])
        # Per-agent override: if the agent record has max_wall_seconds set, it wins over global.
        # A value of 0 or None means "no limit" (opt out of the cap entirely).
        if _agent_rec and _agent_rec.get("max_wall_seconds") is not None:
            try:
                _MAX_WALL_SECONDS = float(_agent_rec["max_wall_seconds"])
            except (ValueError, TypeError):
                pass
        elif _agent_rec and _agent_rec.get("max_wall_seconds") is None:
            # Explicitly None on the agent record means opt out — disable the cap.
            _MAX_WALL_SECONDS = 0.0
        # Liveness heartbeat cadence — how often the loop proves it is alive by
        # advancing session_runs.heartbeat_at. The watchdog's "frozen" threshold
        # must be several multiples of this. Best-effort + throttled.
        try:
            _HEARTBEAT_INTERVAL = float(os.environ.get("AGENT_RUN_HEARTBEAT_INTERVAL", "5"))
        except (ValueError, TypeError):
            _HEARTBEAT_INTERVAL = 5.0
        _last_heartbeat = 0.0

        async def _beat() -> None:
            nonlocal _last_heartbeat
            _hb_now = time.monotonic()
            if (_hb_now - _last_heartbeat) >= _HEARTBEAT_INTERVAL:
                _last_heartbeat = _hb_now
                try:
                    await db.run_state_heartbeat(session_id)
                except Exception:
                    pass

        while max_turns == 0 or turn_count < max_turns:
            # Wall-clock safety cap — finish gracefully instead of timing out.
            # (>0 guard: 0/negative disables it and can't trip on turn 1.)
            if _MAX_WALL_SECONDS > 0 and (time.time() - _loop_start_ts) > _MAX_WALL_SECONDS:
                yield {"type": "pipeline", "level": "pipeline",
                       "step": "stall_guard_stop", "reason": "wall_clock",
                       "elapsed_s": round(time.time() - _loop_start_ts, 1),
                       "turn": turn_count}
                stall_stop_msg = (
                    "I stopped because this task hit the safety time limit — I didn't "
                    "want to leave you waiting on a request that may be stuck. Here's "
                    "where I got to; tell me how you'd like to proceed, or try breaking "
                    "it into smaller steps."
                )
                break
            if loop_config.is_enabled("interrupt_chk"):
                await _check_interrupt(session_id, interrupt_event)

            # Prove liveness between turns (covers long tool executions).
            await _beat()

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
            if max_turns > 0 and loop_config.is_enabled("permission_chk") and turn_count == max_turns and last_extension_at != max_turns:
                fr = get_prompt_fragments()
                permission_message = (fr.get("turn_permission_request") or "").strip()
                if permission_message:
                    messages.append({"role": "system", "content": permission_message})

            # Check if user has granted permission (looks at their most recent message).
            # Only active at the current ceiling, before we have already extended at that ceiling.
            if max_turns > 0 and loop_config.is_enabled("permission_chk") and turn_count >= max_turns and last_extension_at < max_turns:
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

            # Build tool definitions from loaded tools — only the schemas the
            # model may call THIS turn: core/always tools, plus any discoverable
            # tool it has already loaded. Re-read the session's active list each
            # turn so a tool loaded via load_tool last turn now gets its full
            # schema sent (modes are stable for the run; the active set grows).
            from app.tools.tool_modes import is_sent as _tm_is_sent
            if session_id:
                try:
                    _active_set_now = set(await db.get_session_active_tools(session_id))
                except Exception:
                    _active_set_now = set(_active_tool_names)
            else:
                _active_set_now = set(_active_tool_names)
            tool_definitions = []
            for name, info in tools.items():
                if not _tm_is_sent(name, _agent_tool_modes, _active_set_now):
                    continue
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

            async def _persist_stream_progress(force: bool = False) -> None:
                """Create the in-progress assistant row on first token, then
                throttle-update its content as the answer streams. Makes the
                partial answer durable in the DB. ``force`` bypasses the throttle
                (used at step finalize)."""
                nonlocal streaming_asst_id, _last_stream_persist
                # Mid-stream interrupt: lets the Stop button halt a long single
                # completion, not just at turn boundaries. Throttled to the
                # persist cadence so it costs ~one extra flag read per interval.
                if loop_config.is_enabled("interrupt_chk") and not force:
                    now_i = time.monotonic()
                    if streaming_asst_id is None or (now_i - _last_stream_persist) >= _STREAM_PERSIST_INTERVAL:
                        await _check_interrupt(session_id, interrupt_event)
                if streaming_asst_id is None:
                    try:
                        streaming_asst_id = await db.insert_interaction(
                            user_id, session_id, role="assistant",
                            content=collected_content,
                            parent_id=parent_interaction_id,
                            channel=channel,
                            metadata=_build_meta("assistant", input_tokens, output_tokens, llm_cost),
                            sender_id=agent_id, receiver_id=user_id,
                            status="streaming",
                        )
                        try:
                            await db.run_state_set_assistant(session_id, streaming_asst_id)
                        except Exception:
                            pass
                    except Exception as _se:
                        logger.debug("stream persist (create) failed: %s", _se)
                    _last_stream_persist = time.monotonic()
                    return
                now = time.monotonic()
                if not force and (now - _last_stream_persist) < _STREAM_PERSIST_INTERVAL:
                    return
                _last_stream_persist = now
                try:
                    await db.update_interaction(streaming_asst_id, content=collected_content)
                except Exception as _se:
                    logger.debug("stream persist (update) failed: %s", _se)
                # Keep the liveness heartbeat fresh during a long single-turn stream.
                await _beat()

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
                except Exception as e:
                    logger.warning("Failed to save parallel loser response: %s", e)
                except BaseException as be:
                    logger.debug("Parallel loser DB insert interrupted: %s", be)

            # ── Check for parallel multi-provider mode ──
            _parallel_mode = os.environ.get("PARALLEL_MODE", "").lower() == "true"
            _multi_providers_raw = os.environ.get("MULTI_PROVIDERS", "")

            collected_content = ""
            collected_tool_calls: Dict[int, Any] = {}
            streaming_asst_id = None   # new in-progress assistant row per step
            _last_stream_persist = 0.0
            input_tokens = None
            output_tokens = None
            llm_cost = None
            _used_parallel = False

            if _parallel_mode and _multi_providers_raw:
                try:
                    _multi_providers_list = json.loads(_multi_providers_raw)
                except (json.JSONDecodeError, TypeError):
                    _multi_providers_list = []

                # ── Attachment Description: when an image is inlined in the
                #    messages, only let image-capable providers race — a text-only
                #    racer would answer blind or error. chat.py already described
                #    the image when NO selected model could see it, so an inlined
                #    image here implies at least one image-capable racer exists. ──
                def _msgs_have_image(msgs):
                    for _m in msgs:
                        _c = _m.get("content")
                        if isinstance(_c, list) and any(
                            isinstance(_p, dict) and _p.get("type") == "image_url" for _p in _c
                        ):
                            return True
                    return False

                _has_image = _msgs_have_image(messages)
                if _has_image:
                    _multi_providers_list = [p for p in _multi_providers_list if p.get("image_capable")]
                    if not _multi_providers_list:
                        logger.warning(
                            "Parallel race: image present but no image-capable provider "
                            "configured; falling back to single-provider path"
                        )

                # A race normally needs >=2 providers; with an inlined image a
                # single image-capable provider still uses the race path (so that
                # model is used, not the env-default single model).
                _min_racers = 1 if _has_image else 2
                if len(_multi_providers_list) >= _min_racers:
                    _used_parallel = True
                    _parallel_had_error = False

                    async for _pe in _race_llm_calls(
                        messages, tool_definitions, _multi_providers_list, user_id=user_id, save_loser_callback=_save_loser
                    ):
                        if _pe["type"] == "stream":
                            collected_content += _pe["content"]
                            await _persist_stream_progress()
                            # Only yield non-empty content — same as single-provider path
                            if not _pe["content"].strip():
                                continue
                            _pe = dict(_pe)  # shallow copy to avoid mutating original
                            # Prefix only the first stream chunk of the turn
                            if first_stream_chunk_state[0]:
                                _pe["content"] = prefix_content(_pe["content"])
                                first_stream_chunk_state[0] = False
                            _pe["asst_id"] = streaming_asst_id
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

                # Per-chunk inactivity guard: if the provider keeps the stream
                # open but stops emitting tokens, _beat() never fires and the
                # turn would hang until the watchdog's frozen threshold (~60s+).
                # Bounding each read makes a silent stall raise here, where the
                # outer handler records status='error' / stop_cause='crash' — a
                # resumable cause the self-healing layer re-ignites in seconds.
                _stall_s = _stream_stall_seconds()
                _stream_iter = stream.__aiter__()
                while True:
                    try:
                        if _stall_s > 0:
                            chunk = await asyncio.wait_for(
                                _stream_iter.__anext__(), timeout=_stall_s)
                        else:
                            chunk = await _stream_iter.__anext__()
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        try:
                            await stream.close()
                        except Exception:
                            pass
                        # Durable, structured signal on the diagnostics page so
                        # stalls can be counted/correlated. The raise below alone
                        # would only leave a generic 'turn failed' server error.
                        try:
                            from app.agent.diagnostics import record as _diag
                            _diag("warning", "recovery",
                                  f"LLM stream stalled — no token for {_stall_s:.0f}s",
                                  source="stream_stall",
                                  detail={"model": model_name,
                                          "stall_seconds": _stall_s,
                                          "chars_streamed": len(collected_content),
                                          "turn": turn_count},
                                  session_id=session_id, agent_id=agent_id,
                                  user_id=user_id)
                        except Exception:
                            pass
                        raise RuntimeError(
                            f"LLM stream stalled — no token for {_stall_s:.0f}s "
                            f"(model={model_name}); aborting so the run can recover"
                        )

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
                        # Persist FIRST so the per-step assistant row exists and
                        # streaming_asst_id is set, then tag the stream event with
                        # it so the UI can render each step as its own bubble.
                        await _persist_stream_progress()
                        stream_content = delta.content
                        if first_stream_chunk_state[0]:
                            stream_content = prefix_content(stream_content)
                            first_stream_chunk_state[0] = False
                        # Only yield non-empty content — empty/whitespace chunks
                        # waste a bubble and inflate turn counts.
                        if stream_content.strip():
                            yield {"type": "stream", "level": "agent", "content": stream_content, "asst_id": streaming_asst_id}

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

            # ── Billing: record usage + charge wallet (best-effort, never blocks chat) ──
            _billing_event = await _record_billing_usage(
                db, agent_id, user_id, input_tokens, output_tokens, llm_cost,
                model_name=model_name, provider_name=provider_name,
                interaction_id=parent_interaction_id,
            )
            if _billing_event:
                yield {"type": "billing", "level": "billing", **_billing_event}

            # ── Tool-call limit per turn ──
            # Cap the number of tool calls the LLM can make in one turn.
            # 0 = unlimited. Controlled via app-settings.json max_tool_calls.
            if _gs and _gs.get("max_tool_calls") and _gs["max_tool_calls"] > 0 and len(collected_tool_calls) > _gs["max_tool_calls"]:
                extra_count = len(collected_tool_calls) - _gs["max_tool_calls"]
                # Keep only the first _gs_max_tool tool calls (by index order).
                kept = dict(list(sorted(collected_tool_calls.items()))[:_gs["max_tool_calls"]])
                dropped = len(collected_tool_calls) - len(kept)
                collected_tool_calls = kept
                yield {"type": "pipeline", "level": "pipeline",
                       "step": "tool_call_limit", "capped": True,
                       "allowed": _gs["max_tool_calls"], "dropped": dropped}

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

                # Strip <think>...</think> before replaying to the LLM.
                # Reasoning-model providers (e.g. Gemini 3.1 Pro via DeepInfra)
                # return empty on the next turn when they see their own prior
                # think-block, which falls through to the "no tool calls" branch
                # and ends the loop after one productive turn.
                from app.agent.session_history import strip_think_blocks
                replay_content = strip_think_blocks(collected_content) or None
                # When the LLM returns tool calls with empty/whitespace content,
                # set content to None so session_history skips it on replay and
                # the empty bubble is never rendered in the UI.
                if replay_content is not None and not replay_content.strip():
                    replay_content = None
                messages.append({
                    "role": "assistant",
                    "content": replay_content,
                    "tool_calls": full_tool_calls,
                })

                # Persist intermediate assistant message — clean content, no tool-call echo
                assistant_content = (collected_content or "").strip()
                # Tool calls are stored in the `output` field (line 1247 below),
                # NOT embedded in the content string. The legacy `\n\n[Tool calls: ...]`
                # suffix was removed because it contaminated message history: the LLM
                # would see its own tool calls echoed as text in the next turn, causing
                # it to write tool calls as text instead of making structured calls.
                # session_history.py reads tool calls from the `output` field instead.
                meta_asst = _build_meta("assistant", input_tokens, output_tokens, llm_cost)
                inp = _build_input()
                outp = json.dumps({"role": "assistant", "content": collected_content, "tool_calls": full_tool_calls})
                db_start = time.time()
                if streaming_asst_id is not None:
                    # Finalize the in-progress row created while streaming.
                    await db.update_interaction(
                        streaming_asst_id, content=assistant_content,
                        status="complete", output_data=outp, metadata=meta_asst,
                    )
                    asst_id = streaming_asst_id
                else:
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
                # This step's assistant message is finalized; clear the handle so
                # an interrupt during tool execution doesn't re-finalize it.
                streaming_asst_id = None
                yield {"type": "db", "level": "db",
                       "op": "insert_interaction", "role": "assistant",
                       "tool_name": None, "id": asst_id, "ms": db_dur}
                # Finalize THIS step's bubble in the UI (it's an intermediate
                # assistant message that precedes tool calls — shown as its own
                # bubble so the user sees every step, not just the final answer).
                # Skip if content is empty — no bubble to render.
                if assistant_content.strip():
                    yield {"type": "agent_step_end", "level": "agent",
                           "asst_id": asst_id, "content": assistant_content}

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

                    # ── Visualizer: populate render_visual html from assistant text ──
                    # DeepSeek and similar models often put the HTML content in their
                    # text response instead of the tool call's html parameter. When the
                    # html parameter is empty/blank, use the assistant's text content.
                    if tool_name == "render_visual" and collected_content:
                        html_raw = tool_args.get("html", "")
                        if not html_raw or not html_raw.strip():
                            tool_args["html"] = collected_content

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
                        # ── Stall guard: identical-call loop detection ──────────
                        # If the agent calls the SAME tool with the SAME arguments
                        # too many times, it is looping (e.g. run_worker_trials x6).
                        # Block the repeat, tell it to change approach, count a
                        # strike, and move on without executing.
                        # 0 = disabled (infinite).
                        _sig = f"{tool_name}|{tc.function.arguments or ''}"
                        _tool_call_counts[_sig] += 1

                        # ── Stall guard: same-tool-name streak (different args) ──
                        # Catches e.g. 19 consecutive run_python or 12 consecutive
                        # read_source with different paths. The agent is thrashing.
                        # 0 = disabled (infinite).
                        if tool_name == _last_tool_streak_name:
                            _tool_name_streak += 1
                        else:
                            _tool_name_streak = 1
                            _last_tool_streak_name = tool_name

                        _hit_identical = _MAX_IDENTICAL_CALLS > 0 and _tool_call_counts[_sig] >= _MAX_IDENTICAL_CALLS
                        _hit_streak = _MAX_IDENTICAL_CALLS > 0 and _tool_name_streak >= _MAX_IDENTICAL_CALLS

                        if _hit_identical or _hit_streak:
                            stall_strikes += 1
                            if _hit_identical:
                                _loop_warn = (
                                    f"Loop guard: you have called `{tool_name}` with identical "
                                    f"arguments {_tool_call_counts[_sig]} times. That is not making "
                                    f"progress, so I did not run it again. Do NOT repeat the same "
                                    f"call — take a different approach, use a different tool, or "
                                    f"stop and give the user your best answer or a clarifying question."
                                )
                            else:
                                _loop_warn = (
                                    f"Loop guard: you have called `{tool_name}` for "
                                    f"{_tool_name_streak} consecutive turns with different arguments. "
                                    f"That is not making progress, so I did not run it again. "
                                    f"Do NOT call this tool again — take a different approach, use "
                                    f"a different tool, or stop and give the user your best answer "
                                    f"or a clarifying question."
                                )
                            yield {"type": "pipeline", "level": "pipeline",
                                   "step": "stall_guard_loop", "tool": tool_name,
                                   "count": _tool_call_counts[_sig], "strikes": stall_strikes}
                            yield {
                                "type": "tool_result", "level": "agent", "tool": tool_name,
                                "result": json.dumps({"status": "loop_blocked", "message": _loop_warn}),
                                "duration_ms": 0, "error": True,
                                "error_type": "loop_blocked", "recoverable": True,
                            }
                            tool_msg = {"role": "tool", "content": _loop_warn, "tool_call_id": tc.id}
                            messages.append(tool_msg)
                            inp = _build_input()
                            outp = json.dumps({"role": "tool", "content": _loop_warn, "tool_call_id": tc.id, "name": tool_name, "success": False})
                            inter_id = await db.insert_interaction(
                                user_id, session_id, role="tool", content=_loop_warn,
                                parent_id=asst_id, tool_call_id=tc.id, tool_name=tool_name,
                                channel=channel,
                                metadata=json.dumps({"success": False, "duration_ms": 0, "input_params": tool_args, "error_message": "loop_blocked"}),
                                input_data=inp, output_data=outp,
                                sender_id=agent_id, receiver_id=agent_id,
                            )
                            yield {"type": "db", "level": "db",
                                   "op": "insert_interaction", "role": "tool",
                                   "tool_name": tool_name, "id": inter_id, "ms": 0}
                            continue

                        # ── Guardrail: execution mode + confirmation for destructive tools ──
                        # effective_destructive merges the hardcoded baseline with the
                        # agent's safety_policy.destructive_tools and per-tool flags.
                        # auto_confirm skips the gate (useful for automation agents).
                        # execution_mode controls the overall policy:
                        #   'read' — block ALL destructive tools unconditionally
                        #   'write' — require user confirmation for destructive tools
                        #   'auto' — allow all, no gate
                        # In 'read' mode, check the tool's own destructive flag (from metadata)
                        # rather than just effective_destructive, so write_source/edit_source
                        # etc. are blocked even though they're not in DESTRUCTIVE_TOOLS.
                        ti = tools.get(tool_name)
                        is_destructive = tool_name in effective_destructive
                        if execution_mode == 'read':
                            # Block if the tool is flagged destructive in its metadata
                            gate_required = bool(ti and ti.destructive) or is_destructive
                        elif execution_mode == 'auto':
                            gate_required = False
                        else:  # 'write' (default, formerly 'ask')
                            gate_required = (
                                loop_config.is_enabled("guardrails")
                                and is_destructive
                                and not auto_confirm
                            )
                        # Per-arg exemption: read-only shell commands via run_command
                        # (git status, ls, cat, ...) bypass the confirmation gate.
                        if gate_required and tool_name == "run_command" and _is_safe_shell_command(tool_args.get("command", "")):
                            gate_required = False
                            yield {"type": "pipeline", "level": "pipeline",
                                   "step": "guardrail_skip", "tool": tool_name,
                                   "status": "safe_read_only",
                                   "command": str(tool_args.get("command", ""))[:120],
                                   "message": "Read-only shell command — confirmation skipped"}
                        if gate_required:
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
                                tool_msg = {"role": "tool", "content": f"Tool '{tool_name}' was blocked — it requires your explicit permission to run. If this is the only way to complete the task, please authorize it. Otherwise, the agent can explore alternative approaches that don't need confirmation.", "tool_call_id": tc.id}
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
                        # data_src_exec light-up: detect connector-generated tools.
                        _ti = tools.get(tool_name)
                        _tid = getattr(_ti, "tool_id", "") or ""
                        if _tid.startswith("ds:"):
                            yield {"type": "pipeline", "level": "pipeline",
                                   "step": "data_src_query_started",
                                   "tool": tool_name, "tool_id": _tid}

                    if concurrent_limit and len(valid_calls) > 1:
                        # Throttle parallel execution to safety_policy.max_concurrent_tools
                        sem = asyncio.Semaphore(concurrent_limit)
                        async def _throttled(name, args, tc_id):
                            async with sem:
                                return await execute_one(name, args, tc_id)
                        tasks = [_throttled(name, args, tc.id) for _, tc, name, args in valid_calls]
                    else:
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
                        # Light up the skill_track node when an on-demand skill loads.
                        if tool_name == "load_skill" and success:
                            yield {"type": "pipeline", "level": "pipeline",
                                   "step": "skill_track", "tool": tool_name,
                                   "skill": (tool_args or {}).get("name")}
                        _ti2 = tools.get(tool_name)
                        _tid2 = getattr(_ti2, "tool_id", "") or ""
                        if _tid2.startswith("ds:"):
                            yield {"type": "pipeline", "level": "pipeline",
                                   "step": "data_src_query_finished",
                                   "tool": tool_name, "tool_id": _tid2,
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
                                tools = await _load_tools(user_id, agent_id=agent_id, agent_template_id=_tpl_id)

                                # Inject new agent's resolved prompts as a system message
                                try:
                                    _new_sp = (await db.assemble_prompt(_ag_id, user_id) or "").strip() if hasattr(db, "assemble_prompt") else ""
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

                # ── Stall guard: too many loop strikes → stop cleanly ──
                # 0 = disabled (infinite).
                if _MAX_STALL_STRIKES > 0 and stall_strikes >= _MAX_STALL_STRIKES:
                    yield {"type": "pipeline", "level": "pipeline",
                           "step": "stall_guard_stop", "reason": "repeated_loops",
                           "strikes": stall_strikes, "turn": turn_count}
                    stall_stop_msg = (
                        "I stopped because I kept repeating the same step without making "
                        "progress and I didn't want to spin in a loop. Could you clarify "
                        "what you'd like, or point me at the specific file or area to change?"
                    )
                    break

                yield {"type": "pipeline", "level": "pipeline",
                       "step": "check_continue", "turn": turn_count,
                       "max_turns": max_turns, "will_continue": turn_count < max_turns}
                continue

            # ── Empty-response safety net ──
            # If the LLM returned nothing (no content + no tool calls), don't
            # treat it as the final answer — it's almost always a transient
            # provider hiccup. Nudge once and try again. Only retry on turns
            # after the first; an empty first-turn reply is honored as-is.
            _empty_reply = not (collected_content or "").strip()
            if _empty_reply and turn_count > 1 and not empty_retry_used:
                yield {"type": "pipeline", "level": "pipeline",
                       "step": "empty_response_retry", "turn": turn_count,
                       "message": "LLM returned empty content + no tool calls; retrying once."}
                messages.append({
                    "role": "system",
                    "content": "Your previous response was empty. Continue the task: either call the next tool, or write the final answer for the user.",
                })
                empty_retry_used = True
                turn_count -= 1  # Don't burn a turn on the retry
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
            if streaming_asst_id is not None:
                # Finalize the in-progress row created while streaming.
                await db.update_interaction(
                    streaming_asst_id, content=collected_content,
                    status="complete", output_data=outp, metadata=meta_final,
                )
                inter_id = streaming_asst_id
            else:
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

            yield {"type": "response", "level": "agent",
                   "content": prefix_content(collected_content), "asst_id": inter_id}
            if loop_config.is_enabled("fire_optimizer"):
                _fire_optimizer(user_id, session_id, channel, agent_template_id)
            return

        # ── Stall guard stop — finalize cleanly (skip the max-turns message) ──
        if stall_stop_msg is not None:
            messages.append({"role": "assistant", "content": stall_stop_msg})
            meta_final = _build_meta("assistant", input_tokens, output_tokens, llm_cost)
            inp = _build_input()
            outp = json.dumps({"role": "assistant", "content": stall_stop_msg})
            db_start = time.time()
            inter_id = await db.insert_interaction(
                user_id, session_id, role="assistant", content=stall_stop_msg,
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
            yield {"type": "response", "level": "agent",
                   "content": prefix_content(stall_stop_msg), "asst_id": inter_id}
            if loop_config.is_enabled("fire_optimizer"):
                _fire_optimizer(user_id, session_id, channel, agent_template_id)
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
        if loop_config.is_enabled("fire_optimizer"):
            _fire_optimizer(user_id, session_id, channel, agent_template_id)
    except asyncio.CancelledError as e:
        logger.info(f"Agent loop for session {session_id} cancelled: {e}")
        # Finalize any in-progress streaming answer so it isn't stranded as
        # 'streaming' forever — the partial text is kept, marked 'interrupted'.
        if streaming_asst_id is not None:
            try:
                await db.update_interaction(
                    streaming_asst_id, content=collected_content, status="interrupted",
                )
            except Exception:
                pass
        yield {"type": "interrupted", "level": "agent", "message": str(e), "asst_id": streaming_asst_id}
        if loop_config.is_enabled("fire_optimizer"):
            _fire_optimizer(user_id, session_id, channel, agent_template_id)
        return
    except Exception as e:
        logger.error(f"Agent loop error: {e}", exc_info=True)
        if streaming_asst_id is not None:
            try:
                await db.update_interaction(
                    streaming_asst_id, content=collected_content, status="error",
                )
            except Exception:
                pass
        yield {"type": "error", "level": "agent", "message": f"Unexpected error in agent loop: {e}", "asst_id": streaming_asst_id}
        if loop_config.is_enabled("fire_optimizer"):
            _fire_optimizer(user_id, session_id, channel, agent_template_id)
        return


async def run_agent_loop_buffered(
    user_id: str,
    session_id: str,
    user_message: Any,
    system_prompt: str,
    agent_id: str,
    history=None,
    parent_interaction_id=None,
    max_turns: int = 0,
    event_callback=None,
    channel=None,
    timeout_seconds=None,
    db=None,
    agent_template_id=None,
    allowed_tools=None,
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
        import asyncio as _asyncio
        try:
            await _asyncio.wait_for(_run(), timeout=timeout_seconds)
        except _asyncio.TimeoutError:
            import logging as _log
            _log.getLogger(__name__).warning(
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

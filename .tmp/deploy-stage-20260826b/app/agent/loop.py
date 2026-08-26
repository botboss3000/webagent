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
import hashlib
import json
import logging
import os
import re
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

from app.agent.cache_profiles import stable_hash as _cache_hash
from app.agent.error_classifier import classify_tool_error, ToolError
from app.agent.loop_executor import LoopConfig
from app.agent.session_cache import (
    get_session_cache,
    get_tool_defs_cache,
    compute_tool_defs_cache_key,
)
from app.db import get_db
from app.db.offload import db_offload
from app.db.system_prompt_fragments import get_prompt_fragments


# ── Turn-limit & safety-cap defaults ─────────────────────────────────────────
# The turn limit uses 0 = unlimited. When turns are unlimited, this wall-clock
# cap is the real backstop so a tool-looping agent can never hang forever — the
# streaming chat path has no request timeout of its own. Graceful: at the cap
# the loop finalizes with a message instead of being hard-killed. Overridable
# globally via the AGENT_MAX_WALL_SECONDS env var and per-agent via the agent's
# max_wall_seconds field (set an agent's value to 0 to opt out of the cap).
DEFAULT_MAX_WALL_SECONDS = 600.0
MAX_TURN_SNAPSHOT_BYTES = 256 * 1024

# ── Memory-pressure thresholds (MB) ──────────────────────────────────────────
# Before each LLM call, if the Python process exceeds COMPACT_MB, the session
# is force-compacted first.  If it exceeds KILL_MB, the run is aborted
# gracefully to prevent the whole server from going OOM.
MEM_COMPACT_MB = 1500   # 1.5 GB → force compaction
MEM_KILL_MB = 3000      # 3 GB → abort this run


def _process_memory_mb() -> int:
    """Best-effort resident-set size in MB. Returns 0 on failure."""
    import os as _os
    try:
        if _os.name == "nt":
            import ctypes
            from ctypes import wintypes
            _psapi = ctypes.windll.psapi
            _k32 = ctypes.windll.kernel32

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            _h = _k32.OpenProcess(0x0400 | 0x0010, False, _os.getpid())
            if _h:
                _pmc = _PMC()
                if _psapi.GetProcessMemoryInfo(
                    _h, ctypes.byref(_pmc), ctypes.sizeof(_pmc)
                ):
                    _k32.CloseHandle(_h)
                    return _pmc.WorkingSetSize // (1024 * 1024)
                _k32.CloseHandle(_h)
            return 0
        else:
            with open(f"/proc/{_os.getpid()}/status") as _f:
                for _line in _f:
                    if _line.startswith("VmRSS:"):
                        return int(_line.split()[1]) // 1024  # kB → MB
            return 0
    except Exception:
        return 0


logger = logging.getLogger(__name__)

_client = None
_current_base_url = None
_current_api_key = None


def _canonical_tool_signature(
    tool_name: str,
    arguments: Any,
    parameters: Optional[Dict[str, Any]] = None,
) -> str:
    """Stable signature for semantically identical tool calls.

    Provider JSON whitespace/key ordering must not bypass the loop guard. Apply
    top-level schema defaults too, so omitting ``limit=10`` and explicitly
    sending it count as the same request.
    """
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments or "{}")
        except (TypeError, json.JSONDecodeError):
            parsed = arguments.strip()
    else:
        parsed = arguments
    if isinstance(parsed, dict):
        parsed = dict(parsed)
        props = (parameters or {}).get("properties") or {}
        if isinstance(props, dict):
            for key, spec in props.items():
                if key not in parsed and isinstance(spec, dict) and "default" in spec:
                    parsed[key] = spec["default"]
    try:
        canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        canonical = str(parsed)
    return f"{tool_name}|{canonical}"


def _bounded_turn_snapshot(
    messages: Optional[List[Dict[str, Any]]],
    tools: Optional[List[Dict[str, Any]]],
    max_bytes: int = MAX_TURN_SNAPSHOT_BYTES,
) -> Dict[str, Any]:
    """Return one bounded debug snapshot suitable for the final row of a turn.

    Intermediate assistant rows deliberately do not carry request snapshots:
    persisting the growing message list and full tool schema on every internal
    step caused quadratic database growth.
    """
    source_messages = list(messages or [])
    source_tools = list(tools or [])
    original_messages = len(source_messages)
    original_tools = len(source_tools)

    # Serialize each candidate at most once. The previous pop-and-redump loop
    # encoded the full (potentially 1M-token) history once per removed message,
    # turning finalization into quadratic CPU work on the web server event loop.
    reserve = min(1024, max(0, max_bytes // 20))
    remaining = max(0, max_bytes - reserve)
    kept_messages: List[Dict[str, Any]] = []
    for message in reversed(source_messages):
        try:
            size = len(json.dumps(message, ensure_ascii=False, default=str).encode("utf-8")) + 1
        except Exception:
            size = len(str(message).encode("utf-8", errors="replace")) + 1
        if size > remaining:
            break  # keep a contiguous newest-message suffix
        kept_messages.insert(0, message)
        remaining -= size

    kept_tools: List[Dict[str, Any]] = []
    for tool in source_tools:
        try:
            size = len(json.dumps(tool, ensure_ascii=False, default=str).encode("utf-8")) + 1
        except Exception:
            size = len(str(tool).encode("utf-8", errors="replace")) + 1
        if size > remaining:
            break
        kept_tools.append(tool)
        remaining -= size

    out: Dict[str, Any] = {}
    if kept_messages:
        out["_sent_messages"] = kept_messages
    if kept_tools:
        out["_sent_tools"] = kept_tools
    if len(kept_messages) != original_messages or len(kept_tools) != original_tools:
        out["_snapshot_truncated"] = {
            "messages_kept": len(kept_messages),
            "messages_total": original_messages,
            "tools_kept": len(kept_tools),
            "tools_total": original_tools,
        }
    return out


def _assistant_output(
    *,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    messages: Optional[List[Dict[str, Any]]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    include_snapshot: bool = False,
    reasoning_content: Optional[str] = None,
) -> str:
    """Serialize the minimal durable assistant payload.

    ``content`` is intentionally absent because it already lives in the
    interaction's dedicated content column. ``reasoning_content`` (DeepSeek
    thinking mode) is stored here so history replay can pass it back — the
    API 400s any later request whose assistant turns lack it.
    """
    payload: Dict[str, Any] = {"role": "assistant"}
    if tool_calls:
        payload["tool_calls"] = tool_calls
    if reasoning_content:
        payload["reasoning_content"] = reasoning_content
    if include_snapshot:
        payload.update(_bounded_turn_snapshot(messages, tools))
    return json.dumps(payload, ensure_ascii=False)


def _strip_reasoning_content_for_provider(
    messages: List[Dict[str, Any]],
    model_name: Optional[str],
    provider_name: Optional[str],
) -> List[Dict[str, Any]]:
    """DeepSeek's thinking mode REQUIRES assistant ``reasoning_content`` to be
    replayed back for multi-turn continuity; other OpenAI-compatible providers
    may reject the field outright. Strip it from the outgoing request unless the
    active model is a DeepSeek model. Returns a new list — the caller's
    ``messages`` is untouched."""
    ident = f"{provider_name or ''} {model_name or ''}".lower()
    if "deepseek" in ident:
        return messages
    out: List[Dict[str, Any]] = []
    for m in messages:
        if isinstance(m, dict) and "reasoning_content" in m:
            m = dict(m)
            m.pop("reasoning_content", None)
        out.append(m)
    return out


def _sanitize_history_for_deepseek(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Legacy-history recovery for DeepSeek's thinking-mode continuity rule.

    Rows written before reasoning capture can never carry their original
    ``reasoning_content`` back to the API, so the request would 400 forever.
    Drop every assistant/tool turn (a system+user-only history is valid) and
    retry once — a stuck legacy session resumes instead of crash-looping. New
    turns capture reasoning end-to-end and never need this path."""
    out = [m for m in messages if m.get("role") not in ("assistant", "tool")]
    if not any(m.get("role") == "user" for m in out):
        out.append({"role": "user", "content": "Continue the task."})
    return out

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

# ── Model-switch exemption (plan + ask modes) ──
# The Model Switcher ability marks set_model / use_premium_model / set_effort as
# DESTRUCTIVE (they can raise spend), which would put them behind the plan/ask
# confirmation gate. That gate is wrong for these tools: right-sizing the model
# (draft cheap → upgrade to premium / a vision model) is core agent behaviour,
# and these tools only write THIS session's model override — no user data. So in
# plan AND ask modes they run free. Auto mode was never gated, so this makes
# model switching unrestricted everywhere. (grep MODEL-SWITCH-EXEMPT for the
# guardrail + index spots.)
_MODEL_SWITCH_TOOLS = frozenset({"set_model", "use_premium_model", "set_effort"})

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


# ── set_effort per-arg exemption: only RAISING effort needs confirmation ──
# set_effort is confirmation-gated (it can increase cost), but "ask before
# spending more" only applies when the requested reasoning effort is HIGHER than
# the current one. Lowering effort, keeping it the same, or clearing it back to
# default is a cost reduction / revert and should run freely — the same spirit as
# the read-only run_command exemption above.
_EFFORT_RANK = {"minimal": 1, "low": 2, "medium": 3, "high": 4}


def _effort_raises_spend(tool_args: Any, current_effort: Optional[str]) -> bool:
    """True only when a set_effort call asks for a HIGHER effort than the run's
    current one — the single case that costs more and should be confirmed.

    Clearing ('default'/'reset'/empty) is a revert → never gated. An unknown
    requested level errs on the side of confirming. The current effort defaults
    to a mid 'low' baseline when unset (no explicit hint), so from the default a
    jump to medium/high confirms while minimal/low run free."""
    if not isinstance(tool_args, dict):
        return True
    req = str(tool_args.get("level", "")).strip().lower()
    if req in ("", "default", "reset"):
        return False
    req_rank = _EFFORT_RANK.get(req, 3)
    cur_rank = _EFFORT_RANK.get(str(current_effort or "").strip().lower(), 2)
    return req_rank > cur_rank


# ── Cleanup-tool finalization (model-switcher revert) ────────────────────────
# Model-switch overrides are cleaned up automatically by the backend when the
# run ends (runner._finish_run), so the skills no longer mandate a closing
# reset_to_default(). An agent may still choose to revert MID-RUN (set_model /
# set_effort back to default, or reset_to_default). Because such a revert is a
# tool call, the loop would otherwise treat the turn as still working: a
# substantive answer preceding it would be stamped `progress` (buried in the
# tools/updates panel) and the loop would take one more LLM step on the
# now-default model, emitting a redundant wrap-up line that becomes the visible
# "final" bubble. These helpers recognize the revert as housekeeping so the
# real answer is finalized and the loop ends without that extra step.
_CLEANUP_EFFORT_LEVELS = frozenset({"", "default", "reset"})


def _is_cleanup_tool(tool_name: str, tool_args: Any) -> bool:
    """True when a tool call merely reverts the run to its default model/effort
    — housekeeping, not productive work."""
    name = str(tool_name or "").strip()
    if name == "reset_to_default":
        return True
    if not isinstance(tool_args, dict):
        return False
    if name == "set_effort":
        return str(tool_args.get("level", "")).strip().lower() in _CLEANUP_EFFORT_LEVELS
    if name == "set_model":
        return str(tool_args.get("model", "")).strip().lower() in ("", "default")
    return False


def _is_substantive_answer(text: str) -> bool:
    """True when assistant text reads like a real answer rather than a short
    transitional line ('Now switching to premium…'). Long text always counts;
    short text counts when it is structurally an answer (markdown heading or
    bullet list)."""
    if not text:
        return False
    stripped = re.sub(r"[\s#>*_`\[\]()|]+", "", text)
    if len(stripped) >= 200:
        return True
    return bool(re.search(r"(?m)^#{1,4}\s+\S|^\s*[-*+]\s+\S", text))


def _cleanup_final_step(content: str, tool_calls: Any) -> bool:
    """True when a step carries a substantive answer plus ONLY cleanup tool
    calls — the signal that the real answer is done and the remaining tools are
    housekeeping to run before ending the loop."""
    if not content or not content.strip():
        return False
    if not hasattr(tool_calls, "values"):
        return False
    calls = list(tool_calls.values())
    if not calls:
        return False
    if not _is_substantive_answer(content):
        return False
    for tc in calls:
        fn = getattr(tc, "function", None)
        name = getattr(fn, "name", None) if fn is not None else None
        if not name:
            return False
        args_raw = getattr(fn, "arguments", None) or "{}"
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
        except json.JSONDecodeError:
            args = {}
        if not _is_cleanup_tool(name, args):
            return False
    return True


# ── git_tool per-arg exemption: read-only git operations skip the gate ──
# `git_tool` is confirmation-gated (it can commit/push/reset/checkout), but
# inspect-only operations are routine and pointless to gate — same spirit as the
# read-only run_command exemption. Only operations that CANNOT mutate regardless
# of their args are exempt; ops that mutate with flags (branch -D, tag -d, config
# --global, remote add, stash drop, worktree add/remove) deliberately confirm,
# alongside every write op. (The agent still has a no-prompt path for the listing
# forms of those — run_command exempts `git branch`, `git remote`, `git stash
# list`, `git config --get/--list`.)
SAFE_GIT_OPERATIONS = frozenset({
    "status", "log", "diff", "show", "ls-files", "rev-parse", "blame",
    "describe", "for-each-ref", "reflog", "cat-file", "shortlog", "name-rev",
})


def _is_safe_git_operation(tool_args: Any) -> bool:
    """True for read-only git_tool operations that don't need confirmation."""
    if not isinstance(tool_args, dict):
        return False
    op = str(tool_args.get("operation", "")).strip().lower()
    return op in SAFE_GIT_OPERATIONS


# ── http_request per-arg exemption: read-only HTTP methods skip the gate ──
# `http_request` is confirmation-gated (it can POST/PUT/DELETE to any URL), but
# read-only methods (GET/HEAD/OPTIONS) fetch without changing remote state, so
# they run freely — the same spirit as the read-only run_command exemption.
_SAFE_HTTP_METHODS = frozenset({"get", "head", "options"})


def _is_safe_http_request(tool_args: Any) -> bool:
    """True for read-only HTTP methods that don't need confirmation."""
    if not isinstance(tool_args, dict):
        return False
    method = str(tool_args.get("method", "GET")).strip().lower()
    return method in _SAFE_HTTP_METHODS


# ── browser_action per-arg exemption: read/navigate actions skip the gate ──
# `browser_action` is confirmation-gated because it can act AS the logged-in user
# (click, type, run JS). But navigating to a page and reading it (get_text,
# screenshot, …) change nothing on the user's behalf, so only the acting actions
# confirm — the same spirit as the read-only run_command exemption. The SAFE set
# is an allow-list so any newly-added action defaults to confirming.
_SAFE_BROWSER_ACTIONS = frozenset({
    "navigate", "get_text", "get_html", "screenshot", "wait", "title", "url", "close",
})


def _is_safe_browser_action(tool_args: Any) -> bool:
    """True for read-only/navigation browser_action calls (not acting as user)."""
    if not isinstance(tool_args, dict):
        return False
    action = str(tool_args.get("action", "")).strip().lower()
    return action in _SAFE_BROWSER_ACTIONS


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
    global_defaults: Optional[Dict[str, Dict[str, str]]] = None,
) -> frozenset:
    """
    Build the effective set of tool names that require user confirmation before running.

    Merges four sources (union, never subtract):
      1. DESTRUCTIVE_TOOLS  — hardcoded baseline (admin tools always require confirmation).
      2. safety_policy.destructive_tools — per-agent additions saved by the user.
      3. requires_confirmation flag on each ToolInfo — per-tool DB flag.
      4. global per-tool defaults with permission == "ask" — app-wide admin
         defaults every agent inherits. Additive: a global "ask" can only ADD a
         tool to the confirmation set; the agent's own lists already win because
         this is a pure union (we never subtract).
    """
    result = set(DESTRUCTIVE_TOOLS)

    sp = _parse_safety_policy(agent_rec)
    extra = sp.get("destructive_tools") or []
    if isinstance(extra, list):
        result.update(extra)

    for name, info in tools.items():
        if getattr(info, "requires_confirmation", False):
            result.add(name)

    # Global-default "ask" tools (only those actually loaded for this agent).
    if global_defaults:
        for name, dims in global_defaults.items():
            if dims.get("permission") == "ask" and name in tools:
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


def _get_client(base_url: Optional[str] = None, api_key: Optional[str] = None):
    global _client, _current_base_url, _current_api_key

    if base_url is None:
        base_url = os.environ.get("LLM_BASE_URL") or os.environ.get("OPENROUTER_BASE_URL")
    if api_key is None:
        api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or ""

    if not base_url:
        raise ValueError(
            "No LLM base URL configured. Open Settings → Providers to set up a provider, "
            "or set LLM_BASE_URL environment variable."
        )

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


def _max_output_tokens() -> int:
    """Per-call output-token ceiling for the LLM.

    A hard 4096 cap silently truncates any large assistant message — most
    painfully a `render_visual` call whose entire HTML document is carried inline
    as the tool argument. A real dashboard/page easily exceeds 4096 output tokens,
    so the document arrives cut off (no closing </html>), the genui guard rejects
    it, and the agent can never finish — the core "it says it built the dashboard
    but didn't" failure. Default generously and let deployments tune it via
    LLM_MAX_OUTPUT_TOKENS. Kept within what mainstream models accept."""
    try:
        v = int(os.environ.get("LLM_MAX_OUTPUT_TOKENS", "16384"))
        return v if v > 0 else 16384
    except (TypeError, ValueError):
        return 16384


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


def _stream_open_seconds() -> float:
    """Max seconds to wait for the LLM to RETURN the response-stream object (the
    initial connect/open) before treating the turn as stalled.

    The per-chunk stall guard (``_stream_stall_seconds``) only bounds reads AFTER
    the stream object exists. The open call itself is otherwise protected only by
    the client's own request timeout — which has been observed NOT to fire,
    freezing the turn indefinitely (no tokens, no error, no completion, no
    heartbeat advance, so even the liveness watchdog's frozen path can miss it).
    Bounding the open turns that silent freeze into a fast, *resumable* crash.

    0 disables the bound. Kept generously above normal connect + response-head
    latency (a slow model is caught by the per-chunk stall guard, not here)."""
    try:
        return float(os.environ.get("AGENT_STREAM_OPEN_SECONDS", "120"))
    except (TypeError, ValueError):
        return 120.0


async def _open_stream(create_kwargs: Dict[str, Any], timeout_s: float, client: Any = None):
    """Open the streaming LLM response, bounded by ``timeout_s`` so a hung connect
    cannot freeze the turn. ``timeout_s <= 0`` disables the bound (falls back to
    the client's own request timeout). Raises ``asyncio.TimeoutError`` if the
    stream object isn't returned in time — the caller turns that into a resumable
    stop, mirroring the mid-stream stall guard."""
    _coro = (client or _get_client()).chat.completions.create(**create_kwargs)
    if timeout_s and timeout_s > 0:
        return await asyncio.wait_for(_coro, timeout=timeout_s)
    return await _coro


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
    session_id: Optional[str] = None,
    cost_usd: float = 0.0,
    cost_source: Optional[str] = None,
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    uncached_input_tokens: Optional[int] = None,
    reasoning_tokens: int = 0,
) -> Optional[dict]:
    """Compute the per-call charge, write a usage_events row, and debit the
    user's credit wallet when applicable.

    Thin wrapper over the single charge path (plugins/billing/charge.py) so
    text runs and image generation share one implementation. Returns the
    charge summary as a dict for the event stream, or None on failure / when
    billing tables don't exist. Silent-failure-friendly: any exception is
    logged and swallowed so the chat continues working even if billing is
    misconfigured."""
    provider_cents = int(round((llm_cost or 0) * 100)) if llm_cost else 0
    from plugins.billing.charge import record_and_charge
    return await record_and_charge(
        db, agent_id, user_id,
        input_tokens=int(input_tokens or 0),
        output_tokens=int(output_tokens or 0),
        provider_cost_cents=provider_cents,
        model=model_name,
        provider=provider_name,
        interaction_id=interaction_id,
        session_id=session_id,
        cost_usd=cost_usd,
        cost_source=cost_source,
        cached_input_tokens=cached_input_tokens,
        cache_write_tokens=cache_write_tokens,
        uncached_input_tokens=uncached_input_tokens,
        reasoning_tokens=reasoning_tokens,
        source="chat",
    )


async def _check_interrupt(
    session_id: str,
    interrupt_event: Optional[asyncio.Event],
    *,
    db: Optional[Any] = None,
):
    """
    Checks the local event, the DB interrupt flag, and whether the session
    has been recycled/deleted. Raises CancelledError if interrupted.
    """
    if interrupt_event and interrupt_event.is_set():
        raise asyncio.CancelledError("Agent interrupted by new user message (local event).")

    db = db or get_db()
    if await db.check_interrupt(session_id):
        # Clear the flag so next runs are clean
        await db.clear_interrupt(session_id)
        raise asyncio.CancelledError("Agent interrupted by new user message (db flag).")

    # ── Session-death safety check ──────────────────────────────────────────
    # If the session was recycled (soft-deleted) or hard-deleted while the loop
    # was running, abort immediately. This catches the case where the interrupt
    # flag was already consumed on a previous check but the session was recycled
    # between checks, or where the LLM call was in-flight when the recycle happened.
    try:
        if await db.is_session_dead(session_id):
            logger.warning("Session %s is dead (recycled/deleted) — interrupting loop", session_id[:12])
            # Clear any leftover interrupt flag so the next run is clean
            try:
                await db.clear_interrupt(session_id)
            except Exception:
                pass
            raise asyncio.CancelledError("Session has been recycled or deleted.")
    except asyncio.CancelledError:
        raise
    except Exception as _sde:
        logger.warning("session-death check in _check_interrupt failed: %s", _sde)


async def validate_tool_call(name: str, args: dict, tools: Dict[str, Any],
                             denied: Optional[List[str]] = None) -> Optional[dict]:
    """
    Validate a tool call before execution.
    Returns a structured error dict if invalid, or None if valid.

    ``denied`` is the agent's block (deny) list. A denied tool is never loaded,
    so without this it would look like a typo ("not found"); instead we tell the
    agent plainly that the tool is turned OFF for it and how to proceed.
    """
    from app.tools.loader import ToolInfo

    if name not in tools:
        if denied and name in denied:
            msg = (
                f"Tool '{name}' is turned OFF for this agent (permission: deny), "
                f"so you cannot call it. Tell the user this tool is blocked and ask "
                f"them to enable it (in the agent's Tools settings, or by switching "
                f"on the ability that provides it) — or accomplish the task another "
                f"way. Do not retry '{name}'."
            )
            return {
                "status": "tool_denied",
                "tool": name,
                "message": msg,
                "recoverable": True,
                "hint": "Ask the user to enable this tool, or use a different approach.",
            }
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
    Check if the user confirmed a gated tool call (plan/ask modes).

    A confirmation ONLY counts when the user's approving message FOLLOWS the
    agent's approval-seeking message. The user's original task message can
    never authorize execution — it predates any request for approval. (This is
    the PLAN-MODE-APPROVAL rule: without it, a task message containing "ok" as
    a substring was read as standing approval and silently bypassed the plan
    gate.) Keywords are matched on word boundaries so "ok" can't match inside
    "looking at".
    """
    import re as _re

    ask_keywords = ("would you like me to", "should i", "shall i",
                    "let me know if", "do you want me to",
                    "confirm", "approve", "ok to", "okay to",
                    "proceed", "go ahead and")
    confirm_keywords = ("yes", "go ahead", "proceed", "approved", "ok", "okay",
                        "sure", "do it", "confirm", "go for it", "please do",
                        "continue")

    # Anchor: the LAST assistant message that explicitly asked for approval,
    # plus the LAST user message. A tool call may only pass if the user's
    # message comes strictly AFTER that ask — i.e. approval arrived in a
    # following message, never the original request.
    ask_idx = None
    last_user_idx = None
    for i, msg in enumerate(messages):
        role = msg.get("role")
        if role == "user":
            last_user_idx = i
        elif role == "assistant":
            content = (msg.get("content") or "").lower()
            if any(_re.search(r"\b" + _re.escape(kw) + r"\b", content) for kw in ask_keywords):
                ask_idx = i

    # No approval request yet, or no user message after it → not confirmed.
    if ask_idx is None or last_user_idx is None or last_user_idx <= ask_idx:
        return False

    last_user_content = (messages[last_user_idx].get("content") or "").lower()
    for kw in confirm_keywords:
        if _re.search(r"\b" + _re.escape(kw) + r"\b", last_user_content):
            return True
    return False


def _build_layered_messages(
    *,
    shared_system: str,
    capability_parts: List[str],
    agent_system: str,
    turn_parts: List[str],
    history: Optional[List[Dict[str, Any]]],
    user_message: Any,
) -> List[Dict[str, Any]]:
    """Serialize cache layers using only standard provider message roles."""
    messages: List[Dict[str, Any]] = []
    for system_block in (shared_system, *capability_parts, agent_system):
        if system_block and system_block.strip():
            messages.append({"role": "system", "content": system_block.strip()})
    if history:
        messages.extend(history)
    if turn_parts:
        turn_content = "\n\n".join(
            part.strip() for part in turn_parts if part and part.strip()
        )
        if turn_content:
            messages.append({"role": "system", "content": turn_content})
    # user_message=None means "no trailing user turn" (resume-without-nudge):
    # the history is replayed exactly as it ended and the model continues.
    if user_message is not None:
        messages.append({"role": "user", "content": user_message})
    return messages


def _filter_history_for_available_tools(
    messages: Optional[List[Dict[str, Any]]],
    available_tools: set[str],
) -> List[Dict[str, Any]]:
    """Remove historical calls/results for tools absent from this turn.

    Besides preventing invalid provider histories, this is an authorization
    boundary when a caller's tier becomes more restrictive: old discovery-tool
    output must not keep advertising a capability that is no longer available.
    If every call on an assistant progress message is forbidden, the whole
    message is dropped because its prose commonly announces the forbidden tool.
    """
    if not messages:
        return []
    out: List[Dict[str, Any]] = []
    allowed_call_ids: set[str] = set()
    for original in messages:
        msg = dict(original)
        if msg.get("role") == "assistant" and isinstance(msg.get("tool_calls"), list):
            kept_calls = []
            for call in msg["tool_calls"]:
                fn = call.get("function") if isinstance(call, dict) else None
                name = fn.get("name") if isinstance(fn, dict) else None
                if name in available_tools:
                    kept_calls.append(call)
                    if call.get("id"):
                        allowed_call_ids.add(str(call["id"]))
            if not kept_calls:
                continue
            msg["tool_calls"] = kept_calls
        elif msg.get("role") == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            name = str(msg.get("name") or msg.get("tool_name") or "")
            # OpenAI tool-result messages only require ``tool_call_id`` and
            # ``content``.  DB-replayed history intentionally uses that canonical
            # shape, so it usually has no redundant tool name.  The matching
            # assistant call above is the authority for both the name and whether
            # the call is still available this turn.  Requiring a name here drops
            # valid results and leaves an assistant ``tool_calls`` message
            # unanswered, which providers reject with HTTP 400.
            if call_id:
                if call_id not in allowed_call_ids:
                    continue
            elif name not in available_tools:
                continue
        out.append(msg)
    return out


def _schedule_output_closer(
    *,
    user_id: str,
    session_id: str,
    agent_id: Optional[str],
    final_asst_id: Optional[str],
    parent_interaction_id: Optional[str],
    db: Optional[Any],
    channel: Optional[str],
    audit_eligible: bool = False,
    execution_mode: Optional[str] = None,
    starting_execution_mode: Optional[str] = None,
) -> None:
    """Fire the Output Closer (close-out loop) in the background.

    Runs as a separate parallel task right after the final ``response`` event
    is yielded — or after an interrupted/errored run finalizes its partial
    answer — so it never delays or blocks the answer delivery. The worker
    gates itself on the app_function toggle; any error is swallowed there.

    ``audit_eligible`` marks a COMPLETED run (clean final response) so the
    checklist-auditor stage may run; partial endings (interrupt, error,
    stall-guard, max-turns, pre-cleanup) pass False and get the plain summary
    only. ``execution_mode`` is the run's resolved mode, carried so a
    checklist send-back re-runs the agent under the SAME mode the original
    run used (never silently escalated, never silently demoted to ask).
    """
    try:
        from app.agent.output_closer import run_output_closer
        asyncio.create_task(run_output_closer(
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            final_asst_id=final_asst_id,
            parent_interaction_id=parent_interaction_id,
            db=db,
            channel=channel,
            audit_eligible=audit_eligible,
            execution_mode=execution_mode,
            starting_execution_mode=starting_execution_mode,
        ))
    except Exception:
        pass


def _schedule_manager_check(
    kind: str,
    *,
    user_id: str,
    session_id: str,
    agent_id: Optional[str] = None,
    agent_rec: Optional[dict] = None,
    final_asst_id: Optional[str] = None,
    parent_interaction_id: Optional[str] = None,
    db: Optional[Any] = None,
    channel: Optional[str] = None,
    execution_mode: Optional[str] = None,
    extra: str = "",
    max_checks: Optional[int] = None,
    check_index: Optional[int] = None,
    kind_max_checks: Optional[int] = None,
    kind_check_index: Optional[int] = None,
) -> Optional["asyncio.Task"]:
    """Fire a Manager trigger check in the background (advisory triggers:
    watchdog, commit_gate, and async plan/edit gates).

    Runs as a parallel task like the closer, so it never delays or blocks the
    main response. The Manager gates itself on the ``run_manager`` app
    function and the per-run check cap; any error is swallowed there — a
    Manager hiccup must never break a turn.
    """
    try:
        from app.agent.manager import run_manager_check
        return asyncio.create_task(run_manager_check(
            kind,
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            agent_rec=agent_rec,
            final_asst_id=final_asst_id,
            parent_interaction_id=parent_interaction_id,
            db=db,
            channel=channel,
            execution_mode=execution_mode,
            extra=extra,
            max_checks=max_checks,
            check_index=check_index,
            kind_max_checks=kind_max_checks,
            kind_check_index=kind_check_index,
        ))
    except Exception:
        return None


async def _stream_agent_events_impl(
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
    execution_mode: str = 'ask',
    attachment_docs: Optional[List[Dict[str, Any]]] = None,
    system_prompt_parts: Optional[Any] = None,
    turn_reservation_key: Optional[str] = None,
    persona_prompt: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Run the unified agent loop and yield structured events.
    agent_template_id is used to gate admin-only tools (e.g. 'admin-agent').
    allowed_tools is the list of Tier-2 tool names DISABLED for this agent.
    execution_mode controls tool execution permission (set by the chat pill,
    which cycles Ask -> Plan -> Auto):
      'plan' — research freely; ANY write/edit needs confirmation, so the agent
               leans read-only ("read-only but can ask"). Also injects the Plan
               prompt (deep-research, ask clarifying questions, deliver a plan).
      'ask'  — write/destructive tools require user confirmation (default).
      'auto' — all tools allowed without confirmation.
    Legacy values are normalized: 'read' -> 'plan', 'write' -> 'ask'.
    """
    # User BYOD (bring-your-own-database): pin data-plane routing to THIS run's
    # user for the whole turn, so every get_db() below — and in the tools, memory
    # and db_offload calls this loop makes — resolves to this user's own database.
    # This is the single chokepoint every agent turn funnels through (interactive,
    # buffered, background, automation and self-heal resume), which is why setting
    # the caller-uid contextvar here reliably covers the non-HTTP paths where it is
    # not already set by the request middleware. No-op in single-tenant mode.
    try:
        from app.auth.identity import set_verified_caller_uid
        if user_id:
            set_verified_caller_uid(user_id)
    except Exception:  # noqa: BLE001 — never let routing setup break a turn
        pass
    # Normalize the execution-mode id, accepting legacy Read/Write names so
    # in-flight sessions and saved DB values keep working. Valid custom slugs
    # pass through and are resolved against the agent after its record loads.
    from app.agent.execution_modes import (
        accumulated_contract, contract_for_mode, normalize_mode_id,
        resolve_execution_mode,
    )
    _raw_execution_mode = str(execution_mode or '').strip().lower()
    # wkspc is retained for the Codex engine; native/custom mode ids use the
    # shared slug normalizer (legacy read/write aliases remain supported).
    execution_mode = 'wkspc' if _raw_execution_mode == 'wkspc' else normalize_mode_id(execution_mode)
    _run_start_execution_mode = execution_mode
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
    _browser_authority = getattr(db, "authority_mode", "") == "browser"
    if not turn_reservation_key and parent_interaction_id:
        from app.agent.turn_reservations import stable_key as _stable_turn_key
        turn_reservation_key = _stable_turn_key(
            "chat-turn", user_id, session_id, parent_interaction_id
        )

    # Prompt layout v2 keeps reusable platform/capability blocks ahead of
    # agent-, session-, and turn-specific content. Legacy callers may continue
    # passing one flattened string.
    _shared_system = getattr(system_prompt_parts, "shared_core", "") or ""
    _agent_system = getattr(system_prompt_parts, "agent_context", "") or ""
    _turn_system_parts: List[str] = []
    _initial_turn_context = getattr(system_prompt_parts, "turn_context", "") or ""
    if _initial_turn_context:
        _turn_system_parts.append(_initial_turn_context)
    if system_prompt_parts is None:
        _agent_system = system_prompt or ""
    _capability_system_parts: List[str] = []

    # Load the agent record first so a per-agent LLM override can be applied.
    agent_name = "Agent"
    _agent_rec: Optional[Dict[str, Any]] = None
    if agent_id:
        _agent_rec = await db_offload(lambda: db.get_agent_by_id(agent_id))
        if _agent_rec and _agent_rec.get("name"):
            agent_name = _agent_rec["name"]
    _mode_entry = resolve_execution_mode(_agent_rec, execution_mode)
    _permission_policy = _mode_entry.get("permission_policy", "read_only")
    try:
        _mode_history = await db.get_session_execution_mode_history(session_id)
    except Exception:
        _mode_history = []
    _active_mode_contract = contract_for_mode(_mode_entry)
    _accumulated_mode_contract = accumulated_contract(
        _agent_rec, _mode_history, execution_mode,
    )
    _requires_plan_document = bool(_active_mode_contract.get("require_plan_document"))
    if system_prompt_parts is not None:
        try:
            from app.agent.cache_profiles import (
                profile_from_metadata as _profile_from_metadata,
                profile_layer_blocks as _profile_layer_blocks,
                profile_abilities as _profile_abilities,
            )
            _profile_meta = (_agent_rec or {}).get("metadata") or {}
            if isinstance(_profile_meta, str):
                _profile_meta = json.loads(_profile_meta or "{}")
            _profile_extensions = (
                _profile_meta.get("capability_extensions") or []
                if isinstance(_profile_meta, dict)
                else []
            )
            from app.agent.ability_access import filter_abilities_for_caller as _filter_profile
            _profile_name = _profile_from_metadata(_profile_meta)
            _profile_ids = set(_profile_abilities(_profile_name, _profile_extensions))
            _allowed_profile_ids = await _filter_profile(
                agent_id or "", _profile_ids, user_id, db=db,
            )
            _capability_system_parts.extend(_profile_layer_blocks(
                _profile_name,
                _profile_extensions,
                allowed_abilities=_allowed_profile_ids,
            ))
        except Exception as _cpe:
            logger.warning("capability cache layers build failed: %s", _cpe)

    # ── Run contract + Manager config (per-agent) ──────────────────────────
    # The contract is the working agent's MECHANICAL discipline (metadata
    # ['contract']) — flags/counters enforced synchronously, zero LLM calls.
    # The Manager is the opt-in SUPERVISORY layer (metadata['manager']) — one
    # bounded LLM call per trigger (plan_gate / edit_gate / watchdog /
    # commit_gate). Both fail open: a missing/broken config disables the rule.
    _contract: Dict[str, Any] = {}
    _contract_state: Dict[str, Any] = {}
    _manager_cfg: Dict[str, Any] = {}
    _manager_state: Dict[str, Any] = {
        "plan_gate_done": False,
        "block_count": 0,
        "checks_used": 0,
        "checks_used_by_kind": {},
        "changed_paths": set(),
        "edit_events": [],
        "verification_events": [],
        "starter_write_checked": False,
    }
    try:
        from app.agent.contracts import parse_contract, new_contract_state
        _contract = parse_contract(_agent_rec) or {}
        _contract_state = new_contract_state()
    except Exception:
        _contract = {}
        _contract_state = {}
    try:
        from app.agent.manager import resolve_manager_config, trigger_enabled
        _manager_cfg = resolve_manager_config(_agent_rec, execution_mode) or {}
        _manager_state["watchdog_enabled"] = trigger_enabled(_manager_cfg, "watchdog")
    except Exception:
        _manager_cfg = {}

    # ── Alternate engine dispatch ────────────────────────────────────────────
    # An agent may declare a non-default runtime in metadata.engine (e.g. a Local
    # Claude Code agent driven by the installed `claude` CLI). When set, the WHOLE
    # turn is handed to that engine's adapter, which yields THIS loop's event
    # vocabulary and persists the same interactions rows — so the UI/reload are
    # unchanged. Done BEFORE apply_provider_for_run so the engine's child process
    # inherits a clean env (it uses its own login, not this run's provider keys).
    # One generic lookup, no per-engine branch; default agents skip it entirely.
    _engine_id = ""
    try:
        _eng_meta = _agent_rec.get("metadata") if _agent_rec else None
        if isinstance(_eng_meta, str):
            _eng_meta = json.loads(_eng_meta or "{}")
        if isinstance(_eng_meta, dict):
            _engine_id = str(_eng_meta.get("engine") or "").strip()
    except Exception:
        _engine_id = ""
    if _engine_id and _engine_id != "default":
        if _browser_authority:
            raise RuntimeError(
                "Alternate engines are not enabled for browser-authority sessions."
            )
        from plugins.engines import get_engine_stream
        _engine_fn = get_engine_stream(_engine_id)
        if _engine_fn is not None:
            async for _ev in _engine_fn(
                user_id=user_id, session_id=session_id, agent_id=agent_id,
                user_message=user_message, agent_rec=_agent_rec, db=db,
                system_prompt=system_prompt, channel=channel,
                parent_interaction_id=parent_interaction_id,
                # Already excludes the current user row and has run WebAgent's
                # compaction/task-hiding pipeline. Stateless engine wrappers can
                # render it directly instead of repeating DB work.
                history=history,
                interrupt_event=interrupt_event,
                # Raw attachment rows (images + files) for engines that read them
                # off disk via their own tools — the default loop instead gets
                # images pre-inlined into user_message, so it ignores this.
                attachment_docs=attachment_docs,
                # Chat pill (Ask/Plan/Auto) — the claude_code engine maps it onto
                # `claude --permission-mode`. Engines that don't care ignore it via
                # their **_ignored catch-all.
                execution_mode=execution_mode,
                # Persona-only prompt for engine agents that forward it to the CLI
                # (agent-authored instructions without platform boilerplate).
                persona_prompt=persona_prompt,
            ):
                yield _ev
            return
        logger.warning("agent %s declares unknown engine %r — using default loop",
                       agent_id, _engine_id)

    # Apply the effective provider config for THIS run (not shared with any other
    # user): the user's default with any per-agent LLM override (custom model)
    # and then any per-session override (the model picked in this chat's footer)
    # layered on top, so the run uses the right model — not just the global
    # default. Resolution order: app-default → agent → session.
    # Keep provider credentials local to this coroutine. Writing the selected
    # model's configuration to os.environ lets concurrent chats cross-wire keys.
    llm_config = await apply_provider_for_run(
        user_id, _agent_rec, None if _browser_authority else session_id,
        apply_env=False)

    def _runtime_model(config: Dict[str, Any]) -> str:
        model = str(config.get("model") or "")
        base_url = str(config.get("base_url") or "")
        return model.split("/", 1)[-1] if base_url and "openrouter.ai" not in base_url and "/" in model else model

    def _client_for(config: Dict[str, Any]):
        return _get_client(config.get("base_url"), config.get("api_key"))

    model_name = _runtime_model(llm_config)
    provider_name = llm_config.get("provider") or ""
    llm_client = _client_for(llm_config)
    reasoning_effort = (llm_config.get("reasoning_effort") or "").strip().lower() or None

    if llm_config.get("_credential_error") == "decrypt_failed":
        yield {
            "type": "error",
            "level": "fatal",
            "stop_cause": "failed",
            "message": (
                "The configured LLM credential exists but could not be decrypted: "
                "its encryption key no longer matches the stored secret. Re-save "
                "the API key in App Settings → Providers, then retry."
            ),
        }
        return

    # ── Validate resolved config — no silent fallbacks to OpenRouter ──────────
    if not model_name:
        yield {"type": "error", "level": "fatal",
               "message": "No LLM model configured. Open Settings → Providers to select a model."}
        return
    if not llm_config.get("base_url"):
        yield {"type": "error", "level": "fatal",
               "message": "No LLM provider configured. Open Settings → Providers to set one up."}
        return

    # ── App-wide global per-tool defaults (admin) ────────────────────────────
    # The DEFAULTS every agent inherits unless it has its own per-tool override.
    # Fetched once per run and threaded into permission (deny/ask) + visibility
    # resolution below. Empty {} when unset → a complete no-op. Never let a read
    # failure break the run.
    _global_tool_defaults: Dict[str, Dict[str, str]] = {}
    try:
        from app.admin.integrations import get_global_tool_defaults as _ggtd
        _global_tool_defaults = await _ggtd() or {}
    except Exception as _gde:
        logger.warning("global tool defaults load failed: %s", _gde)

    # DENY: merge global-default "deny" tools into the allowed_tools (block) list
    # passed to load_tools. Additive — globals can ADD a deny, but the agent's own
    # lists always win, so we EXCLUDE any tool the agent explicitly named in its
    # own allowed_tools (block) or safety_policy.destructive_tools (ask). Tier-1
    # always-on tools can never be denied.
    if _global_tool_defaults:
        try:
            from app.tools.loader import TIER_1_ALWAYS_ON as _TIER1
            from app.tools.tool_defaults import (
                _agent_deny_set as _ads, _agent_ask_set as _aas,
            )
            _agent_named = _ads(_agent_rec) | _aas(_agent_rec)
            _global_deny = [
                t for t, dims in _global_tool_defaults.items()
                if dims.get("permission") == "deny"
                and t not in _agent_named
                and t not in _TIER1
            ]
            if _global_deny:
                _merged_block = list(dict.fromkeys(list(allowed_tools or []) + _global_deny))
                allowed_tools = _merged_block
        except Exception as _dde:
            logger.warning("global deny merge failed: %s", _dde)

    load_start = time.time()
    tools = await load_tools(user_id, agent_id=agent_id, agent_template_id=agent_template_id,
                              allowed_tools=allowed_tools, session_id=session_id,
                              turn_id=parent_interaction_id or "",
                              gate_caller_access=True)
    history = _filter_history_for_available_tools(history, set(tools))
    load_duration = int((time.time() - load_start) * 1000)

    # ── Pipeline: tools loaded ──
    yield {"type": "pipeline", "level": "pipeline",
           "step": "load_tools", "count": len(tools),
           "names": list(tools.keys()),
           "duration_ms": load_duration}

    # ── Session-death safety switch ──────────────────────────────────────────
    # If the session was recycled (soft-deleted) while this generator was being
    # set up, abort NOW — before any LLM call, tool execution, or interaction
    # write. This is the single chokepoint every agent turn funnels through:
    # interactive chat, background automation, optimizer runs, and self-heal
    # resume. Without it a recycled session could still execute agent loops.
    try:
        if await db.is_session_dead(session_id):
            logger.warning("Session %s is dead (recycled/deleted) — aborting loop", session_id[:12])
            yield {"type": "error", "level": "agent",
                   "message": "Session has been deleted — loop aborted"}
            # Still yield a final pipeline event so the caller knows we stopped
            yield {"type": "pipeline", "level": "pipeline",
                   "step": "session_dead", "session_id": session_id}
            return
    except Exception as _sde:
        # Never let a safety check failure break the run — log and continue
        logger.warning("session-death check failed: %s", _sde)

    # ── Integration status: inject available OAuth integrations into system prompt ──
    _OAUTH_PROVIDER_TYPES = {"google", "microsoft", "yahoo", "dropbox", "meta",
                              "twitter", "linkedin", "tiktok", "pinterest",
                              "reddit", "snapchat", "twitch"}
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
        # browser_session removed — the web_session_* cookie-replay tools moved into
        # the Browser Control ability (they source cookies from the user's live
        # in-app browser login, with a pasted-cookie fallback).
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
        _turn_system_parts.append("## Available Integrations\n" + "\n".join(_int_lines))

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
                _turn_system_parts.append("## Web automation recipes\n" + _recipes)

    yield {"type": "pipeline", "level": "pipeline",
           "step": "integration_status",
           "count": len(_int_summary),
           "integrations": _int_summary}

    # ── Per-tool gating sets (computed once, after tools load) ───────────────
    # effective_destructive = tools that require user confirmation (the "ask"
    # set); auto_confirm skips the gate; concurrent_limit caps parallel tools.
    # Computed here (before the # [TOOLS] index) so the index can TELL the agent
    # which of its tools are confirmation-gated or outright blocked — closing the
    # gap where an agent only discovered a tool was off-limits by calling it.
    effective_destructive = _build_effective_destructive_set(_agent_rec, tools, _global_tool_defaults)
    auto_confirm = _is_auto_confirm(_agent_rec)
    concurrent_limit = _max_concurrent_tools(_agent_rec)
    # The agent's deny (block) set — tools withheld entirely — minus the Tier-1
    # always-on tools (which can never be denied). Surfaced as "Blocked" so the
    # agent knows the tool exists but is off-limits, rather than hitting a
    # misleading "tool not found".
    from app.tools.loader import TIER_1_ALWAYS_ON as _TIER1_FOR_INDEX
    _denied_names = [t for t in (allowed_tools or []) if t not in _TIER1_FOR_INDEX]

    # ── Ability + tool exposure index (# [ABILITIES] + # [TOOLS]) ────────────
    # Generate both catalogs from the ACTUAL loaded set. A "discoverable" ability
    # collapses to one # [ABILITIES] entry and its tools are withheld from
    # # [TOOLS] until load_ability pulls them in; a "visible" ability's tools flow
    # into # [TOOLS] per their own visibility (sent now vs load_tool on demand).
    # `_agent_tool_modes` / `_agent_ability_modes` are stable for the run; the
    # active sets grow as the model loads tools/abilities. (Function-scope vars —
    # the per-iteration schema filter below reuses them.)
    _agent_tool_modes: Dict[str, str] = {}
    _agent_ability_modes: Dict[str, str] = {}
    _agent_discovery_default: Optional[str] = None
    _active_tool_names: List[str] = []
    _active_ability_names: List[str] = []
    _suppressed_ability_names: List[str] = []
    try:
        from app.tools.tool_modes import (
            resolve_mode as _tm_resolve, render_index as _tm_render,
            render_ability_index as _tm_render_abilities,
            ability_is_revealed as _tm_ability_revealed,
            tool_hidden_by_ability as _tm_tool_hidden,
            ability_for_tool as _tm_ability_for_tool,
        )
        if agent_id:
            _agent_tool_modes = await db.get_agent_tool_modes(agent_id)
            _agent_ability_modes = await db.get_agent_ability_modes(agent_id)
            # Agent-level default visibility: when set to "discoverable", every
            # ability with no explicit per-ability choice is withheld behind the
            # `# [ABILITIES]` menu (tools + skill arrive via load_ability) instead
            # of shipping its full tool schema each turn. Absent ⇒ "visible".
            try:
                _agent_discovery_default = await db.get_agent_discovery_default(agent_id)
            except Exception:
                _agent_discovery_default = None
        if session_id:
            # Offloaded + concurrent: these three session reads are NOT turn-cached
            # (the active set legitimately grows mid-run), so on remote Postgres
            # each is a ~150ms round-trip. Running them on the event loop serially
            # froze the loop ~450ms/turn; offload keeps the loop free for the LLM
            # stream and collapses the three into one concurrent batch.
            _active_tool_names, _active_ability_names, _suppressed_ability_names = await asyncio.gather(
                db_offload(lambda: db.get_session_active_tools(session_id)),
                db_offload(lambda: db.get_session_active_abilities(session_id)),
                db_offload(lambda: db.get_session_suppressed_abilities(session_id)),
            )
        _active_set = set(_active_tool_names)
        _active_ability_set = set(_active_ability_names)
        _suppressed_ability_set = set(_suppressed_ability_names)

        # ── Semantic routing: hint + auto-reveal (drop-in, best-effort) ──
        # Rank the agent's discoverable-and-unloaded abilities against what the
        # user just asked (in-process embeddings, free). The top matches are
        # starred in the [ABILITIES] menu (hint); the single strongest match above
        # a high bar is loaded automatically (auto-reveal) so the agent skips the
        # notice → load_ability → act round-trip. Everything is guarded: any
        # failure just leaves the plain alphabetical menu with no auto-reveal.
        # See app/agent/ability_router.py.
        _ability_route = {"ranked": [], "starred": [], "reveal": None, "reveal_score": None}
        try:
            from app.agent.ability_router import route_abilities as _route_ab, message_text as _rt_text
            from app.abilities import ui_catalog as _ui_cat0
            _msg_txt = _rt_text(user_message)
            if _msg_txt.strip():
                _cat0 = (_ui_cat0() or {}).get("abilities", {})
                _cands, _seen_ab = [], set()
                for _tn in tools:
                    _aid0 = _tm_ability_for_tool(_tn)
                    if not _aid0 or _aid0 in _seen_ab:
                        continue
                    if _tm_ability_revealed(_aid0, _agent_ability_modes, _active_ability_set,
                                            _suppressed_ability_set, ability_default=_agent_discovery_default):
                        continue
                    _seen_ab.add(_aid0)
                    _m0 = _cat0.get(_aid0, {})
                    _cands.append({
                        "id": _aid0,
                        "name": _m0.get("display_name") or _aid0,
                        "desc": _m0.get("skill_summary") or _m0.get("description") or "",
                        "tools": _m0.get("tools") or [],
                    })
                if _cands:
                    _ability_route = await _route_ab(_msg_txt, _cands)
        except Exception as _rterr:
            logger.debug("ability routing skipped: %s", _rterr)

        # Auto-reveal: silently activate the single strongest match, mirroring
        # load_ability (persist active-ability + skill so the per-iteration schema
        # build sends its tools this turn), and inline its how-to so instructions
        # and tools arrive together — not a turn apart.
        # The reveal body is turn-specific and is therefore appended only to the
        # late turn layer. Identical onboarding prompts still produce identical
        # reveals without weakening first-turn ability routing.
        _reveal_id = _ability_route.get("reveal")
        if _reveal_id and _reveal_id not in _active_ability_set:
            try:
                from app.abilities import ability_feature_with_skill as _afs
                from app.agent.ability_skills import _skill_from_feature as _sff
                _sk_body, _sk_handle = "", ""
                _feat = _afs(_reveal_id)
                if _feat:
                    _sk = _sff(_feat, _reveal_id)
                    if _sk:
                        _sk_body = _sk.get("body") or ""
                        _sk_handle = _sk.get("handle") or ""
                if session_id:
                    await db.set_session_active_ability(session_id, _reveal_id, True)
                    if _sk_handle:
                        try:
                            await db.set_session_active_skill(session_id, _sk_handle, True)
                        except Exception:
                            pass
                _active_ability_set.add(_reveal_id)
                _active_ability_names.append(_reveal_id)
                if _sk_body:
                    _turn_system_parts.append("# [AUTO-LOADED ABILITY]\n" + _sk_body)
                _rv_score = _ability_route.get("reveal_score")
                yield {"type": "pipeline", "level": "pipeline",
                       "step": "ability_auto_revealed", "ability_id": _reveal_id,
                       "score": round(_rv_score, 3) if isinstance(_rv_score, (int, float)) else None}
                logger.info("auto-revealed ability %s (score=%s) for session %s",
                            _reveal_id, _rv_score, str(session_id)[:12])
            except Exception as _rverr:
                logger.debug("auto-reveal of %s failed: %s", _reveal_id, _rverr)

        # ── Agent suggestions (proactive delegation hint, best-effort) ──
        # If this agent can delegate, rank the user's saved specialist agents
        # against the message and surface the strong matches up front — the PUSH
        # counterpart to the pull-only list_delegatable_agents tool. Gated so it
        # only appears for orchestrators, and only when a specialist actually fits.
        try:
            _can_delegate = any(t in tools for t in (
                "list_delegatable_agents", "delegate_to_agent",
                "delegate_task_to_agent", "spawn_agent"))
            if _can_delegate and _ability_route is not None:
                from app.agent.ability_router import suggest_agents as _suggest_agents, message_text as _rt_text2
                _msg_txt2 = _rt_text2(user_message)
                if _msg_txt2.strip():
                    _tmpls = await db.list_agent_templates(include_admin=True)
                    _tmpl_cands = [{
                        "id": _t.get("id"),
                        "name": _t.get("name") or _t.get("id"),
                        "desc": _t.get("trigger_description") or _t.get("description") or "",
                    } for _t in (_tmpls or []) if _t.get("id") and not _t.get("is_pipeline")]
                    _agent_hint = await _suggest_agents(
                        _msg_txt2, _tmpl_cands, exclude_id=agent_template_id)
                    if _agent_hint:
                        _turn_system_parts.append(_agent_hint)
        except Exception as _aserr:
            logger.debug("agent suggestions skipped: %s", _aserr)

        # # [TOOLS] — exclude tools whose gating ability is discoverable-and-unloaded
        # (they are reachable only by loading the ability) or session-suppressed.
        _idx_entries = []
        for _tn, _ti in tools.items():
            if _tm_tool_hidden(_tn, _agent_ability_modes, _active_ability_set, _active_set,
                               _suppressed_ability_set, ability_default=_agent_discovery_default):
                continue
            _d = (_ti.handler.__doc__ or "").strip() if hasattr(_ti, "handler") else ""
            # When no attachments are present, move read_attachment to "Load on
            # demand" instead of "Ready to use" so the agent doesn't reach for it
            # as a generic file-reading tool. The # [USER ATTACHMENTS] section
            # already tells the agent to call read_attachment when needed.
            if _tn == "read_attachment" and not (attachment_docs or []):
                _idx_entries.append({
                    "name": _tn,
                    "desc": _d,
                    "mode": "discoverable",
                    "active": False,
                })
                continue
            _idx_entries.append({
                "name": _tn,
                "desc": _d,
                "mode": _tm_resolve(_tn, _agent_tool_modes, _global_tool_defaults),
                "active": _tn in _active_set,
            })
        # Which loaded tools are confirmation-gated in the current policy.
        # Read-only modes gate every mutating tool; write-capable modes gate none.
        # Model-switch tools remain exempt (MODEL-SWITCH-EXEMPT).
        if _permission_policy == 'read_only':
            _ask_names = ({n for n, ti in tools.items() if getattr(ti, 'destructive', False)} | set(effective_destructive)) - _MODEL_SWITCH_TOOLS
        else:
            _ask_names = set()
        _tools_index = _tm_render(_idx_entries, ask_names=_ask_names, denied_names=_denied_names)
        if _tools_index:
            _turn_system_parts.append(_tools_index)

        # # [ABILITIES] — discoverable-and-unloaded abilities (their tools + skill
        # are hidden until load_ability). Derive the enabled host-ability set from
        # the loaded tools, then attach each one's catalog name + summary.
        try:
            _ab_ids = {_tm_ability_for_tool(_tn) for _tn in tools}
            _ab_ids.discard(None)
            _ab_entries = []
            if _ab_ids:
                from app.abilities import ui_catalog as _ui_cat
                _cat_abilities = (_ui_cat() or {}).get("abilities", {})
                for _aid in _ab_ids:
                    if _tm_ability_revealed(_aid, _agent_ability_modes, _active_ability_set,
                                            _suppressed_ability_set,
                                            ability_default=_agent_discovery_default):
                        continue
                    _meta = _cat_abilities.get(_aid, {})
                    _ab_entries.append({
                        "id": _aid,
                        "name": _meta.get("display_name") or _aid,
                        "desc": _meta.get("skill_summary") or _meta.get("description") or "",
                    })
            # Keep the shared menu canonical. Semantic routing is emitted below
            # as a late, request-specific hint.
            _ab_index = _tm_render_abilities(_ab_entries)
            if _ab_index:
                _turn_system_parts.append(_ab_index)
            _starred = [
                a for a in (_ability_route.get("starred") or [])
                if any(e.get("id") == a for e in _ab_entries)
            ]
            if _starred:
                _turn_system_parts.append(
                    "# [ABILITY ROUTING HINT]\nLikely relevant to this request: "
                    + ", ".join(f"`{a}`" for a in _starred)
                    + ". Load one only if it fits."
                )
        except Exception as _aie:
            logger.warning("abilities index build failed: %s", _aie)
    except Exception as _tie:
        logger.warning("tools index build failed: %s", _tie)

    # ── Starter handoff: optional first-write readiness + seeded context ────
    # The Scout normally remains a truly parallel second opinion. Agents that
    # explicitly opt into wait_before_write pay the latency once, before the
    # main model can select its first tool, so the allowed objective/plan/
    # checklist and prior-run relationship are part of that inference.
    try:
        from app.agent.run_scout import (
            artifact_for_turn as _starter_for_turn,
            seeded_context_for_turn as _starter_seeded_context,
        )
        _starter_row = _starter_for_turn(db, parent_interaction_id) or {}
        _starter_settings = (
            _starter_row.get("starter_config")
            if isinstance(_starter_row.get("starter_config"), dict) else {}
        )
        # Never delay read/research inference. If the parallel result already
        # exists, include it now; otherwise the first-write gate below waits.
        _starter_seed = _starter_seeded_context(db, parent_interaction_id)
        if _starter_seed.get("ready"):
            _seed_payload = {
                key: _starter_seed.get(key) for key in (
                    "objective", "relationship", "linked_prior_task_id",
                    "linked_capsule_id", "relationship_confidence",
                    "relationship_reason", "plan", "checklist",
                ) if _starter_seed.get(key) not in (None, "", [], 0.0)
            }
            if _seed_payload:
                _capability_system_parts.append(
                    "## Run Starter handoff\n"
                    "This tool-free parallel intake is advisory run context. "
                    "Use it to stay linked to the right prior task and contract; "
                    "the selected execution mode remains the permission authority.\n"
                    + json.dumps(_seed_payload, ensure_ascii=False, default=str)
                )
    except Exception as _sse:
        logger.debug("run starter context unavailable: %s", _sse)

    # ── Execution-mode guidance: append the active mode's prompt + guardrail ──
    # The chat pill (Ask / Plan / Auto) sets execution_mode; each mode carries its
    # own posture. Plan tells the agent to research freely, think deeply, ask
    # clarifying questions for real ambiguity, and deliver a plan instead of
    # executing. Loaded from app/defaults/app-prompts.json (editable) with an
    # inline fallback so a missing/broken file never breaks a run.
    try:
        _mode_tpl = str(_mode_entry.get("prompt") or "").strip()
        if not _mode_tpl:
            _MODE_FALLBACK = {
                'plan': (
                    "You are in PLAN mode. Research freely with read-only tools without asking "
                    "permission, but do not make changes, even after per-step approval. "
                    "Think deeply and verify against the real code. When a guess could change your "
                    "whole approach, STOP and ask the user a clarifying question, then wait. Collect "
                    "smaller unknowns in an \"Open questions / assumptions\" section, and deliver a "
                    "clear step-by-step plan rather than executing it. "
                    "The Plan checklist describes future implementation and stays unchecked here. "
                    "When the user wants execution, have them approve a switch to a write-capable mode. "
                    "Model switching: If the Model Switcher ability is enabled, use it to right-size "
                    "your planning. Draft on your standard model, then upgrade to a premium model "
                    "(or a vision model, if the work needs sight) for the final planning pass — "
                    "model-switch tools run freely in plan mode, no approval needed. When you deliver "
                    "the plan, include a one-line model recommendation for execution (standard vs "
                    "premium). See the Model Switcher skill for the full protocol."
                ),
                'auto': (
                    "You are in AUTO mode, running autonomously. Tools execute without confirmation, "
                    "including changes to files and state. Do not pause for routine actions — proceed "
                    "and report what you did, calling out any irreversible or high-impact operations."
                ),
                'ask': (
                    "You are in ASK mode. Read and research freely, then deliver a concrete proposal. "
                    "Do not change files, state, or external systems in this mode, even after per-step "
                    "approval; execution requires a write-capable mode. "
                    "Model switching is exempt: if the Model Switcher ability is enabled, you may call "
                    "set_model / use_premium_model / set_effort freely to right-size this task — they "
                    "run without confirmation."
                ),
            }
            _mode_tpl = _MODE_FALLBACK.get(execution_mode, _MODE_FALLBACK['ask'])
        if _mode_tpl:
            _capability_system_parts.append(
                f"## Execution mode: {_mode_entry.get('label') or execution_mode}\n" + _mode_tpl
            )
        _mode_checklist = [
            item.get("label") for item in _accumulated_mode_contract.get("checklist", [])
            if isinstance(item, dict) and item.get("label")
        ]
        if _mode_checklist:
            _capability_system_parts.append(
                "## Accumulated mode completion contract\n"
                "These requirements survive mode changes within this session. Complete every item before finishing:\n"
                + "\n".join(f"- {item}" for item in _mode_checklist)
            )
        if _requires_plan_document:
            _capability_system_parts.append(
                "## Required Plan workspace\n"
                "Before you may finish this turn, you MUST call `present_plan` with both a task-specific "
                "overview and a concrete checklist. This writes the persistent Plan Overview document and "
                "Plan Checklist. A text-only plan does not satisfy this contract. Update the same workspace "
                "as your understanding changes; it will be preserved if the session later switches modes."
            )
        # A plan workspace is session state, not mode-local scratch. Surface its
        # current document/checklist after a Plan→Ask/Auto transition so the
        # executing mode keeps working against the exact same artifacts.
        try:
            from app.chat_components import list_components as _list_chat_components
            _plan_components = await _list_chat_components(user_id, session_id)
            _plan_overview = next((c for c in _plan_components if c.get("id") == "plan-overview"), None)
            _plan_checklist = next((c for c in _plan_components if c.get("id") == "plan-checklist"), None)
            _plan_lines = []
            if _plan_overview:
                _markdown = str((_plan_overview.get("data") or {}).get("markdown") or "").strip()
                if _markdown:
                    _plan_lines.append("### Plan document\n" + _markdown)
            if _plan_checklist:
                _items = (_plan_checklist.get("data") or {}).get("items") or []
                if _items:
                    _plan_lines.append("### Plan checklist\n" + "\n".join(
                        f"- [{'x' if item.get('done') else ' '}] {item.get('label')}"
                        for item in _items if item.get("label")
                    ))
            if _plan_lines:
                _capability_system_parts.append(
                    "## Preserved session plan workspace\n"
                    "This workspace remains authoritative after mode changes. Maintain it and complete its open items.\n\n"
                    + "\n\n".join(_plan_lines)
                )
        except Exception as _pce:
            logger.debug("plan workspace context unavailable: %s", _pce)
    except Exception as _moe:
        logger.warning("execution-mode prompt injection failed: %s", _moe)

    # ── Run-contract prompt block ──
    # Active mechanical contract rules injected so the agent KNOWS its
    # contract — the same visibility pattern as the execution-mode block above.
    # Enforcement happens at the contract_chk node regardless; this only tells
    # the agent what is being enforced.
    try:
        if _contract:
            from app.agent.contracts import contract_prompt_block
            _contract_block = contract_prompt_block(_contract)
            if _contract_block:
                _capability_system_parts.append("## Contract\n" + _contract_block)
    except Exception as _cte:
        logger.warning("contract prompt injection failed: %s", _cte)

    # ── Session message cache ────────────────────────────────────────────
    # Stable hash of the agent's system-prompt layers.  When unchanged the
    # cache holds a byte-identical prefix, so the LLM provider's prompt cache
    # fires (DeepSeek, Anthropic, OpenAI) — ~90 % cheaper input tokens.
    # Includes turn_system_parts so an ability-load or tool-change mid-session
    # invalidates the stale cached prefix.
    _sys_hash = _cache_hash([_shared_system, _capability_system_parts,
                             _agent_system, _turn_system_parts])
    _sc = get_session_cache()
    _canonical_incoming_history = [
        item for item in (history or []) if item.get("role") != "system"
    ]
    _incoming_history_hash = _cache_hash(_canonical_incoming_history)
    _cached = await _sc.get(
        user_id, session_id, _sys_hash, _incoming_history_hash
    )
    if _cached is not None:
        messages = _cached
        # Inject fresh turn-specific context (ability hints, tools index,
        # execution-mode guidance) BEFORE the new user message — the cached
        # prefix stays byte-identical above this point, so prompt-cache fires.
        if _turn_system_parts:
            for _tp in _turn_system_parts:
                if _tp and _tp.strip():
                    messages.append({"role": "system", "content": _tp.strip()})
        # user_message=None (resume-without-nudge) → no trailing user turn; the
        # cached prefix already ends where the conversation really ended.
        if user_message is not None:
            messages.append({"role": "user", "content": user_message})
    else:
        # Cache miss — build from scratch as before
        messages = _build_layered_messages(
            shared_system=_shared_system,
            capability_parts=_capability_system_parts,
            agent_system=_agent_system,
            turn_parts=_turn_system_parts,
            history=history,
            user_message=user_message,
        )

    async def _store_validated_session_cache() -> None:
        canonical_history = [
            item for item in messages if item.get("role") != "system"
        ]
        await _sc.set(
            user_id,
            session_id,
            messages,
            _sys_hash,
            _cache_hash(canonical_history),
        )

    # ── Context Control: surface the live context-fill signal to the agent ──────
    # Ask whichever context-management strategy is enabled for this agent (the
    # default is Context Control) for its fill gauge, then inject a one-block
    # status into the system message so the agent can feel itself filling up, and
    # emit a pipeline event so the UI can show the same number. No strategy
    # enabled / disabled → no-op. Never let a failure here break the run.
    try:
        from app.abilities import context_strategy_for_agent
        _cc_mod = await context_strategy_for_agent(agent_id)
        if _cc_mod is not None:
            _cc_get = getattr(_cc_mod, "CONTEXT_SETTINGS", None)
            _cc_status = getattr(_cc_mod, "CONTEXT_STATUS", None)
            _cc = await _cc_get(db, agent_id, session_id, user_id) if _cc_get else {}
            _st = _cc_status(messages, _cc) if (_cc.get("enabled") and _cc_status) else None
            if _st:
                _cc_line = _st["line"]
                # Insert as a separate system message right before the user turn
                # so messages[0] (core system prompt) and all frozen compaction
                # cars stay byte-identical across turns, enabling provider
                # prefix caching (DeepSeek, Anthropic, etc.).
                messages.insert(len(messages) - 1, {"role": "system", "content": _cc_line})
                yield {"type": "pipeline", "level": "pipeline", "step": "context_status",
                       "tokens": _st["tokens"], "limit": _st["limit"],
                       "pct": _st["pct"], "enabled": True}
    except Exception as _cce:
        logger.warning("context_control signal skipped: %s", _cce)

    turn_count = 0
    original_max_turns = max_turns  # the configured block size; used to rearm at each ceiling
    last_extension_at = 0           # ceiling turn at which we last extended (0 = not yet)
    # System pipeline sessions must obey their configured ceiling. They have no
    # human at the keyboard during a runaway turn, so keyword-based extensions
    # are unsafe and can silently defeat the limit.
    _allow_turn_extension = not (
        session_id.startswith("optimizer-") or session_id.startswith("closer-")
    )
    empty_retry_used = False        # safety net: one retry per session for an empty LLM reply
    plan_document_retry_used = False  # one mechanical retry when Plan omitted present_plan
    plan_document_updated = False
    asst_id: Optional[str] = None   # latest persisted assistant row; Manager anchor
    _tool_name_streak = 0        # consecutive same-tool-name calls (diff args)
    _last_tool_streak_name = ""  # which tool the streak is counting

    try:
        # Build loop config — drives per-node enable/disable at runtime.
        # If caller supplied one (e.g. tests), use it directly; otherwise
        # parse the agent's stored loop_logic (backward-compat: flat array
        # = all nodes enabled, preserving current behavior exactly).
        if loop_config is None:
            loop_config = LoopConfig.from_agent(_agent_rec)

        # effective_destructive / auto_confirm / concurrent_limit were computed
        # earlier (right after tools loaded) so the # [TOOLS] index could annotate
        # the gated/blocked tools; they are reused here for the runtime gate.

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
        _tool_total_counts = _collections.Counter()  # tool name -> calls requested
        _tool_result_counts = _collections.Counter() # (tool, normalized result hash) -> count
        _no_progress_tools = set()
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
        _agent_meta = _agent_rec.get("metadata") if _agent_rec else {}
        if isinstance(_agent_meta, str):
            try:
                _agent_meta = json.loads(_agent_meta or "{}")
            except (TypeError, json.JSONDecodeError):
                _agent_meta = {}
        if not isinstance(_agent_meta, dict):
            _agent_meta = {}
        _tool_call_budgets = _agent_meta.get("tool_call_budgets") or {}
        if not isinstance(_tool_call_budgets, dict):
            _tool_call_budgets = {}
        try:
            _MAX_NO_PROGRESS_RESULTS = max(
                0, int(_agent_meta.get("max_no_progress_results") or 0)
            )
        except (TypeError, ValueError):
            _MAX_NO_PROGRESS_RESULTS = 0
        # data/config/debug-config.json overrides win over app-settings for the
        # debug knobs (max_tool_calls / max_wall_seconds / max_identical_tool_calls).
        try:
            from app.admin.debug_config import debug_overrides as _dbg_over
            _ov = _dbg_over()
            if _ov:
                _gs = {**(_gs or {}), **_ov}
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
        # Same-tool-name streak limit (SAME tool, DIFFERENT args) — a much SOFTER
        # signal than identical-args repeats. Legitimate bulk work hits one tool
        # many times with different args (reading a dozen files, configuring all of
        # an agent's abilities/tools, scraping page after page). The identical-args
        # guard above already catches true loops; this one must only catch real
        # thrashing, so it gets its OWN, far more lenient ceiling and is NOT gated by
        # the (low) identical-call limit. Resolution: per-agent ▸ global ▸ a generous
        # derived default. 0 = disabled.
        _raw_streak = _agent_rec.get("max_tool_name_streak") if _agent_rec else None
        if _raw_streak is not None and int(_raw_streak) > 0:
            _MAX_TOOLNAME_STREAK = max(2, int(_raw_streak))
        elif _gs and _gs.get("max_tool_name_streak") and int(_gs["max_tool_name_streak"]) > 0:
            _MAX_TOOLNAME_STREAK = max(2, int(_gs["max_tool_name_streak"]))
        else:
            # Default well above any legitimate bulk-config / bulk-read sequence.
            _MAX_TOOLNAME_STREAK = max(_MAX_IDENTICAL_CALLS * 3, 30) if _MAX_IDENTICAL_CALLS > 0 else 30
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

        # ── Integrated Manager watchdog ─────────────────────────────────────
        # Mechanical sensors remain in this loop. Suspicious events are handed
        # to the Manager as compact JSON, and completed background verdicts are
        # drained into the active in-memory conversation before the next model
        # turn (in addition to the durable system:manager DB note).
        from app.agent.run_health import RunHealthTracker, health_event
        _wd_cfg = _manager_cfg.get("watchdog") or {}
        _health = RunHealthTracker(
            error_window=(
                _wd_cfg.get("error_window", 8)
                if isinstance(_wd_cfg, dict) else 8
            )
        )
        _manager_tasks: set = set()

        def _deliver_manager_feedback(task: "asyncio.Task") -> None:
            """Deliver a completed advisory verdict at the next safe loop await."""
            _manager_tasks.discard(task)
            try:
                verdict = task.result()
            except Exception:
                verdict = None
            try:
                from app.agent.manager import manager_feedback_message
                note = manager_feedback_message(verdict)
            except Exception:
                note = ""
            if note:
                # The event loop is single-threaded: callbacks run between await
                # boundaries, so this cannot interleave with synchronous list work.
                # If an LLM request is already streaming, the note naturally lands
                # in the following inference; otherwise it is included immediately.
                messages.append({"role": "system", "content": note})

        _default_manager_kind_caps = {
            "plan_gate": 1,
            "edit_gate": 4,
            "watchdog": 3,
            "commit_gate": 1,
        }

        def _manager_kind_cap(kind: str) -> int:
            configured = _manager_cfg.get("max_checks_by_kind")
            raw_cap = (
                configured.get(kind)
                if isinstance(configured, dict) and kind in configured
                else _default_manager_kind_caps.get(kind, 1)
            )
            try:
                return max(0, int(raw_cap))
            except (TypeError, ValueError):
                return _default_manager_kind_caps.get(kind, 1)

        def _reserve_manager_check(kind: str) -> Optional[tuple]:
            """Atomically reserve the overall lane and this trigger's lane.

            This function contains no await and runs on the single event-loop
            thread, so background and blocking checks cannot observe a partial
            reservation. Separate lanes keep frequent edit reviews from
            consuming the plan, watchdog, or commit allowance.
            """
            if not loop_config.is_enabled("manager_chk"):
                return None
            try:
                cap = max(1, int(_manager_cfg.get("max_checks") or 9))
            except (TypeError, ValueError):
                cap = 9
            kind_cap = _manager_kind_cap(kind)
            used = int(_manager_state.get("checks_used") or 0)
            used_by_kind = _manager_state.setdefault("checks_used_by_kind", {})
            kind_used = int(used_by_kind.get(kind) or 0)
            if used >= cap or kind_used >= kind_cap:
                return None
            used += 1
            kind_used += 1
            _manager_state["checks_used"] = used
            used_by_kind[kind] = kind_used
            return used, kind_used, kind_cap

        def _release_manager_check(kind: str, reservation: tuple) -> None:
            """Release a reservation when no Manager task could be created."""
            check_index, kind_check_index, _ = reservation
            _manager_state["checks_used"] = max(
                0, min(int(_manager_state.get("checks_used") or 0), check_index) - 1
            )
            used_by_kind = _manager_state.setdefault("checks_used_by_kind", {})
            used_by_kind[kind] = max(
                0, min(int(used_by_kind.get(kind) or 0), kind_check_index) - 1
            )

        def _launch_manager_check(kind: str, *, anchor_id: Optional[str], extra: str) -> bool:
            if not anchor_id:
                return False
            reservation = _reserve_manager_check(kind)
            if reservation is None:
                return False
            check_index, kind_check_index, kind_cap = reservation
            task = _schedule_manager_check(
                kind,
                user_id=user_id, session_id=session_id,
                agent_id=agent_id, agent_rec=_agent_rec,
                final_asst_id=anchor_id,
                parent_interaction_id=parent_interaction_id,
                db=db, channel=channel,
                execution_mode=execution_mode,
                extra=extra,
                max_checks=_manager_cfg.get("max_checks"),
                check_index=check_index,
                kind_max_checks=kind_cap,
                kind_check_index=kind_check_index,
            )
            if task is None:
                _release_manager_check(kind, reservation)
                return False
            _manager_tasks.add(task)
            task.add_done_callback(_deliver_manager_feedback)
            return True

        def _launch_health_watchdog(event: Dict[str, Any], *, anchor_id: Optional[str]) -> bool:
            if (not anchor_id or not _manager_state.get("watchdog_enabled")
                    or not isinstance(_wd_cfg, dict)):
                return False
            cooldown = int(_wd_cfg.get("cooldown_turns") or 0)
            if not _health.allow_watchdog(turn=turn_count, cooldown_turns=cooldown):
                return False
            return _launch_manager_check(
                "watchdog", anchor_id=anchor_id,
                extra=json.dumps(event, sort_keys=True, default=str)[:3000],
            )

        async def _run_blocking_manager_check(
            kind: str, *, anchor_id: Optional[str], extra: str,
        ) -> Optional[Dict[str, Any]]:
            if not anchor_id:
                return None
            reservation = _reserve_manager_check(kind)
            if reservation is None:
                return None
            check_index, kind_check_index, kind_cap = reservation
            try:
                from app.agent.manager import run_manager_check
                return await run_manager_check(
                    kind,
                    user_id=user_id, session_id=session_id,
                    agent_id=agent_id, agent_rec=_agent_rec,
                    final_asst_id=anchor_id,
                    parent_interaction_id=parent_interaction_id,
                    db=db, channel=channel,
                    execution_mode=execution_mode,
                    extra=extra,
                    max_checks=_manager_cfg.get("max_checks"),
                    check_index=check_index,
                    kind_max_checks=kind_cap,
                    kind_check_index=kind_check_index,
                )
            except Exception:
                return None

        def _commit_manager_context(
            tool_name: str, tool_args: Dict[str, Any],
        ) -> str:
            """Build commit evidence from safe run metadata, never raw output."""
            changed_paths = sorted(
                str(path) for path in (_manager_state.get("changed_paths") or set())
                if path
            )
            edit_events = list(_manager_state.get("edit_events") or [])
            verification_events = list(
                _manager_state.get("verification_events") or []
            )
            evidence = {
                "tool": tool_name,
                "intended_commit_arguments": tool_args,
                "change_inventory": {
                    "changed_path_count": len(changed_paths),
                    "changed_paths": changed_paths[:100],
                    "successful_edit_count": len(edit_events),
                    "recent_edits": edit_events[-12:],
                    "truncated": len(changed_paths) > 100,
                },
                "verification_summary": {
                    "attempted": len(verification_events),
                    "passed": sum(
                        1 for event in verification_events if event.get("success")
                    ),
                    "failed": sum(
                        1 for event in verification_events
                        if not event.get("success")
                    ),
                    "recent_checks": verification_events[-12:],
                },
            }
            return json.dumps(evidence, sort_keys=True, default=str)[:6000]

        async def _evaluate_manager_gates(
            tool_name: str,
            tool_args: Dict[str, Any],
            *,
            tool_call_id: str,
            anchor_id: Optional[str],
            current_mode: str,
        ) -> Optional[Dict[str, Any]]:
            """Run/schedule the configured gates for one otherwise-valid call."""
            from app.agent.contracts import is_commit_tool, is_edit_tool

            # Starter wait_before_write is independent of Manager trigger
            # policy. Reads remain lightning-fast and parallel. On the first
            # proposed edit, wait for the one-shot, inject its allowed durable
            # fields, and ask the main model to reconsider that edit once with
            # the newly available task linkage/plan/checklist.
            if is_edit_tool(tool_name) and not _manager_state.get("starter_write_checked"):
                _manager_state["starter_write_checked"] = True
                try:
                    from app.agent.run_scout import (
                        artifact_for_turn as _starter_for_write,
                        await_write_ready as _await_starter_for_write,
                    )
                    _starter_row = _starter_for_write(db, parent_interaction_id) or {}
                    _starter_cfg = (_starter_row.get("starter_config")
                                    if isinstance(_starter_row.get("starter_config"), dict)
                                    else {})
                    if _starter_cfg.get("wait_before_write"):
                        _seed = await _await_starter_for_write(db, parent_interaction_id)
                        if _seed.get("ready"):
                            _payload = {
                                key: _seed.get(key) for key in (
                                    "objective", "relationship", "linked_prior_task_id",
                                    "linked_capsule_id", "relationship_confidence",
                                    "relationship_reason", "plan", "checklist",
                                ) if _seed.get(key) not in (None, "", [], 0.0)
                            }
                            if _payload:
                                messages.append({
                                    "role": "system",
                                    "content": (
                                        "Run Starter context became ready before the first write. "
                                        "Re-evaluate the proposed edit against this context; the "
                                        "execution mode still controls permissions.\n"
                                        + json.dumps(_payload, ensure_ascii=False, default=str)
                                    ),
                                })
                                return {
                                    "verdict": "revise",
                                    "reason": "starter_context_ready",
                                    "feedback": (
                                        "The Run Starter handoff is now available. Review its "
                                        "objective, prior-task linkage, plan, and checklist, then "
                                        "retry the edit if it still fits."
                                    ),
                                }
                except Exception as exc:
                    logger.debug("run starter first-write gate failed open: %s", exc)

            if not loop_config.is_enabled("manager_chk") or not _manager_cfg:
                return None

            affected_paths = [
                str(value) for key, value in tool_args.items()
                if key.lower() in {"path", "file", "filename", "target", "destination"}
                and isinstance(value, (str, int))
            ]
            context_payload = {
                "tool_call_id": tool_call_id,
                "tool": tool_name,
                "args": tool_args,
                "argument_hash": "sha256:" + hashlib.sha256(
                    json.dumps(tool_args, sort_keys=True, separators=(",", ":"), default=str)
                    .encode("utf-8")
                ).hexdigest(),
                "affected_paths": affected_paths[:30],
                "generation": parent_interaction_id or "",
                "turn_reservation_key": turn_reservation_key or "",
            }
            if turn_reservation_key:
                from app.agent.turn_reservations import stable_key as _reservation_key
                context_payload["tool_reservation_id"] = _reservation_key(
                    "tool", turn_reservation_key, tool_name, tool_args,
                )
            context = json.dumps(context_payload, default=str)
            if len(context) > 6000:
                context_payload["args"] = {"preview": str(tool_args)[:3000]}
                context_payload["arguments_truncated"] = True
                context = json.dumps(context_payload, default=str)
            if is_edit_tool(tool_name):
                # Plan gate is once per run. Edit gate is per edit, as its name
                # promises, rather than sharing the old first-edit flag.
                plan_mode = _manager_cfg.get("plan_gate")
                if not _manager_state.get("plan_gate_done"):
                    if plan_mode == "blocking":
                        verdict = await _run_blocking_manager_check(
                            "plan_gate", anchor_id=anchor_id, extra=context,
                        )
                        if verdict and verdict.get("verdict") in ("block", "revise"):
                            return verdict
                        if verdict and verdict.get("verdict") == "approve":
                            _manager_state["block_count"] = 0
                        _manager_state["plan_gate_done"] = True
                    elif plan_mode == "async":
                        _manager_state["plan_gate_done"] = True
                        _launch_manager_check("plan_gate", anchor_id=anchor_id, extra=context)

                edit_mode = _manager_cfg.get("edit_gate")
                if edit_mode == "blocking":
                    verdict = await _run_blocking_manager_check(
                        "edit_gate", anchor_id=anchor_id, extra=context,
                    )
                    if verdict and verdict.get("verdict") in ("block", "revise"):
                        return verdict
                    if verdict and verdict.get("verdict") == "approve":
                        _manager_state["block_count"] = 0
                elif edit_mode == "async":
                    _launch_manager_check("edit_gate", anchor_id=anchor_id, extra=context)

            if is_commit_tool(tool_name):
                context = _commit_manager_context(tool_name, tool_args)
                commit_mode = _manager_cfg.get("commit_gate")
                if commit_mode == "blocking":
                    verdict = await _run_blocking_manager_check(
                        "commit_gate", anchor_id=anchor_id, extra=context,
                    )
                    if verdict and verdict.get("verdict") in ("block", "revise"):
                        return verdict
                    if verdict and verdict.get("verdict") == "approve":
                        _manager_state["block_count"] = 0
                elif commit_mode == "async":
                    _launch_manager_check("commit_gate", anchor_id=anchor_id, extra=context)
            return None

        async def _beat() -> None:
            nonlocal _last_heartbeat
            _hb_now = time.monotonic()
            if (_hb_now - _last_heartbeat) >= _HEARTBEAT_INTERVAL:
                _last_heartbeat = _hb_now
                try:
                    # Offloaded — fires during streaming; must not freeze the loop.
                    await db_offload(lambda: db.run_state_heartbeat(session_id))
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
                await _check_interrupt(session_id, interrupt_event, db=db)

            # Prove liveness between turns (covers long tool executions).
            await _beat()

            turn_count += 1
            if agent_id:
                await db_offload(lambda: db.increment_agent_turn_count(agent_id))

            # ── Manager watchdog (periodic, async) ──
            # Every N turns (metadata['manager'].watchdog.every_n_turns), fire a
            # background on-track check. Advisory only — never blocks the turn;
            # a verdict of off_track/stuck lands as a self-note the agent's next
            # turn sees. Gated on the run_manager app function inside the check.
            if (loop_config.is_enabled("manager_chk")
                    and _manager_state.get("watchdog_enabled")):
                _wd_cfg = _manager_cfg.get("watchdog") or {}
                _every = _wd_cfg.get("every_n_turns") if isinstance(_wd_cfg, dict) else 0
                if _every and turn_count % _every == 0:
                    _launch_manager_check(
                        "watchdog", anchor_id=asst_id,
                        extra=json.dumps(health_event("periodic", turn=turn_count)),
                    )

            # Reset stream chunk tracker for new turn
            first_stream_chunk_state[0] = True

            # ── Pipeline: turn start ──
            yield {"type": "pipeline", "level": "pipeline",
                   "step": "turn_start", "turn": turn_count, "max_turns": max_turns}

            # Ask for permission to continue when the agent reaches the configured turn ceiling.
            # Rearms automatically after each granted extension (last_extension_at tracks the
            # ceiling at which we last extended, so asking fires again at each new ceiling).
            if (_allow_turn_extension and max_turns > 0
                    and loop_config.is_enabled("permission_chk")
                    and turn_count == max_turns and last_extension_at != max_turns):
                fr = get_prompt_fragments()
                permission_message = (fr.get("turn_permission_request") or "").strip()
                if permission_message:
                    messages.append({"role": "system", "content": permission_message})

            # Check if user has granted permission (looks at their most recent message).
            # Only active at the current ceiling, before we have already extended at that ceiling.
            if (_allow_turn_extension and max_turns > 0
                    and loop_config.is_enabled("permission_chk")
                    and turn_count >= max_turns and last_extension_at < max_turns):
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
            from app.tools.tool_modes import (
                is_sent as _tm_is_sent, tool_hidden_by_ability as _tm_tool_hidden,
            )
            if session_id:
                try:
                    # Offloaded + concurrent (see prep note above): this re-read
                    # fires every turn iteration to pick up tools loaded mid-run.
                    # Keep it off the event loop so the live LLM stream never stalls.
                    _now_tools, _now_abils, _now_suppressed = await asyncio.gather(
                        db_offload(lambda: db.get_session_active_tools(session_id)),
                        db_offload(lambda: db.get_session_active_abilities(session_id)),
                        db_offload(lambda: db.get_session_suppressed_abilities(session_id)),
                    )
                    _active_set_now = set(_now_tools)
                    _active_ability_now = set(_now_abils)
                    _suppressed_ability_now = set(_now_suppressed)
                except Exception:
                    _active_set_now = set(_active_tool_names)
                    _active_ability_now = set(_active_ability_names)
                    _suppressed_ability_now = set(_suppressed_ability_names)
            else:
                _active_set_now = set(_active_tool_names)
                _active_ability_now = set(_active_ability_names)
                _suppressed_ability_now = set(_suppressed_ability_names)

            # ── Tool-defs cache ─────────────────────────────────────────────
            # Tool schemas are identical across turns for the same agent + tool
            # set.  Cache them to avoid re-iterating over every loaded tool and
            # re-building full JSON-Schema objects each turn.
            _td_cache = get_tool_defs_cache()
            _td_key = compute_tool_defs_cache_key(
                agent_id or "",
                _active_set_now,
                _active_ability_now,
                _suppressed_ability_now,
            )
            _cached_defs = await _td_cache.get(_td_key)
            if _cached_defs is not None:
                tool_definitions = _cached_defs
            else:
                tool_definitions = []
                for name, info in tools.items():
                    # Withhold tools of a discoverable-and-unloaded ability (re-checked
                    # each iteration so a mid-turn load_ability reveals them next pass),
                    # or of an ability the user suppressed from the chat panel.
                    if _tm_tool_hidden(name, _agent_ability_modes, _active_ability_now, _active_set_now,
                                       _suppressed_ability_now, ability_default=_agent_discovery_default):
                        continue
                    if not _tm_is_sent(name, _agent_tool_modes, _active_set_now, _global_tool_defaults):
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
                tool_definitions.sort(key=lambda td: td["function"]["name"])
                await _td_cache.set(_td_key, tool_definitions)

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

            def _build_meta(role: str, in_tok: int=None, out_tok: int=None,
                            cost: float=None, message_phase: str="pending") -> str:
                meta = {
                    "provider": provider_name,
                    "model": model_name,
                    "effort": reasoning_effort,
                    "turn": turn_count,
                    "duration_ms": int((time.time() - llm_start_time) * 1000),
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "role": role,
                    "streaming": True,
                    "message_phase": message_phase,
                }
                if cost is not None:
                    meta["cost"] = cost
                return json.dumps(meta)

            async def _persist_stream_progress(force: bool = False) -> None:
                """Commit the current assistant snapshot before browser emission."""
                nonlocal streaming_asst_id, _last_stream_persist
                # Mid-stream interrupt: lets the Stop button halt a long single
                # completion, not just at turn boundaries. Throttled to the
                # persist cadence so it costs ~one extra flag read per interval.
                if loop_config.is_enabled("interrupt_chk") and not force:
                    now_i = time.monotonic()
                    if streaming_asst_id is None or (now_i - _last_stream_persist) >= _STREAM_PERSIST_INTERVAL:
                        await _check_interrupt(session_id, interrupt_event, db=db)
                if streaming_asst_id is None:
                    raise RuntimeError("streaming assistant row was not created before LLM output")
                now = time.monotonic()
                if not force and (now - _last_stream_persist) < _STREAM_PERSIST_INTERVAL:
                    return
                _last_stream_persist = now
                _content_snapshot = collected_content
                _sid = streaming_asst_id
                await db_offload(lambda: db.update_interaction(_sid, content=_content_snapshot))
                # Keep the liveness heartbeat fresh during a long single-turn stream.
                await _beat()

            if loop_config.is_enabled("interrupt_chk"):
                await _check_interrupt(session_id, interrupt_event, db=db)

            # ── Process-memory guard ──────────────────────────────────────────
            # Before every LLM call, check the Python process RSS.  If it exceeds
            # MEM_COMPACT_MB, try to compact the session first.  If it exceeds
            # MEM_KILL_MB, abort this run gracefully to avoid the whole server OOM.
            _mem_mb = _process_memory_mb()
            if _mem_mb > MEM_KILL_MB:
                logger.warning(
                    "memory guard: session %s at %d MB (>%d) — aborting run",
                    session_id[:12], _mem_mb, MEM_KILL_MB,
                )
                yield {"type": "pipeline", "level": "pipeline",
                       "step": "mem_guard_kill", "mem_mb": _mem_mb,
                       "limit_mb": MEM_KILL_MB}
                stall_stop_msg = (
                    "I stopped because the system memory got too full — this task was "
                    "using too much RAM. Please try breaking it into smaller steps."
                )
                break

            if _mem_mb > MEM_COMPACT_MB:
                logger.warning(
                    "memory guard: session %s at %d MB (>%d) — force compacting",
                    session_id[:12], _mem_mb, MEM_COMPACT_MB,
                )
                yield {"type": "pipeline", "level": "pipeline",
                       "step": "mem_guard_compact", "mem_mb": _mem_mb,
                       "limit_mb": MEM_COMPACT_MB}
                try:
                    from app.abilities import context_strategy_for_agent
                    _cc_mod = await context_strategy_for_agent(agent_id)
                    _cc_get = getattr(_cc_mod, "CONTEXT_SETTINGS", None) if _cc_mod else None
                    _cc_compact = getattr(_cc_mod, "CONTEXT_COMPACT", None) if _cc_mod else None
                    _cc_s = await _cc_get(db, agent_id, session_id, user_id) if _cc_get else {}
                    if (_cc_s.get("enabled") and _cc_s.get("compaction_enabled", True)
                            and _cc_compact):
                        _result = await _cc_compact(db, user_id, session_id, _cc_s)
                        if _result:
                            logger.info(
                                "memory guard: compaction folded %d rows for session %s",
                                _result.get("summarised_rows", 0), session_id[:12],
                            )
                except Exception as _mce:
                    logger.warning("memory guard: compaction attempt failed: %s", _mce)

            # ── Per-turn model hot-swap ──
            # Re-read the session override here (inside the turn loop) so a model
            # picked in the chat footer — or switched by the Model Switcher ability
            # via set_model() — takes effect on the very next tool-call turn without
            # needing Stop/Continue or a new user message. Only the model + effort
            # override are re-read; the full provider/config resolution (user + agent)
            # is unchanged from the initial apply_provider_for_run above.
            # One lightweight DB read per turn; no-op when the override hasn't moved.
            if session_id:
                try:
                    _new_config = await apply_provider_for_run(
                        user_id, _agent_rec, session_id, apply_env=False)
                    _new_model = _runtime_model(_new_config)
                    if _new_model:
                        llm_config = _new_config
                        model_name = _new_model
                        provider_name = llm_config.get("provider") or provider_name
                        reasoning_effort = (llm_config.get("reasoning_effort") or "").strip().lower() or None
                        llm_client = _client_for(llm_config)
                except Exception:
                    pass  # best-effort — never break the turn on a model re-read

            # ── Pipeline: LLM call start ──
            yield {"type": "pipeline", "level": "pipeline",
                   "step": "llm_call_start", "model": model_name,
                   "effort": reasoning_effort,
                   "message_count": len(messages),
                   "turn": turn_count}

            # ── Stream the LLM response ──
            llm_start = time.time()

            collected_content = ""
            collected_tool_calls: Dict[int, Any] = {}
            # DeepSeek thinking-mode reasoning arrives as delta.reasoning_content
            # and MUST be replayed with the assistant message on later turns —
            # the API 400s requests whose assistant turns lack it.
            collected_reasoning = ""
            streaming_asst_id = None   # new in-progress assistant row per step
            _last_stream_persist = 0.0
            input_tokens = None
            output_tokens = None
            llm_cost = None
            cached_input_tokens = 0
            cache_write_tokens = 0
            uncached_input_tokens = None
            reasoning_tokens = 0

            # DB-first streaming contract: no provider output is requested until
            # the local transcript has a durable assistant destination.  Hybrid
            # mode commits this row and its remote-sync outbox marker together.
            try:
                streaming_asst_id = await db_offload(lambda: db.insert_interaction(
                    user_id, session_id, role="assistant", content="",
                    parent_id=parent_interaction_id, channel=channel,
                    metadata=_build_meta("assistant", input_tokens, output_tokens, llm_cost),
                    sender_id=agent_id, receiver_id=user_id, status="streaming",
                ))
                await db_offload(lambda: db.run_state_set_assistant(session_id, streaming_asst_id))
                # Sequence the row at creation, before opening the provider
                # stream. A connection failure before the first token must not
                # leave a permanently unsequenced error interaction.
                yield {
                    "type": "db", "level": "db", "op": "insert_interaction",
                    "role": "assistant", "tool_name": None,
                    "id": streaming_asst_id,
                }
            except Exception as _persist_error:
                logger.error("cannot start LLM stream without durable assistant row: %s", _persist_error)
                yield {"type": "error", "level": "agent",
                       "message": f"Could not save the assistant response before streaming: {_persist_error}"}
                return

            # ── Single LLM call ──
            # The agent runs ONE resolved model: the agent's default, the model the
            # user picked for THIS chat (per the per-turn session-override re-read
            # above), or the model resolved by apply_provider_for_run at loop start.
            # Parallel model racing was removed — there is exactly one brain per turn.

            # ── Snapshot the full schema sent to the LLM (messages + tools) ──
            # Persisted in the output JSON so the UI can show "what the model saw"
            # when the user clicks the 3-line icon above an agent bubble.
            _sent_messages = messages.copy() if messages else []
            _sent_tools = [td for td in tool_definitions] if tool_definitions else None

            _create_kwargs = dict(
                model=model_name,
                messages=_strip_reasoning_content_for_provider(messages, model_name, provider_name),
                tools=tool_definitions if tool_definitions else None,
                tool_choice="auto" if tool_definitions else None,
                temperature=0.0,
                max_tokens=_max_output_tokens(),
                stream=True,
                stream_options={"include_usage": True},
            )
            # Apply the per-session reasoning-effort hint, if any. Sent via the
            # OpenRouter-normalised `extra_body.reasoning.effort`; a provider that
            # rejects it (or the level) gets the call retried once without it, so an
            # unsupported effort never breaks the turn.
            if reasoning_effort:
                _create_kwargs["extra_body"] = {"reasoning": {"effort": reasoning_effort}}
            # One-shot legacy-history recovery per turn: DeepSeek thinking-mode
            # replay rejection (assistant turns missing their original
            # reasoning_content) triggers a single sanitized retry below.
            _reasoning_sanitized = False
            # Bound the stream-OPEN call (see _stream_open_seconds). A hung connect
            # otherwise freezes the turn indefinitely; the per-chunk stall guard
            # below only covers reads AFTER the stream object exists.
            _open_s = _stream_open_seconds()
            # DIAGNOSTIC (REMOVE-WHEN latency diagnosis done): measure WHY the first
            # token is slow. Logs the prompt size (proxy for prefill cost) and the
            # time the provider takes just to RETURN the stream object (headers) vs.
            # the time from there to the first text chunk. A big open-time = provider
            # queueing/prefill before any byte; a big prompt = our prompt is too big.
            _llm_diag_on = (os.environ.get("WEBAGENT_PERF_TRACE", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
            _t_open0 = time.monotonic()
            try:
                try:
                    stream = await _open_stream(_create_kwargs, _open_s, llm_client)
                    if _llm_diag_on:
                        try:
                            _msg_chars = sum(len(str(m.get("content") or "")) for m in messages)
                            _sys_chars = sum(len(str(m.get("content") or "")) for m in messages if m.get("role") == "system")
                            from app.agent.diagnostics import record as _diag
                            _diag("info", "perf",
                                  f"llm_stream_open +{int((time.monotonic()-_t_open0)*1000)}ms",
                                  source="llm_latency",
                                  detail={"model": model_name,
                                          "open_ms": int((time.monotonic()-_t_open0)*1000),
                                          "msg_count": len(messages),
                                          "prompt_chars": _msg_chars,
                                          "system_chars": _sys_chars,
                                          "tool_count": len(tool_definitions or []),
                                          "shared_core_hash": _cache_hash(_shared_system),
                                          "capability_hash": _cache_hash(_capability_system_parts),
                                          "agent_context_hash": _cache_hash(_agent_system),
                                          "tool_schema_hash": _cache_hash(tool_definitions or []),
                                          "reasoning_effort": reasoning_effort or "none"},
                                  session_id=session_id, agent_id=agent_id, user_id=user_id)
                        except Exception:
                            pass
                except asyncio.TimeoutError:
                    raise  # an open stall, NOT an effort rejection — handled below
                except Exception as e_eff:
                    _eff_err = str(e_eff)
                    if ("reasoning_content" in _eff_err
                            and "passed back" in _eff_err
                            and not _reasoning_sanitized):
                        # DeepSeek thinking-mode continuity rule: assistant turns
                        # must carry their original reasoning_content back. Rows
                        # written before reasoning capture can never satisfy it —
                        # drop the assistant/tool history once and retry so a
                        # stuck legacy session resumes instead of crash-looping.
                        logger.warning("DeepSeek thinking-mode replay rejected (%s) — "
                                       "retrying with assistant history sanitized", e_eff)
                        _reasoning_sanitized = True
                        _sanitized_msgs = _sanitize_history_for_deepseek(messages)
                        _create_kwargs["messages"] = _sanitized_msgs
                        messages[:] = _sanitized_msgs
                        stream = await _open_stream(_create_kwargs, _open_s, llm_client)
                    elif "extra_body" in _create_kwargs:
                        logger.info("reasoning effort '%s' rejected (%s) — retrying without it",
                                    reasoning_effort, e_eff)
                        _create_kwargs.pop("extra_body", None)
                        stream = await _open_stream(_create_kwargs, _open_s)
                    else:
                        raise
            except asyncio.TimeoutError:
                # Durable, structured signal (parity with the mid-stream stall) so
                # open-stalls can be counted/correlated on the diagnostics page.
                try:
                    from app.agent.diagnostics import record as _diag
                    _diag("warning", "recovery",
                          f"LLM stream open stalled — no response stream for {_open_s:.0f}s",
                          source="stream_open_stall",
                          detail={"model": model_name, "open_seconds": _open_s,
                                  "turn": turn_count},
                          session_id=session_id, agent_id=agent_id,
                          user_id=user_id)
                except Exception:
                    pass
                # Raise (don't yield a soft error) so the supervising layer records
                # stop_cause='crash' — a RESUMABLE cause the self-healing layer
                # re-ignites, instead of leaving the turn hung.
                raise RuntimeError(
                    f"LLM stream open stalled — no response stream for {_open_s:.0f}s "
                    f"(model={model_name}); aborting so the run can recover"
                )
            except Exception as e:
                try:
                    await db_offload(lambda: db.update_interaction(
                        streaming_asst_id, status="error", content="",
                    ))
                except Exception:
                    logger.exception("could not mark pre-stream assistant row as failed")
                # Durable flight-recorder entry — the error event yielded below
                # is consumed by buffered wrappers and frequently lost, so
                # without this the failure is invisible to post-hoc diagnosis
                # (an audit re-run whose LLM open failed left no trace at all).
                try:
                    from app.agent.diagnostics import record as _diag
                    _diag("error", "loop",
                          f"LLM call failed before stream: {e}",
                          source="llm_call_error",
                          detail={"model": model_name, "provider": provider_name,
                                  "turn": turn_count,
                                  "reasoning_effort": reasoning_effort or "none"},
                          session_id=session_id, agent_id=agent_id, user_id=user_id)
                except Exception:
                    pass
                logger.error("LLM call failed before stream (session=%s model=%s): %s",
                             session_id, model_name, e, exc_info=True)
                # Detect network-level failures (connection lost, DNS, timeout) and
                # tag them as recoverable crashes so the self-healing layer re-ignites
                # the turn instead of leaving a dead error bubble.
                _llm_err = str(e)
                _is_network_error = (
                    "Network connection lost" in _llm_err
                    or "APITimeoutError" in _llm_err
                    or "APIConnectionError" in _llm_err
                )
                if _is_network_error:
                    yield {"type": "error", "level": "agent",
                           "message": "The connection to the AI model was lost — "
                                      "the system will automatically recover and resume.",
                           "stop_cause": "crash"}
                else:
                    yield {"type": "error", "level": "agent",
                           "message": f"LLM call failed: {e}"}
                    # Surface provider credit/billing errors as a visible system message.
                    _llm_err_str = str(e)
                    try:
                        from app.util.alerts import is_provider_credit_error, persist_402_alert
                        if is_provider_credit_error(_llm_err_str):
                            await persist_402_alert(
                                _llm_err_str, user_id, session_id, model_name,
                                provider_name or "",
                            )
                    except Exception:
                        pass
                return

            # Per-chunk inactivity guard: if the provider keeps the stream
            # open but stops emitting tokens, a silent hang results. Bounding
            # each read makes a silent stall raise here, where the outer handler
            # records status='error' / stop_cause='crash' — a resumable cause
            # the self-healing layer re-ignites in seconds.
            _stall_s = _stream_stall_seconds()
            _stream_iter = stream.__aiter__()
            # Background heartbeat keeps the liveness watchdog alive during the
            # stream independent of token arrival. Without this, a silent stream
            # (no tokens) also means no heartbeat, so the frozen detector misses
            # the stall entirely. Runs at _HEARTBEAT_INTERVAL via asyncio.sleep.
            async def _stream_heartbeat():
                while True:
                    await asyncio.sleep(_HEARTBEAT_INTERVAL)
                    await _beat()
            _hb_task = asyncio.ensure_future(_stream_heartbeat())
            # The read loop is wrapped so the HTTP stream is ALWAYS closed on
            # the way out — normal end, stall, hard-cancel (a superseding
            # message), watchdog freeze-cancel, or crash. Without the finally,
            # an interrupted turn's suspended generator frame keeps the httpx
            # response (and its socket + buffered bytes) alive until GC; on the
            # very hot resume path that leaked connections and memory.
            try:
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
                        # Durable, structured signal on the diagnostics page so
                        # stalls can be counted/correlated. The raise below alone
                        # would only leave a generic 'turn failed' server error.
                        # The stream itself is closed by the finally below.
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
                        raw_cost = getattr(chunk.usage, 'cost', None)
                        if raw_cost is None and isinstance(extra, dict):
                            raw_cost = extra.get('total_cost', extra.get('cost'))
                        try:
                            if raw_cost is not None:
                                llm_cost = float(raw_cost)
                        except (TypeError, ValueError):
                            pass
                        details = getattr(chunk.usage, 'prompt_tokens_details', None)
                        if details is None and isinstance(extra, dict):
                            details = extra.get('prompt_tokens_details') or {}
                        if not isinstance(details, dict):
                            details = getattr(details, 'model_dump', lambda: {})()
                        cached_input_tokens = int((details or {}).get('cached_tokens', 0) or 0)
                        cache_write_tokens = int((details or {}).get('cache_write_tokens', 0) or 0)
                        if isinstance(extra, dict):
                            cached_input_tokens = int(extra.get('prompt_cache_hit_tokens', cached_input_tokens) or 0)
                            _miss = extra.get('prompt_cache_miss_tokens')
                            if _miss is not None:
                                uncached_input_tokens = int(_miss or 0)
                        completion_details = getattr(chunk.usage, 'completion_tokens_details', None)
                        if completion_details is None and isinstance(extra, dict):
                            completion_details = extra.get('completion_tokens_details') or {}
                        if not isinstance(completion_details, dict):
                            completion_details = getattr(completion_details, 'model_dump', lambda: {})()
                        reasoning_tokens = int((completion_details or {}).get('reasoning_tokens', 0) or 0)

                    if not chunk.choices:
                        continue

                    delta = chunk.choices[0].delta
                    if not delta:
                        continue

                    # DeepSeek thinking-mode: hidden reasoning streams as
                    # `delta.reasoning_content` (the OpenAI SDK surfaces unknown
                    # fields via getattr / model_extra). Capture it — it must be
                    # persisted and replayed with the assistant message, or the
                    # API 400s the next request.
                    _rc = getattr(delta, "reasoning_content", None)
                    if _rc is None:
                        try:
                            _rc = (getattr(delta, "model_extra", None) or {}).get("reasoning_content")
                        except Exception:
                            _rc = None
                    if _rc:
                        collected_reasoning += _rc

                    if delta.content:
                        collected_content += delta.content
                        # The assistant row already exists before provider output.
                        # Do not block every delta on a database round-trip.
                        # The interval-throttled write below plus the final forced
                        # write preserve recovery without making streaming lag.
                        stream_content = delta.content
                        if first_stream_chunk_state[0]:
                            stream_content = prefix_content(stream_content)
                            first_stream_chunk_state[0] = False
                        # Only yield non-empty content — empty/whitespace chunks
                        # waste a bubble and inflate turn counts.
                        if stream_content.strip():
                            yield {"type": "stream", "level": "agent", "content": stream_content, "asst_id": streaming_asst_id}
                        await _persist_stream_progress()

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
            finally:
                # Best-effort, never masks the original exit. Catches
                # BaseException so a re-raised CancelledError from close() can't
                # swallow the cancel that is unwinding us.
                _hb_task.cancel()
                try:
                    await stream.close()
                except BaseException:
                    pass

            # Interrupt check immediately after the LLM stream closes. The
            # stream's own persist-throttled check may have missed the flag if
            # the LLM only emitted content deltas before the interrupt was set
            # (common for short text-only responses).
            if loop_config.is_enabled("interrupt_chk"):
                await _check_interrupt(session_id, interrupt_event, db=db)

            llm_duration = int((time.time() - llm_start) * 1000)

            # Feed the admin Dashboard's live loop-throughput / token-rate cards
            # (in-memory, no DB write — see app/metrics.py). Best-effort.
            try:
                from app import metrics as _metrics
                _metrics.record_llm(llm_duration, input_tokens or 0, output_tokens or 0)
            except Exception:
                pass

            # ── Per-call cost (published price × this call's tokens) ──
            # When OpenRouter (or another provider) sends the actual billed total
            # cost in the stream response (includes prompt-caching discounts, volume
            # pricing, etc.), prefer it over the catalog formula. Fall back to the
            # catalog price when the provider didn't report a total.
            if llm_cost is not None and llm_cost >= 0:
                call_cost_usd, _cost_source = float(llm_cost), "provider_actual"
            else:
                try:
                    from app import model_catalog as _model_catalog
                    call_cost_usd, _cost_source = _model_catalog.cost_for(
                        model_name or "", input_tokens, output_tokens, provider_name or "",
                        cached_input_tokens=cached_input_tokens,
                        cache_write_tokens=cache_write_tokens,
                        uncached_input_tokens=uncached_input_tokens)
                except Exception:
                    call_cost_usd, _cost_source = 0.0, "unknown"

            # ── Pipeline: LLM call end ──
            _billing_event = await _record_billing_usage(
                db, agent_id, user_id, input_tokens, output_tokens, llm_cost,
                model_name=model_name, provider_name=provider_name,
                interaction_id=parent_interaction_id, session_id=session_id,
                cost_usd=call_cost_usd, cost_source=_cost_source,
                cached_input_tokens=cached_input_tokens,
                cache_write_tokens=cache_write_tokens,
                uncached_input_tokens=uncached_input_tokens,
                reasoning_tokens=reasoning_tokens)
            if await _agent_profile_visitor(agent_id, user_id):
                try:
                    from app.api.rate_limit import record_anonymous_actual_usage
                    _anon_usage = await asyncio.to_thread(
                        record_anonymous_actual_usage,
                        user_id, input_tokens or 0, output_tokens or 0,
                        call_cost_usd or 0.0,
                    )
                    if _anon_usage.get("exhausted"):
                        _scope = str(_anon_usage.get("scope") or "cost")
                        _where = "network" if _scope == "cost_source" else (
                            "anonymous service" if _scope == "cost_global" else "guest"
                        )
                        yield {
                            "type": "error", "level": "agent",
                            "code": "registration_required",
                            "scope": f"anonymous_actual_{_scope}_budget",
                            "message": (
                                f"The {_where} anonymous credit allowance has been used. "
                                "Register and sign in to continue on account credits."
                            ),
                        }
                        return
                except Exception:
                    logger.debug("Anonymous usage telemetry write failed", exc_info=True)

            tool_calls_data = list(collected_tool_calls.values()) if collected_tool_calls else None
            yield {"type": "pipeline", "level": "pipeline",
                   "step": "llm_call_end", "duration_ms": llm_duration,
                   "input_tokens": input_tokens, "output_tokens": output_tokens,
                   "cost_usd": call_cost_usd, "cost_source": _cost_source, "model": model_name,
                   "cached_input_tokens": cached_input_tokens,
                   "cache_write_tokens": cache_write_tokens,
                   "uncached_input_tokens": uncached_input_tokens,
                   "reasoning_tokens": reasoning_tokens,
                   "effort": reasoning_effort,
                   "has_tool_calls": bool(tool_calls_data)}

            # ── Billing: record usage + charge wallet (best-effort, never blocks chat) ──
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
                    yield {
                        "type": "tool_call", "level": "agent",
                        "tool": tool_name, "args": tool_args,
                        "tool_call_id": tc.id,
                    }

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
                _asst_msg: Dict[str, Any] = {
                    "role": "assistant",
                    "content": replay_content,
                    "tool_calls": full_tool_calls,
                }
                if collected_reasoning:
                    # DeepSeek thinking mode: the reasoning must travel with the
                    # assistant message on the next request, or the API 400s.
                    _asst_msg["reasoning_content"] = collected_reasoning
                messages.append(_asst_msg)

                # Persist intermediate assistant message — clean content, no tool-call echo
                assistant_content = (collected_content or "").strip()
                cleanup_final = _cleanup_final_step(assistant_content, collected_tool_calls)
                if _requires_plan_document and not plan_document_updated:
                    cleanup_final = False
                # Tool calls are stored in the `output` field (line 1247 below),
                # NOT embedded in the content string. The legacy `\n\n[Tool calls: ...]`
                # suffix was removed because it contaminated message history: the LLM
                # would see its own tool calls echoed as text in the next turn, causing
                # it to write tool calls as text instead of making structured calls.
                # session_history.py reads tool calls from the `output` field instead.
                meta_asst = _build_meta(
                    "assistant", input_tokens, output_tokens, llm_cost,
                    message_phase=("main" if cleanup_final else "progress"),
                )
                # Keep only structured tool calls on intermediate rows. The old
                # payload copied the entire growing prompt + tool schema into
                # every step, producing quadratic database growth.
                outp = _assistant_output(
                    tool_calls=full_tool_calls,
                    reasoning_content=collected_reasoning or None,
                )
                db_start = time.time()
                _updated_streaming_row = streaming_asst_id is not None
                if _updated_streaming_row:
                    # Finalize the in-progress row created while streaming.
                    # Offloaded: this per-step write is a ~150ms remote round-trip;
                    # keeping it off the event loop stops it freezing the live stream.
                    await db_offload(lambda: db.update_interaction(
                        streaming_asst_id, content=assistant_content,
                        status="complete", output_data=outp, metadata=meta_asst,
                    ))
                    asst_id = streaming_asst_id
                else:
                    asst_id = await db_offload(lambda: db.insert_interaction(
                        user_id, session_id, role="assistant", content=assistant_content,
                        parent_id=parent_interaction_id,
                        channel=channel,
                        metadata=meta_asst,
                        output_data=outp,
                        sender_id=agent_id,
                        receiver_id=user_id,
                    ))
                db_dur = int((time.time() - db_start) * 1000)
                # This step's assistant message is finalized; clear the handle so
                # an interrupt during tool execution doesn't re-finalize it.
                streaming_asst_id = None
                yield {"type": "db", "level": "db",
                       "op": ("update_interaction" if _updated_streaming_row
                              else "insert_interaction"), "role": "assistant",
                       "tool_name": None, "id": asst_id, "ms": db_dur}
                # Finalize THIS step's bubble in the UI (it's an intermediate
                # assistant message that precedes tool calls — shown as its own
                # bubble so the user sees every step, not just the final answer).
                # Skip if content is empty — no bubble to render.
                if assistant_content.strip():
                    if cleanup_final:
                        yield {"type": "response", "level": "agent",
                               "message_phase": "main",
                               "asst_id": asst_id,
                               "content": prefix_content(assistant_content)}
                        # Cleanup-final IS a completed run: the substantive answer
                        # is already persisted and emitted, and the cleanup tools
                        # (reset_to_default / set_effort→default / set_model→default)
                        # are model/effort reversion — housekeeping, not checklist-
                        # relevant work. So the checklist audit is safe here, and
                        # required — otherwise any run that ends by reverting its
                        # model would silently skip the auditor.
                        _schedule_output_closer(
                            user_id=user_id, session_id=session_id,
                            agent_id=agent_id, final_asst_id=asst_id,
                            parent_interaction_id=parent_interaction_id,
                            db=db, channel=channel,
                            audit_eligible=True,
                            execution_mode=execution_mode,
                            starting_execution_mode=_run_start_execution_mode,
                        )
                    else:
                        yield {"type": "agent_step_end", "level": "agent",
                               "message_phase": "progress",
                               "asst_id": asst_id, "content": assistant_content}

                # ── Pipeline: validation start ──
                yield {"type": "pipeline", "level": "pipeline",
                       "step": "validate_start", "tool_count": len(collected_tool_calls)}

                valid_calls: List[Any] = []
                blocked_calls: List[Any] = []
                validated_tool_context: Dict[str, Dict[str, Any]] = {}
                for idx, tc in sorted(collected_tool_calls.items()):
                    await _check_interrupt(session_id, interrupt_event, db=db)
                    
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

                    validation_error = await validate_tool_call(tool_name, tool_args, tools, denied=allowed_tools)

                    yield {"type": "pipeline", "level": "pipeline",
                           "step": "validate_result", "tool": tool_name,
                           "passed": validation_error is None,
                           "error": str(validation_error) if validation_error else None}

                    if validation_error:
                        error_json = json.dumps(validation_error)
                        yield {
                            "type": "tool_result", "level": "agent",
                            "tool": tool_name,
                            "tool_call_id": tc.id,
                            "result": error_json[:2000],
                            "error": True,
                            "error_type": "validation_error",
                            "recoverable": True,
                        }
                        tool_msg = {"role": "tool", "content": error_json[:10000], "tool_call_id": tc.id}
                        messages.append(tool_msg)

                        outp = json.dumps({"role": "tool", "content": tool_msg["content"], "tool_call_id": tc.id, "name": tool_name, "success": False})
                        db_start = time.time()
                        inter_id = await db.insert_interaction(
                            user_id, session_id, role="tool", content=tool_msg["content"],
                            parent_id=asst_id,
                            tool_call_id=tc.id,
                            channel=channel,
                            metadata=json.dumps({"success": False, "duration_ms": 0, "input_params": tool_args, "error_message": "Validation failed"}),
                            output_data=outp,
                            sender_id=agent_id,
                            receiver_id=agent_id,
                        )
                        db_dur = int((time.time() - db_start) * 1000)
                        yield {"type": "db", "level": "db",
                               "op": "insert_interaction", "role": "tool",
                               "tool_name": tool_name, "id": inter_id, "ms": db_dur}
                        _validation_event = _health.record_outcome(
                            False, turn=turn_count, tool=tool_name,
                            error_type="validation_error",
                            threshold=(_wd_cfg.get("on_errors", 0) if isinstance(_wd_cfg, dict) else 0),
                        )
                        if _validation_event:
                            stall_strikes += 1
                            yield {"type": "pipeline", "level": "pipeline",
                                   "step": "watchdog_trigger", "reason": _validation_event["reason"],
                                   "severity": _validation_event["severity"], "strikes": stall_strikes}
                            _launch_health_watchdog(_validation_event, anchor_id=asst_id)
                    else:
                        # ── Stall guard: identical-call loop detection ──────────
                        # If the agent calls the SAME tool with the SAME arguments
                        # too many times, it is looping (e.g. run_worker_trials x6).
                        # Block the repeat, tell it to change approach, count a
                        # strike, and move on without executing.
                        # 0 = disabled (infinite).
                        _tool_info = tools.get(tool_name)
                        _sig = _canonical_tool_signature(
                            tool_name,
                            tool_args,
                            getattr(_tool_info, "parameters", None),
                        )
                        _tool_call_counts[_sig] += 1
                        _tool_total_counts[tool_name] += 1
                        _oscillation_event = _health.record_request(
                            tool_name, turn=turn_count, signature=_sig
                        )

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
                        _hit_streak = _MAX_TOOLNAME_STREAK > 0 and _tool_name_streak >= _MAX_TOOLNAME_STREAK
                        try:
                            _tool_budget = max(0, int(_tool_call_budgets.get(tool_name) or 0))
                        except (TypeError, ValueError):
                            _tool_budget = 0
                        _hit_budget = _tool_budget > 0 and _tool_total_counts[tool_name] > _tool_budget
                        _hit_no_progress = tool_name in _no_progress_tools

                        if (_hit_identical or _hit_streak or _hit_budget
                                or _hit_no_progress or _oscillation_event):
                            stall_strikes += 1
                            if _hit_identical:
                                _loop_warn = (
                                    f"Loop guard: you have called `{tool_name}` with identical "
                                    f"arguments {_tool_call_counts[_sig]} times. That is not making "
                                    f"progress, so I did not run it again. Do NOT repeat the same "
                                    f"call — take a different approach, use a different tool, or "
                                    f"stop and give the user your best answer or a clarifying question."
                                )
                            elif _hit_budget:
                                _loop_warn = (
                                    f"Loop guard: `{tool_name}` has reached its per-turn budget "
                                    f"of {_tool_budget} calls. I did not run it again. Synthesize "
                                    f"what you already found, use a different tool, or give the "
                                    f"user your best answer."
                                )
                            elif _hit_no_progress:
                                _loop_warn = (
                                    f"Loop guard: repeated `{tool_name}` calls returned the same "
                                    f"result {_MAX_NO_PROGRESS_RESULTS} times. Further calls are "
                                    f"blocked for this turn because they are not making progress."
                                )
                            elif _oscillation_event:
                                _loop_warn = (
                                    "Loop guard: the recent tool sequence is alternating between "
                                    f"the same two actions ({_oscillation_event['evidence']['pattern']}). "
                                    "I did not run this call. Re-plan with a materially different "
                                    "approach, or ask the user for missing information."
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
                                    "count": _tool_call_counts[_sig],
                                    "total_count": _tool_total_counts[tool_name],
                                    "reason": (
                                        "identical" if _hit_identical else
                                        "budget" if _hit_budget else
                                        "no_progress" if _hit_no_progress else
                                        "oscillation" if _oscillation_event else
                                        "name_streak"
                                    ),
                                    "strikes": stall_strikes}
                            if isinstance(_wd_cfg, dict) and _wd_cfg.get("on_stall", True):
                                _stall_reason = (
                                    "identical_call" if _hit_identical else
                                    "tool_budget" if _hit_budget else
                                    "same_result" if _hit_no_progress else
                                    "alternating_tool_loop" if _oscillation_event else
                                    "same_tool_streak"
                                )
                                _stall_event = _oscillation_event or health_event(
                                    _stall_reason,
                                    turn=turn_count,
                                    tool=tool_name,
                                    severity=2,
                                    signature_count=_tool_call_counts[_sig],
                                    tool_total=_tool_total_counts[tool_name],
                                    tool_name_streak=_tool_name_streak,
                                    stall_strikes=stall_strikes,
                                )
                                _launch_health_watchdog(_stall_event, anchor_id=asst_id)
                            yield {
                                "type": "tool_result", "level": "agent", "tool": tool_name,
                                "tool_call_id": tc.id,
                                "result": json.dumps({"status": "loop_blocked", "message": _loop_warn}),
                                "duration_ms": 0, "error": True,
                                "error_type": "loop_blocked", "recoverable": True,
                            }
                            tool_msg = {"role": "tool", "content": _loop_warn, "tool_call_id": tc.id}
                            messages.append(tool_msg)
                            outp = json.dumps({"role": "tool", "content": _loop_warn, "tool_call_id": tc.id, "name": tool_name, "success": False})
                            inter_id = await db.insert_interaction(
                                user_id, session_id, role="tool", content=_loop_warn,
                                parent_id=asst_id, tool_call_id=tc.id, tool_name=tool_name,
                                channel=channel,
                                metadata=json.dumps({"success": False, "duration_ms": 0, "input_params": tool_args, "error_message": "loop_blocked"}),
                                output_data=outp,
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
                        # The selected mode's permission_policy controls the boundary:
                        #   read_only — block every write/edit or destructive tool
                        #   write     — allow tools without per-step confirmation
                        # Model-switch tools (set_model/use_premium_model/set_effort) are
                        # exempt in plan AND ask — see the guardrail_skip below
                        # (MODEL-SWITCH-EXEMPT).
                        #
                        # ── Live mode re-read (mid-turn pill flip) ──
                        # The user may flip the chat pill (Ask→Plan→Auto) while the
                        # agent is mid-turn. Re-read the session's persisted mode here
                        # so the change takes effect on the very next tool call without
                        # needing a new user message. Mirrors the model hot-swap at the
                        # top of the turn loop. One lightweight DB read per tool call.
                        _live_mode = None
                        if session_id:
                            try:
                                _live_mode = await db.get_session_execution_mode(session_id)
                            except Exception:
                                pass  # best-effort — never break the turn on a mode re-read
                        _current_mode = _live_mode or execution_mode
                        _current_mode_entry = resolve_execution_mode(_agent_rec, _current_mode)
                        _current_policy = _current_mode_entry.get("permission_policy", "read_only")
                        ti = tools.get(tool_name)

                        # ── contract_chk node: mechanical contract rules ──
                        # The per-agent run contract (metadata['contract']) is
                        # enforced synchronously here with ZERO LLM calls — flags
                        # and counters the loop already keeps. A violation blocks
                        # the call with an actionable message, exactly like the
                        # guardrail/validation paths (error_type
                        # "contract_blocked"), so the agent sees why and adjusts.
                        # Disabled via loop_logic (contract_chk) or when no
                        # contract is configured.
                        if loop_config.is_enabled("contract_chk") and _contract:
                            from app.agent.contracts import mechanical_check
                            _contract_msg = mechanical_check(
                                _contract, _contract_state,
                                tool_name=tool_name, tool_args=tool_args,
                                execution_mode=("auto" if _current_policy == "write" else "plan"))
                            if _contract_msg:
                                yield {"type": "pipeline", "level": "pipeline",
                                       "step": "contract_blocked", "tool": tool_name,
                                       "message": _contract_msg[:300]}
                                yield {
                                    "type": "tool_result", "level": "agent",
                                    "tool": tool_name,
                                    "tool_call_id": tc.id,
                                    "result": json.dumps({"status": "contract_blocked", "message": _contract_msg}),
                                    "duration_ms": 0, "error": True,
                                    "error_type": "contract_blocked", "recoverable": True,
                                }
                                tool_msg = {"role": "tool", "content": _contract_msg, "tool_call_id": tc.id}
                                messages.append(tool_msg)
                                outp = json.dumps({"role": "tool", "content": _contract_msg, "tool_call_id": tc.id, "name": tool_name, "success": False})
                                db_start = time.time()
                                inter_id = await db.insert_interaction(
                                    user_id, session_id, role="tool", content=_contract_msg,
                                    parent_id=asst_id, tool_call_id=tc.id, tool_name=tool_name,
                                    channel=channel,
                                    metadata=json.dumps({"success": False, "duration_ms": 0, "input_params": tool_args, "error_message": "contract_blocked"}),
                                    output_data=outp,
                                    sender_id=agent_id, receiver_id=agent_id,
                                )
                                db_dur = int((time.time() - db_start) * 1000)
                                yield {"type": "db", "level": "db",
                                       "op": "insert_interaction", "role": "tool",
                                       "tool_name": tool_name, "id": inter_id, "ms": db_dur}
                                # A blocked call counts as a failed outcome so
                                # the max_tool_errors soft strike stays honest.
                                _contract_state["consecutive_errors"] = int(
                                    _contract_state.get("consecutive_errors") or 0) + 1
                                _contract_event = _health.record_outcome(
                                    False, turn=turn_count, tool=tool_name,
                                    error_type="contract_blocked",
                                    threshold=(_wd_cfg.get("on_errors", 0) if isinstance(_wd_cfg, dict) else 0),
                                )
                                if _contract_event:
                                    stall_strikes += 1
                                    yield {"type": "pipeline", "level": "pipeline",
                                           "step": "watchdog_trigger", "reason": _contract_event["reason"],
                                           "severity": _contract_event["severity"], "strikes": stall_strikes}
                                    _launch_health_watchdog(_contract_event, anchor_id=asst_id)
                                continue

                        is_destructive = tool_name in effective_destructive
                        if _current_policy == 'read_only':
                            # Read-only policy gates every tool flagged as mutating.
                            gate_required = bool(ti and ti.destructive) or is_destructive
                        else:
                            gate_required = False
                        # Switching INTO auto mode is a privilege escalation — it turns
                        # the per-step confirmation gate OFF for the rest of the run — so
                        # it ALWAYS needs the user's explicit go-ahead, whatever the
                        # current mode (Plan→Auto needs specific approval; Ask stays
                        # per-tool and shouldn't promote at all). Other mode switches
                        # (→Plan / →Ask only tighten safety) are never gated.
                        if tool_name == "set_execution_mode":
                            _tgt = normalize_mode_id((tool_args or {}).get("mode", ""), "")
                            _target_policy = resolve_execution_mode(_agent_rec, _tgt).get(
                                "permission_policy", "read_only")
                            gate_required = (
                                _target_policy == "write" and _current_policy != "write" and not auto_confirm)
                        # Per-arg exemption: read-only shell commands via run_command
                        # (git status, ls, cat, ...) bypass the confirmation gate.
                        if gate_required and tool_name == "run_command" and _is_safe_shell_command(tool_args.get("command", "")):
                            gate_required = False
                            yield {"type": "pipeline", "level": "pipeline",
                                   "step": "guardrail_skip", "tool": tool_name,
                                   "status": "safe_read_only",
                                   "command": str(tool_args.get("command", ""))[:120],
                                   "message": "Read-only shell command — confirmation skipped"}
                        # Per-arg exemption: set_effort only needs confirmation when it
                        # RAISES reasoning effort (costs more). Lowering / clearing runs free.
                        if gate_required and tool_name == "set_effort" and not _effort_raises_spend(tool_args, reasoning_effort):
                            gate_required = False
                            yield {"type": "pipeline", "level": "pipeline",
                                   "step": "guardrail_skip", "tool": tool_name,
                                   "status": "effort_not_raised",
                                   "message": "Lowering/clearing reasoning effort — confirmation skipped"}
                        # Per-mode exemption: model switching (set_model /
                        # use_premium_model / set_effort) in PLAN and ASK modes.
                        # These write only THIS session's model override — no user
                        # data — and right-sizing the model (upgrade to
                        # premium/vision) is core agent behaviour, so both gated
                        # modes run them free. Auto was never gated, so model
                        # switching is unrestricted everywhere.
                        # (MODEL-SWITCH-EXEMPT)
                        if gate_required and _current_policy == 'read_only' and tool_name in _MODEL_SWITCH_TOOLS:
                            gate_required = False
                            yield {"type": "pipeline", "level": "pipeline",
                                   "step": "guardrail_skip", "tool": tool_name,
                                   "status": "model_switch_exempt",
                                   "message": "Model switch — confirmation skipped (unrestricted in plan/ask)"}
                        # Per-arg exemption: read-only git operations via git_tool
                        # (status, log, diff, …) skip the gate; mutating ops confirm.
                        if gate_required and tool_name == "git_tool" and _is_safe_git_operation(tool_args):
                            gate_required = False
                            yield {"type": "pipeline", "level": "pipeline",
                                   "step": "guardrail_skip", "tool": tool_name,
                                   "status": "safe_read_only",
                                   "operation": str(tool_args.get("operation", ""))[:60],
                                   "message": "Read-only git operation — confirmation skipped"}
                        # Per-arg exemption: read-only HTTP methods (GET/HEAD/OPTIONS)
                        # via http_request fetch without changing state → no gate.
                        if gate_required and tool_name == "http_request" and _is_safe_http_request(tool_args):
                            gate_required = False
                            yield {"type": "pipeline", "level": "pipeline",
                                   "step": "guardrail_skip", "tool": tool_name,
                                   "status": "safe_read_only",
                                   "method": str(tool_args.get("method", "GET"))[:12],
                                   "message": "Read-only HTTP request — confirmation skipped"}
                        # Per-arg exemption: navigating/reading a page via browser_action
                        # changes nothing on the user's behalf → no gate. Acting on the
                        # page (click / type / evaluate) still confirms.
                        if gate_required and tool_name == "browser_action" and _is_safe_browser_action(tool_args):
                            gate_required = False
                            yield {"type": "pipeline", "level": "pipeline",
                                   "step": "guardrail_skip", "tool": tool_name,
                                   "status": "safe_read_only",
                                   "action": str(tool_args.get("action", ""))[:24],
                                   "message": "Read-only browser action — confirmation skipped"}
                        # Persist the exact prospective authority with the tool
                        # result so the detached Closer can reconstruct which
                        # mode segment performed mutating work without receiving
                        # raw tool logs. Safe per-argument operations and mode/
                        # model controls are not implementation mutations.
                        _mutating_call = bool((ti and ti.destructive) or is_destructive)
                        if tool_name in _MODEL_SWITCH_TOOLS or tool_name == "set_execution_mode":
                            _mutating_call = False
                        elif tool_name == "run_command" and _is_safe_shell_command(tool_args.get("command", "")):
                            _mutating_call = False
                        elif tool_name == "git_tool" and _is_safe_git_operation(tool_args):
                            _mutating_call = False
                        elif tool_name == "http_request" and _is_safe_http_request(tool_args):
                            _mutating_call = False
                        elif tool_name == "browser_action" and _is_safe_browser_action(tool_args):
                            _mutating_call = False
                        validated_tool_context[str(tc.id)] = {
                            "execution_mode": _current_mode,
                            "mutating": _mutating_call,
                        }
                        if gate_required:
                            yield {"type": "pipeline", "level": "pipeline",
                                   "step": "guardrail_check", "tool": tool_name,
                                   "status": "requires_confirmation",
                                   "message": f"Tool '{tool_name}' requires user confirmation per system prompt"}

                            # A read-only policy is a hard capability boundary,
                            # not a per-step approval mode. The only confirmed
                            # transition allowed through this branch is a switch
                            # into a write-capable execution mode.
                            user_confirmed = (
                                _check_user_confirmed(messages, tool_name)
                                if tool_name == "set_execution_mode" else False
                            )

                            if not user_confirmed:
                                yield {"type": "pipeline", "level": "pipeline",
                                       "step": "guardrail_blocked", "tool": tool_name,
                                       "status": "blocked",
                                       "message": f"Tool '{tool_name}' BLOCKED: user confirmation not detected in conversation"}
                                blocked_calls.append((idx, tc, tool_name, tool_args))
                                yield {
                                    "type": "tool_result", "level": "agent",
                                    "tool": tool_name,
                                    "tool_call_id": tc.id,
                                    "result": json.dumps({"status": "blocked", "message": (
                                        f"Tool '{tool_name}' is unavailable under the read-only permission policy. "
                                        "Select and approve a write-capable mode before execution."
                                    )}),
                                    "duration_ms": 0,
                                    "error": True,
                                    "error_type": "guardrail_blocked",
                                    "recoverable": True,
                                }
                                if tool_name == "set_execution_mode":
                                    _gate_help = (
                                        "Switching to AUTO turns off per-step confirmation for the rest of the run, so it "
                                        "needs the user's explicit go-ahead. Don't retry the switch yet. Ask the user to "
                                        "confirm they want you to proceed autonomously; once they clearly agree "
                                        "(\"yes\", \"go ahead\", \"switch to auto\"), call set_execution_mode(\"auto\") again."
                                    )
                                elif _current_mode == 'plan':
                                    _gate_help = (
                                        f"Tool '{tool_name}' is blocked because you are in PLAN mode, which is "
                                        f"read-only — it makes changes and needs the user's go-ahead. Don't retry it. "
                                        f"Finish presenting your plan and ask the user to approve it. Once they approve, "
                                        f"ask for their explicit go-ahead to switch to Auto, then call set_execution_mode(\"auto\") "
                                        f"and carry out the plan."
                                    )
                                elif _current_mode == 'ask':
                                    _gate_help = (
                                        f"Tool '{tool_name}' is blocked because ASK mode is proposal-only and read-only. "
                                        f"Do not execute the change in this mode, even after per-step approval. Finish the "
                                        f"proposal and ask the user to select a write-capable mode when they want it executed."
                                    )
                                else:
                                    _gate_help = (
                                        f"Tool '{tool_name}' is blocked because {_current_mode_entry.get('label', _current_mode)} "
                                        "uses the read-only permission policy. Finish that mode's deliverable, then have the "
                                        "user select a write-capable mode before execution."
                                    )
                                tool_msg = {"role": "tool", "content": _gate_help, "tool_call_id": tc.id}
                                messages.append(tool_msg)
                                outp = json.dumps({"role": "tool", "content": tool_msg["content"], "tool_call_id": tc.id, "name": tool_name, "success": False})
                                db_start = time.time()
                                inter_id = await db.insert_interaction(
                                    user_id, session_id, role="tool", content=tool_msg["content"],
                                    parent_id=asst_id,
                                    tool_call_id=tc.id,
                                    tool_name=tool_name,
                                    channel=channel,
                                    metadata=json.dumps({"success": False, "duration_ms": 0, "input_params": tool_args, "error_message": "Guardrail blocked — requires confirmation"}),
                                    output_data=outp,
                                    sender_id=agent_id,
                                    receiver_id=agent_id,
                                )
                                db_dur = int((time.time() - db_start) * 1000)
                                yield {"type": "db", "level": "db",
                                       "op": "insert_interaction", "role": "tool",
                                       "tool_name": tool_name, "id": inter_id, "ms": db_dur}
                                _guard_event = _health.record_outcome(
                                    False, turn=turn_count, tool=tool_name,
                                    error_type="guardrail_blocked",
                                    threshold=(_wd_cfg.get("on_errors", 0) if isinstance(_wd_cfg, dict) else 0),
                                )
                                if _guard_event:
                                    stall_strikes += 1
                                    yield {"type": "pipeline", "level": "pipeline",
                                           "step": "watchdog_trigger", "reason": _guard_event["reason"],
                                           "severity": _guard_event["severity"], "strikes": stall_strikes}
                                    _launch_health_watchdog(_guard_event, anchor_id=asst_id)
                                continue
                            else:
                                yield {"type": "pipeline", "level": "pipeline",
                                       "step": "guardrail_override", "tool": tool_name,
                                       "status": "confirmed", "by": "user"}

                        # Mechanical accounting and Manager judgment happen after
                        # the confirmation gate, so confirmed edits are supervised
                        # exactly like edits that needed no confirmation.
                        if loop_config.is_enabled("contract_chk") and _contract_state:
                            from app.agent.contracts import mark_tool_scheduled
                            mark_tool_scheduled(_contract_state)

                        _mv = await _evaluate_manager_gates(
                            tool_name, tool_args,
                            tool_call_id=tc.id,
                            anchor_id=asst_id, current_mode=_current_mode,
                        )
                        if _mv and _mv.get("verdict") in ("block", "revise"):
                            _starter_revise = _mv.get("reason") == "starter_context_ready"
                            if not _starter_revise:
                                _manager_state["block_count"] = int(
                                    _manager_state.get("block_count") or 0
                                ) + 1
                            try:
                                _max_manager_blocks = max(
                                    1, int(_manager_cfg.get("max_blocks") or 3)
                                )
                            except (TypeError, ValueError):
                                _max_manager_blocks = 3
                            _m_feedback = (
                                _mv.get("feedback") or _mv.get("reason")
                                or "The Manager did not approve this action."
                            )
                            yield {"type": "pipeline", "level": "pipeline",
                                   "step": ("starter_write_gate" if _starter_revise
                                            else "manager_blocked"), "tool": tool_name,
                                   "verdict": _mv.get("verdict"),
                                   "message": _m_feedback[:300]}
                            yield {
                                "type": "tool_result", "level": "agent",
                                "tool": tool_name, "tool_call_id": tc.id,
                                "result": json.dumps({
                                    "status": ("starter_context_ready" if _starter_revise
                                               else "manager_blocked"),
                                    "message": _m_feedback,
                                }),
                                "duration_ms": 0, "error": True,
                                "error_type": ("starter_write_gate" if _starter_revise
                                               else "manager_blocked"), "recoverable": True,
                            }
                            tool_msg = {"role": "tool", "content": _m_feedback, "tool_call_id": tc.id}
                            messages.append(tool_msg)
                            outp = json.dumps({"role": "tool", "content": _m_feedback, "tool_call_id": tc.id, "name": tool_name, "success": False})
                            inter_id = await db.insert_interaction(
                                user_id, session_id, role="tool", content=_m_feedback,
                                parent_id=asst_id, tool_call_id=tc.id, tool_name=tool_name,
                                channel=channel,
                                metadata=json.dumps({"success": False, "duration_ms": 0, "input_params": tool_args, "error_message": "manager_blocked"}),
                                output_data=outp,
                                sender_id=agent_id, receiver_id=agent_id,
                            )
                            yield {"type": "db", "level": "db",
                                   "op": "insert_interaction", "role": "tool",
                                   "tool_name": tool_name, "id": inter_id, "ms": 0}
                            _manager_event = _health.record_outcome(
                                False, turn=turn_count, tool=tool_name,
                                error_type="manager_blocked",
                                threshold=(_wd_cfg.get("on_errors", 0) if isinstance(_wd_cfg, dict) else 0),
                            )
                            if _manager_event:
                                stall_strikes += 1
                                yield {"type": "pipeline", "level": "pipeline",
                                       "step": "watchdog_trigger", "reason": _manager_event["reason"],
                                       "severity": _manager_event["severity"], "strikes": stall_strikes}
                                _launch_health_watchdog(_manager_event, anchor_id=asst_id)
                            if _manager_state["block_count"] >= _max_manager_blocks:
                                yield {
                                    "type": "pipeline", "level": "pipeline",
                                    "step": "manager_escalation_stop",
                                    "reason": "repeated_manager_blocks",
                                    "blocks": _manager_state["block_count"],
                                    "max_blocks": _max_manager_blocks,
                                    "turn": turn_count,
                                }
                                stall_stop_msg = (
                                    "I stopped after the Manager repeatedly rejected "
                                    "proposed actions. Continuing to retry would risk "
                                    "drifting from your request. Please clarify the "
                                    "intended change or approve a revised approach."
                                )
                                valid_calls.clear()
                                break
                            continue

                        # A valid verdict authorizes only this still-current
                        # generation. Stop/replacement is durable and wins even
                        # when the optional interrupt loop node is disabled.
                        from app.agent.run_fence import side_effects_allowed
                        if not await side_effects_allowed(
                            db, session_id,
                            expected_turn_id=parent_interaction_id,
                        ):
                            raise asyncio.CancelledError(
                                "Turn stopped or replaced after Manager review."
                            )

                        valid_calls.append((idx, tc, tool_name, tool_args))

                if loop_config.is_enabled("interrupt_chk"):
                    await _check_interrupt(session_id, interrupt_event, db=db)

                if stall_stop_msg is not None:
                    break

                if valid_calls:
                    yield {"type": "pipeline", "level": "pipeline",
                           "step": "execute_batch_start", "tool_count": len(valid_calls),
                           "tools": [name for _, _, name, _ in valid_calls]}

                    async def execute_one(name: str, args: dict, tc_id: str) -> dict:
                        start = time.time()
                        tool_reservation = None
                        tool_info = tools[name]
                        side_effecting = bool(
                            getattr(tool_info, "destructive", False)
                            or getattr(tool_info, "requires_confirmation", False)
                        )
                        if side_effecting:
                            from app.agent.run_fence import side_effects_allowed
                            if not await side_effects_allowed(
                                db, session_id,
                                expected_turn_id=parent_interaction_id,
                            ):
                                raise asyncio.CancelledError(
                                    "Side effect fenced by Stop or replacement."
                                )
                        if side_effecting and turn_reservation_key:
                            from app.agent.turn_reservations import reserve_tool
                            tool_reservation = reserve_tool(
                                turn_reservation_key,
                                tc_id,
                                name,
                                args,
                                side_effecting=True,
                                lease_seconds=900,
                            )
                            if tool_reservation.state == "replay" and tool_reservation.result:
                                return {
                                    **tool_reservation.result,
                                    "input_params": args,
                                    "replayed": True,
                                }
                            if tool_reservation.state != "acquired":
                                detail = tool_reservation.detail or tool_reservation.state
                                return {
                                    "tool_call_id": tc_id,
                                    "tool": name,
                                    "content": json.dumps({
                                        "status": "recovery_required",
                                        "message": (
                                            "This side-effecting tool was not replayed because "
                                            f"its durable reservation is {detail}."
                                        ),
                                    }),
                                    "duration_ms": int((time.time() - start) * 1000),
                                    "success": False,
                                    "error": None,
                                    "input_params": args,
                                    "changed_paths": [],
                                    "reservation_state": tool_reservation.state,
                                }
                        from app.agent.session_changes import (
                            capture_tool_state_async,
                            record_tool_delta,
                        )
                        before_changes = await capture_tool_state_async(name, args)
                        try:
                            handler = tools[name].handler if hasattr(tools[name], 'handler') else tools[name]
                            from app.tools.execution_context import (
                                ToolExecutionContext,
                                tool_execution_scope,
                            )
                            with tool_execution_scope(ToolExecutionContext(
                                user_id=user_id,
                                session_id=session_id,
                                turn_key=turn_reservation_key or "",
                                tool_name=name,
                                tool_call_id=tc_id,
                                authority_mode=getattr(db, "authority_mode", "server"),
                                idempotency_key=(
                                    tool_reservation.key
                                    if tool_reservation is not None
                                    and tool_reservation.state == "acquired"
                                    else None
                                ),
                                side_effecting=side_effecting,
                            )):
                                if side_effecting:
                                    from app.agent.run_fence import side_effects_allowed
                                    if not await side_effects_allowed(
                                        db, session_id,
                                        expected_turn_id=parent_interaction_id,
                                    ):
                                        raise asyncio.CancelledError(
                                            "Side effect fenced immediately before invocation."
                                        )
                                result = await handler(**args)
                            result_str = str(result)
                            after_changes = await capture_tool_state_async(name, args)
                            changed_paths = await record_tool_delta(
                                session_id, before_changes, after_changes
                            )
                            duration_ms = int((time.time() - start) * 1000)
                            result_record = {
                                "tool_call_id": tc_id,
                                "tool": name,
                                "content": result_str,
                                "duration_ms": duration_ms,
                                "success": True,
                                "error": None,
                                "changed_paths": changed_paths,
                            }
                            if tool_reservation is not None:
                                from app.agent.turn_reservations import complete
                                complete(tool_reservation, result_record)
                            return {**result_record, "input_params": args}
                        except Exception as e:
                            if tool_reservation is not None:
                                from app.agent.turn_reservations import fail
                                # The external service may have accepted the call
                                # before the exception reached us. Refuse replay.
                                fail(tool_reservation, uncertain=True)
                            duration_ms = int((time.time() - start) * 1000)
                            te: ToolError = classify_tool_error(e, name, args)
                            result_str = json.dumps({
                                "status": "error", "error_type": te.error_type, "tool": te.tool_name,
                                "message": te.message, "recoverable": te.recoverable, "hint": te.retry_hint,
                            })
                            return {"tool_call_id": tc_id, "tool": name, "content": result_str, "duration_ms": duration_ms, "success": False, "error": te, "input_params": args, "changed_paths": []}

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
                            await _check_interrupt(session_id, interrupt_event, db=db)
                        success = result["success"]
                        te = result.get("error")

                        yield {
                            "type": "tool_result", "level": "agent",
                            "tool": tool_name,
                            "tool_call_id": tc.id,
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
                        # ── Live execution-mode switch (set_execution_mode tool) ──
                        # The agent flipped Ask/Plan/Auto — typically Plan→Auto once
                        # the user approved a plan. Apply it to the rest of THIS turn's
                        # gating (reassigning the local the gate reads) and broadcast an
                        # `execution_mode` event so the UI pill visibly switches over.
                        if tool_name == "set_execution_mode" and success:
                            try:
                                _mres = json.loads(result["content"])
                                _newmode = _mres.get("execution_mode")
                            except Exception:
                                _newmode = None
                            if _newmode in ("ask", "plan", "auto") and _newmode != execution_mode:
                                execution_mode = _newmode
                                yield {"type": "execution_mode", "level": "pipeline",
                                        "mode": _newmode,
                                        "reason": (tool_args or {}).get("reason", "")}
                        if tool_name == "present_plan" and success:
                            plan_document_updated = True
                        if success and _MAX_NO_PROGRESS_RESULTS > 0:
                            _normalized_result = re.sub(
                                r"\s+", " ", str(result.get("content") or "").strip()
                            )
                            _result_hash = hashlib.sha256(
                                _normalized_result.encode("utf-8", errors="replace")
                            ).hexdigest()
                            _result_key = (tool_name, _result_hash)
                            _tool_result_counts[_result_key] += 1
                            if _tool_result_counts[_result_key] >= _MAX_NO_PROGRESS_RESULTS:
                                _no_progress_tools.add(tool_name)
                        _ti2 = tools.get(tool_name)
                        _tid2 = getattr(_ti2, "tool_id", "") or ""
                        if _tid2.startswith("ds:"):
                            yield {"type": "pipeline", "level": "pipeline",
                                   "step": "data_src_query_finished",
                                   "tool": tool_name, "tool_id": _tid2,
                                   "duration_ms": result["duration_ms"],
                                   "success": success}

                        _validated_ctx = validated_tool_context.get(str(tc.id), {})
                        tool_exec_meta = json.dumps({
                            "success": success,
                            "duration_ms": result["duration_ms"],
                            "input_params": tool_args,
                            "changed_paths": result.get("changed_paths") or [],
                            "execution_mode": _validated_ctx.get("execution_mode", execution_mode),
                            "mutating": bool(_validated_ctx.get("mutating", False)),
                            "error_message": None if success else result["content"][:500],
                        })

                        tool_msg = {"role": "tool", "content": result["content"][:10000], "tool_call_id": tc.id}
                        messages.append(tool_msg)

                        # ── contract state + integrated error watchdog ──
                        # Contract counters remain optional. Run-health outcomes
                        # are always tracked so Manager supervision does not vanish
                        # when the mechanical contract node is disabled.
                        from app.agent.contracts import (
                            is_edit_tool, is_verify_tool,
                        )
                        _result_changed_paths = [
                            str(path) for path in (result.get("changed_paths") or [])
                            if path
                        ]
                        if success and _result_changed_paths:
                            _manager_state.setdefault("changed_paths", set()).update(
                                _result_changed_paths
                            )
                        if success and is_edit_tool(tool_name):
                            _edit_events = _manager_state.setdefault("edit_events", [])
                            _edit_events.append({
                                "tool": tool_name,
                                "turn": turn_count,
                                "changed_paths": _result_changed_paths[:25],
                                "changed_path_count": len(_result_changed_paths),
                            })
                            del _edit_events[:-50]
                        if is_verify_tool(tool_name, tool_args):
                            _verification_events = _manager_state.setdefault(
                                "verification_events", []
                            )
                            _verification_events.append({
                                "tool": tool_name,
                                "turn": turn_count,
                                "success": bool(success),
                                "duration_ms": int(result.get("duration_ms") or 0),
                                "error_type": te.error_type if te else None,
                            })
                            del _verification_events[:-50]
                        if loop_config.is_enabled("contract_chk") and _contract_state:
                            from app.agent.contracts import (
                                mark_tool_outcome, mark_edit_executed,
                                mark_verify_executed,
                            )
                            mark_tool_outcome(_contract_state, success)
                            if success and is_edit_tool(tool_name):
                                mark_edit_executed(_contract_state)
                            elif success and is_verify_tool(tool_name, tool_args):
                                mark_verify_executed(_contract_state)
                        _on_err = _wd_cfg.get("on_errors") if isinstance(_wd_cfg, dict) else 0
                        _error_event = _health.record_outcome(
                            success,
                            turn=turn_count,
                            tool=tool_name,
                            error_type=(te.error_type if te else ""),
                            threshold=_on_err,
                        )
                        if _error_event:
                            stall_strikes += 1
                            yield {"type": "pipeline", "level": "pipeline",
                                   "step": "watchdog_trigger", "reason": _error_event["reason"],
                                   "severity": _error_event["severity"], "strikes": stall_strikes}
                            _launch_health_watchdog(_error_event, anchor_id=asst_id)

                        outp = json.dumps({"role": "tool", "content": result["content"][:10000], "tool_call_id": tc.id, "name": tool_name, "success": success, "duration_ms": result["duration_ms"]})
                        db_start = time.time()
                        # Offloaded: the tool-result row is the highest-frequency
                        # write in the loop (once per executed tool call). A ~150ms
                        # remote round-trip here on the event loop stalls the stream.
                        inter_id = await db_offload(lambda: db.insert_interaction(
                            user_id, session_id, role="tool", content=tool_msg["content"],
                            parent_id=asst_id,
                            tool_call_id=tc.id,
                            tool_name=tool_name,
                            channel=channel,
                            metadata=tool_exec_meta,
                            output_data=outp,
                            sender_id=agent_id,
                            receiver_id=agent_id,
                        ))
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

                                # Reload tools for the new template. Carry the SAME
                                # gating the initial load got (bug fix): the per-session
                                # tool/ability state (session_id) and the block list
                                # (this run's agent-deny + global admin denies). Without
                                # them a delegated-to agent silently regains tools the
                                # session had disabled. The block list is fail-closed —
                                # it can only withhold tools, never grant new ones.
                                from app.tools.loader import load_tools as _load_tools
                                tools = await _load_tools(user_id, agent_id=agent_id, agent_template_id=_tpl_id,
                                                          allowed_tools=allowed_tools, session_id=session_id,
                                                          turn_id=parent_interaction_id or "",
                                                          gate_caller_access=True)

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
                        "I stopped because the run accumulated repeated loop or failure "
                        "signals without making reliable progress. I didn't want to keep "
                        "spinning or making unsafe guesses. Could you clarify the missing "
                        "information, or point me at the specific area to change?"
                    )
                    break

                # ── Cleanup-final end ──
                # The substantive answer was already persisted as `final` and
                # emitted as a `response`; the cleanup tool(s) just ran. End the
                # run now so the reverted model doesn't emit a redundant wrap-up
                # line that would surface instead of the real answer.
                if cleanup_final:
                    try:
                        await _store_validated_session_cache()
                    except Exception:
                        pass
                    return

                yield {"type": "pipeline", "level": "pipeline",
                       "step": "check_continue", "turn": turn_count,
                       "max_turns": max_turns, "will_continue": turn_count < max_turns}
                continue

            # ── Empty-response safety net ──
            # A provider can return only hidden reasoning / whitespace.  That is
            # not a user-visible final answer and must never close the run as
            # successful.  Check the same display-safe text that history replays.
            from app.agent.session_history import strip_think_blocks
            _final_content = strip_think_blocks(collected_content or "").strip()
            if not _final_content and not empty_retry_used:
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

            if not _final_content:
                # Deliberately surface this as a resumable failure, rather than
                # emitting an empty `response` event which callers interpret as
                # successful completion.  The web runner preserves this cause and
                # the watchdog re-ignites the task with its continue nudge.
                yield {"type": "error", "level": "agent",
                       "message": "The model returned no visible final response; the task will be resumed automatically.",
                       "stop_cause": "empty_response", "asst_id": streaming_asst_id}
                return

            # Plan-mode contract: a prose plan alone is not enough. Require the
            # durable Plan Overview + Plan Checklist produced by present_plan.
            if _requires_plan_document and not plan_document_updated:
                if not plan_document_retry_used:
                    yield {"type": "pipeline", "level": "pipeline",
                           "step": "mode_contract_blocked", "turn": turn_count,
                           "message": "Plan document contract missing; requesting present_plan."}
                    messages.append({
                        "role": "system",
                        "content": (
                            "MODE CONTRACT BLOCKED COMPLETION: You have not called present_plan. "
                            "Call present_plan now with a task-specific overview AND checklist, then "
                            "return your final planning response. A prose-only plan is not accepted."
                        ),
                    })
                    plan_document_retry_used = True
                    turn_count -= 1
                    continue
                yield {"type": "error", "level": "agent",
                       "message": "Plan mode could not complete because the required Plan document and checklist were not created.",
                       "stop_cause": "contract_blocked", "asst_id": streaming_asst_id}
                return

            # ── No tool calls → final response ──
            _final_asst: Dict[str, Any] = {"role": "assistant", "content": _final_content}
            if collected_reasoning:
                # DeepSeek thinking mode: the reasoning must travel with the
                # assistant message on the next request, or the API 400s.
                _final_asst["reasoning_content"] = collected_reasoning
            messages.append(_final_asst)

            meta_final = _build_meta(
                "assistant", input_tokens, output_tokens, llm_cost,
                message_phase="main",
            )
            # Compact durable evidence for independent close contracts. Tool
            # bodies stay bounded; reviewers receive paths/outcomes rather than
            # the main loop's full raw tool transcript.
            meta_final["contract_evidence"] = {
                "changed_paths": sorted(
                    str(path) for path in (_manager_state.get("changed_paths") or set())
                    if path
                )[:100],
                "edit_events": list(_manager_state.get("edit_events") or [])[-20:],
                "verification_events": list(
                    _manager_state.get("verification_events") or []
                )[-20:],
                "manager_checks_used": int(_manager_state.get("checks_used") or 0),
                "manager_blocks": int(_manager_state.get("block_count") or 0),
            }
            outp = _assistant_output(
                messages=_sent_messages,
                tools=_sent_tools,
                include_snapshot=True,
                reasoning_content=collected_reasoning or None,
            )
            db_start = time.time()
            _updated_streaming_row = streaming_asst_id is not None
            if _updated_streaming_row:
                # Finalize the in-progress row created while streaming.
                # Offloaded: final-answer write on every turn's critical path.
                await db_offload(lambda: db.update_interaction(
                    streaming_asst_id, content=_final_content,
                    status="complete", output_data=outp, metadata=meta_final,
                ))
                inter_id = streaming_asst_id
            else:
                inter_id = await db_offload(lambda: db.insert_interaction(
                    user_id, session_id, role="assistant", content=_final_content,
                    parent_id=parent_interaction_id,
                    channel=channel,
                    metadata=meta_final,
                    output_data=outp,
                    sender_id=agent_id,
                    receiver_id=user_id,
                ))
            db_dur = int((time.time() - db_start) * 1000)
            yield {"type": "db", "level": "db",
                   "op": ("update_interaction" if _updated_streaming_row
                          else "insert_interaction"), "role": "assistant",
                   "tool_name": None, "id": inter_id, "ms": db_dur}

            # Interrupt check before the final response — the LLM stream may have
            # finished before the interrupt flag was set (common for short text-only
            # responses where the stream emits one chunk), and no other interrupt
            # check fires in the no-tool-calls path between the stream and here.
            if loop_config.is_enabled("interrupt_chk"):
                await _check_interrupt(session_id, interrupt_event, db=db)

            yield {"type": "response", "level": "agent", "message_phase": "main",
                   "content": prefix_content(_final_content), "asst_id": inter_id}
            _schedule_output_closer(
                user_id=user_id, session_id=session_id,
                agent_id=agent_id, final_asst_id=inter_id,
                parent_interaction_id=parent_interaction_id,
                db=db, channel=channel,
                audit_eligible=True,  # completed run — checklist audit may fire
                execution_mode=execution_mode,  # send-back re-runs under the same mode
                starting_execution_mode=_run_start_execution_mode,
            )
            # Cache the messages array for prompt-cache benefit on next turn
            try:
                await _store_validated_session_cache()
            except Exception:
                pass
            return

        # ── Stall guard stop — finalize cleanly (skip the max-turns message) ──
        if stall_stop_msg is not None:
            messages.append({"role": "assistant", "content": stall_stop_msg})
            meta_final = _build_meta(
                "assistant", input_tokens, output_tokens, llm_cost,
                message_phase="main",
            )
            outp = _assistant_output(
                messages=_sent_messages,
                tools=_sent_tools,
                include_snapshot=True,
            )
            db_start = time.time()
            inter_id = await db.insert_interaction(
                user_id, session_id, role="assistant", content=stall_stop_msg,
                parent_id=parent_interaction_id,
                channel=channel,
                metadata=meta_final,
                output_data=outp,
                sender_id=agent_id,
                receiver_id=user_id,
            )
            db_dur = int((time.time() - db_start) * 1000)
            yield {"type": "db", "level": "db",
                   "op": "insert_interaction", "role": "assistant",
                   "tool_name": None, "id": inter_id, "ms": db_dur}
            yield {"type": "response", "level": "agent", "message_phase": "main",
                   "content": prefix_content(stall_stop_msg), "asst_id": inter_id}
            _schedule_output_closer(
                user_id=user_id, session_id=session_id,
                agent_id=agent_id, final_asst_id=inter_id,
                parent_interaction_id=parent_interaction_id,
                db=db, channel=channel,
            )
            try:
                await _store_validated_session_cache()
            except Exception:
                pass
            return

        # ── Max turns reached ──
        yield {"type": "pipeline", "level": "pipeline",
               "step": "max_turns_reached", "turn": turn_count,
               "max_turns": max_turns,
               "message": f"Reached maximum {max_turns} turns"}
        max_turns_msg = (
            f"I've reached the maximum number of turns ({max_turns}). "
            "Reply 'continue' or 'keep going' and I'll pick up where I left off."
        )
        messages.append({"role": "assistant", "content": max_turns_msg})
        meta_final = _build_meta(
            "assistant", input_tokens, output_tokens, llm_cost,
            message_phase="main",
        )
        outp = _assistant_output(
            messages=_sent_messages,
            tools=_sent_tools,
            include_snapshot=True,
        )
        inter_id = await db.insert_interaction(
            user_id, session_id, role="assistant", content=max_turns_msg,
            parent_id=parent_interaction_id,
            channel=channel,
            metadata=meta_final,
            output_data=outp,
            sender_id=agent_id,
            receiver_id=user_id,
        )
        yield {"type": "db", "level": "db",
               "op": "insert_interaction", "role": "assistant",
               "tool_name": None, "id": inter_id}
        yield {"type": "response", "level": "agent", "message_phase": "main",
               "content": prefix_content(max_turns_msg), "asst_id": inter_id}
        _schedule_output_closer(
            user_id=user_id, session_id=session_id,
            agent_id=agent_id, final_asst_id=inter_id,
            parent_interaction_id=parent_interaction_id,
            db=db, channel=channel,
        )
        try:
            await _store_validated_session_cache()
        except Exception:
            pass
        return
    except asyncio.CancelledError as e:
        logger.error("Agent loop cancelled (session=%s turn=%d, content_len=%d): %s",
                     session_id, turn_count, len(collected_content), e)
        # Finalize any in-progress streaming answer so it isn't stranded as
        # 'streaming' forever — the partial text is kept, marked 'interrupted'.
        _cancel_persist_error = None
        if streaming_asst_id is not None:
            # Finalize the partial answer even though we're mid-cancellation. A HARD
            # cancel (a replace past the grace window, or the watchdog frozen-kill)
            # re-raises CancelledError at the next await — a bare `await` here would be
            # abandoned, stranding the row as status='streaming' (a forever "typing…"
            # bubble that only a server reboot clears). Shield the write so it runs to
            # completion, and swallow the re-raised cancel so the finalize isn't skipped.
            try:
                await asyncio.shield(db.update_interaction(
                    streaming_asst_id, content=collected_content, status="interrupted",
                ))
            except asyncio.CancelledError as _finalize_cancelled:
                # A second hard cancellation can interrupt the shield wait.  Do
                # not claim the partial answer was saved: surface the exact
                # failure to the turn runner and its durable run-state record.
                _cancel_persist_error = (
                    "Assistant response could not be finalized after cancellation "
                    f"(cancellation interrupted the database write: {_finalize_cancelled!r})."
                )
                logger.error("%s session=%s assistant=%s", _cancel_persist_error,
                             session_id, streaming_asst_id)
            except Exception as _finalize_error:
                _cancel_persist_error = (
                    "Assistant response could not be finalized after cancellation: "
                    f"{_finalize_error}"
                )
                logger.exception("%s session=%s assistant=%s", _cancel_persist_error,
                                 session_id, streaming_asst_id)
        if _cancel_persist_error:
            # The caller persists this as status=error with the existing stop
            # cause intact (for example 'frozen' or 'replaced'), and sends a red
            # error event rather than a misleading successful interruption.
            yield {"type": "error", "level": "agent", "message": _cancel_persist_error,
                   "asst_id": streaming_asst_id, "persistence_failure": True}
        else:
            yield {"type": "interrupted", "level": "agent", "message": str(e), "asst_id": streaming_asst_id}
        # Fire the closer for the interrupted run too — the user must still
        # get a Summary covering everything since the last summary (the partial
        # answer is already persisted above with the collected content, and a
        # new user message that caused the interrupt is already in the DB). A
        # run cancelled before any assistant row existed (streaming_asst_id is
        # None) has nothing to close out and is skipped.
        if streaming_asst_id is not None:
            _schedule_output_closer(
                user_id=user_id, session_id=session_id,
                agent_id=agent_id, final_asst_id=streaming_asst_id,
                parent_interaction_id=parent_interaction_id,
                db=db, channel=channel,
            )
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
        # Same as the cancel path: an errored run still deserves a Summary of
        # what was completed before the failure (the partial answer above).
        if streaming_asst_id is not None:
            _schedule_output_closer(
                user_id=user_id, session_id=session_id,
                agent_id=agent_id, final_asst_id=streaming_asst_id,
                parent_interaction_id=parent_interaction_id,
                db=db, channel=channel,
            )
        return


async def stream_agent_events(*args, **kwargs) -> AsyncGenerator[Dict[str, Any], None]:
    """Run the agent loop under the native anonymous concurrency lease.

    Keeping the lease around the generator—not the HTTP request—also covers
    background `/send`, browser-authority streaming, resumed runs, and callers
    that invoke the loop without an HTTP response lifecycle.
    """
    user_id = str(kwargs.get("user_id") or (args[0] if args else ""))
    session_id = str(kwargs.get("session_id") or (args[1] if len(args) > 1 else ""))
    lease_id = None
    anonymous_error = ""
    agent_id = str(kwargs.get("agent_id") or (args[4] if len(args) > 4 else ""))
    is_visitor = await _agent_profile_visitor(agent_id, user_id)
    if is_visitor:
        from app.api.rate_limit import begin_anonymous_run
        pool_key = "platform"
        public_maximum = None
        if agent_id and agent_id != "shared_default":
            try:
                from app.db import get_db
                from app.agent.public_policy import normalize_public_access
                public_agent = await get_db().get_agent_by_id(agent_id)
                public_policy = normalize_public_access(public_agent or {})
                pool_key = f"agent:{agent_id}"
                public_maximum = int(public_policy["usage"]["concurrent_runs"])
            except Exception:
                # The earlier funding gate is authoritative. Falling back to the
                # restrictive platform pool cannot grant extra concurrency.
                pool_key, public_maximum = "platform", None
        try:
            lease_id = await asyncio.to_thread(
                begin_anonymous_run, user_id, session_id, pool_key, public_maximum,
            )
        except RuntimeError as exc:
            if str(exc) == "anonymous_concurrency_limit":
                yield {
                    "type": "error",
                    "level": "agent",
                    "code": "registration_required",
                    "scope": "anonymous_concurrency",
                    "message": (
                        "Anonymous chat is at its concurrent-run limit. "
                        "Wait for a run to finish or register and sign in."
                    ),
                }
                return

            raise
    try:
        async for event in _stream_agent_events_impl(*args, **kwargs):
            if event.get("type") == "error" and event.get("code") != "registration_required":
                anonymous_error = str(event.get("message") or event.get("code") or "agent error")
            yield event
    finally:
        if anonymous_error and is_visitor:
            from app.api.rate_limit import record_anonymous_run_error
            await asyncio.to_thread(
                record_anonymous_run_error, user_id, anonymous_error,
            )
        if lease_id:
            from app.api.rate_limit import end_anonymous_run
            await asyncio.to_thread(end_anonymous_run, lease_id)


async def _agent_profile_visitor(agent_id: str, user_id: str) -> bool:
    if str(user_id or "").startswith("anon_"):
        return True
    if not agent_id:
        return False
    try:
        from app.agent.profiles import resolve_member, VISITOR
        member = await resolve_member(agent_id, user_id, auto_link_app=False)
        return ((member or {}).get("profile") or {}).get("slug") == VISITOR
    except Exception:
        return False


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
    execution_mode: str = 'ask',
    attachment_docs=None,
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
            execution_mode=execution_mode,
            # Raw attachment rows (images + files) for alternate engines that read
            # them off disk via their own tools (e.g. claude_code). The default loop
            # ignores this — it inlines images into user_message itself. Lets a turn
            # handed to another device (app/devices/worker.py) still carry attachments.
            attachment_docs=attachment_docs,
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
                    # Honest failure, not a fake success: log the REAL error text
                    # (the loop's error event otherwise never reaches server.log —
                    # the observability gap that hid a crashed fix-loop) and
                    # return "" so every caller sees the failure. Supervisors
                    # classify empty as a resumable failure; the old fabricated
                    # "I encountered an error: …" was recorded as 'complete' and
                    # could never be revived.
                    logger.error(
                        "Agent loop error (session=%s): %s", session_id,
                        event.get("message") or "unknown error",
                    )
                    return ""
            elif event["type"] == "interrupted":
                if not final_response:
                    logger.warning(
                        "Agent loop interrupted (session=%s): %s", session_id,
                        event.get("message") or "interrupted",
                    )
                    final_response = f"I was interrupted: {event['message']}"

        # No terminal event (response / error / interrupted) was seen — the
        # loop ended without producing anything. Return "" so every caller can
        # distinguish "no output" from a real completion: a fabricated
        # success-looking string here let failures pass as 'complete' (the
        # audit re-run classifier trusted it), stranding the task with an
        # errored bubble and no resume. Callers that deliver to users guard
        # against empty; supervisors classify empty as a resumable failure.

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

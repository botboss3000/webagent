"""
Per-agent RUN CONTRACTS — mechanical rules enforced inside the main agent loop.

The contract is the working agent's OWN discipline layer, deliberately separate
from both the mode gates (ask/plan/auto — what the agent may do) and the
Manager (``app/agent/manager.py`` — background supervisory verdicts). It is
configured per-agent as ``metadata['contract']`` (a JSON object) and enforced
synchronously in ``loop.py`` with ZERO extra LLM calls: every rule is a flag or
counter the loop already keeps.

Supported rules (all optional):

- ``require_plan_before_edit`` (bool) — in AUTO mode the first write/edit tool
  is blocked until the user's latest message reads as approval/continuation
  sent AFTER the original request. Plan/Ask modes pass automatically — the
  guardrails gate already enforces confirmation there. This is the machine
  analogue of "verify the plan before it edits", with no LLM involved.
- ``require_verify_after_edit`` (bool) — after a write/edit tool executes, the
  NEXT tool call must be a read/verify tool (read_source, git diff, a safe
  run_command, …). Anything else is blocked with an actionable message telling
  the agent to verify its change first. Prevents edit-edit-edit without ever
  checking the result.
- ``max_tool_errors`` (int) — consecutive failed tool results beyond this count
  trip a ONE-TIME contract strike (a nudge via a tool_result, not a hard stop;
  the stall guard owns the hard stop). 0 = disabled.
- ``max_tool_calls_per_turn`` (int) — a global cap on executed tool calls per
  turn (per-tool budgets already exist separately). 0 = disabled.

The active rules are injected into the system prompt as a ``## Contract`` block
so the agent KNOWS its contract — the same visibility pattern the execution-mode
prompt uses (``app/agent/loop.py``). Mechanical violations return an actionable
``tool_result`` error (``error_type="contract_blocked"``) exactly like the
guardrail/validation paths, so the agent sees why and adjusts.

The loop node is ``contract_chk`` (see ``app/agent/loop_executor.py``) — it can
be disabled per-agent via loop_logic like any other node. Enforcement is
fail-open: a missing/broken contract config simply disables the rules.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

# ── Canonical tool sets ──────────────────────────────────────────────────────
# Write/edit tools that change the repository (mirror of the _PATH_TOOLS set in
# app/agent/session_changes.py, plus commit). run_command/run_python/git_tool
# are deliberately NOT auto-classified as edits: whether they mutate depends on
# their arguments, so the contract only treats the unambiguous file writers as
# edits. (A mutating run_command still trips the mode gate in plan/ask.)
EDIT_TOOLS: frozenset = frozenset({
    "write_source", "edit_source", "patch_source", "delete_source",
    "resolve_conflict",
})

COMMIT_TOOLS: frozenset = frozenset({"commit_and_push"})

# Tools that inspect rather than mutate — satisfying ``require_verify_after_edit``.
# run_command / git_tool only count when their arguments are read-only
# (mirrors the guardrail per-arg exemptions in loop.py).
_VERIFY_TOOL_NAMES: frozenset = frozenset({
    "read_source", "read_directory", "search_source", "search_comments",
    "get_genui_data", "get_genui_logs", "list_genui", "session_search",
    "list_my_agents", "list_agent_templates", "list_webhooks", "list_skills",
})

# Read-only git_tool operations (mirror of SAFE_GIT_OPERATIONS in loop.py).
_SAFE_GIT_OPERATIONS: frozenset = frozenset({
    "status", "log", "diff", "show", "ls-files", "rev-parse", "blame",
    "describe", "for-each-ref", "reflog", "cat-file", "shortlog", "name-rev",
})

# Read-only shell prefixes (mirror of SAFE_RUN_COMMAND_PREFIXES in loop.py).
_SAFE_SHELL_PREFIXES: tuple = (
    "cd",
    "git status", "git log", "git diff", "git show", "git branch",
    "git ls-files", "git rev-parse", "git remote", "git stash list",
    "git config --get", "git config --list",
    "ls", "dir", "pwd", "tree", "stat", "file",
    "cat", "head", "tail", "type", "more",
    "find", "grep", "rg", "where", "which",
    "wc", "du", "df",
    "whoami", "hostname", "date", "uname", "id", "groups", "uptime",
    "ps", "env", "printenv", "echo",
    "python --version", "python -V", "python3 --version", "python3 -V",
    "pip list", "pip show", "pip --version",
    "node --version", "node -v",
    "npm list", "npm ls", "npm --version", "npm -v",
)
_HARD_UNSAFE_TOKENS: tuple = (">", "<", "`", "$(", "|&")
_CHAIN_SEPARATOR_RE = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")

# Approval keywords — the user's latest message must read as explicit
# approval/continuation (word-boundary matched, so "ok" can't match inside
# "looking at"). Mirror of the confirm set in loop._check_user_confirmed.
_CONFIRM_KEYWORDS: tuple = (
    "yes", "go ahead", "proceed", "approved", "ok", "okay", "sure", "do it",
    "confirm", "go for it", "please do", "continue", "looks good",
    "sounds good", "that's fine", "approved",
)

# Default contract prompt block template — renders only the ACTIVE rules so the
# agent is told exactly what is being enforced.
_CONTRACT_PROMPT_TEMPLATE = (
    "You are working under a run contract that the loop enforces:\n{bullets}"
)

_RULE_TEXT: Dict[str, str] = {
    "require_plan_before_edit": (
        "- Before your FIRST file edit, present your plan and get the user's "
        "explicit go-ahead (a new message approving it). The original request "
        "is not approval. Edits attempted before approval are blocked."
    ),
    "require_verify_after_edit": (
        "- After each file edit, verify it before moving on (read the file "
        "back, run a test, or check a diff). Continuing without verifying is "
        "blocked."
    ),
    "max_tool_errors": (
        "- If tool calls keep failing, stop retrying the same approach: "
        "change tactics, ask a clarifying question, or report the blocker."
    ),
    "max_tool_calls_per_turn": (
        "- Keep the number of tool calls per turn within the configured budget; "
        "bundle reads and avoid redundant calls."
    ),
}


# ── Parsing ──────────────────────────────────────────────────────────────────

def parse_contract(agent_rec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve the per-agent run contract from ``metadata['contract']``.

    Accepts a JSON object (the canonical form) or a JSON string. Any error,
    wrong type, or missing config yields ``{}`` — fail-open: no contract rules
    are enforced. (An app-level default in app-prompts.json can be layered on
    later; per-agent is the v1 surface.)
    """
    if not agent_rec:
        return {}
    meta = agent_rec.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta) or {}
        except Exception:
            meta = {}
    if not isinstance(meta, dict):
        return {}
    raw = meta.get("contract")
    if raw is None:
        return {}
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return {}
        try:
            raw = json.loads(s)
        except Exception:
            return {}
    if not isinstance(raw, dict):
        return {}
    # Coerce known rule fields to their canonical types (never trust the JSON).
    out: Dict[str, Any] = {}
    for key in ("require_plan_before_edit", "require_verify_after_edit"):
        out[key] = bool(raw.get(key, False))
    for key in ("max_tool_errors", "max_tool_calls_per_turn"):
        try:
            val = int(raw.get(key) or 0)
        except (TypeError, ValueError):
            val = 0
        out[key] = max(0, val)
    return out


def active_rules(contract: Dict[str, Any]) -> List[str]:
    """Names of the rules that are actually on for this contract."""
    rules: List[str] = []
    if contract.get("require_plan_before_edit"):
        rules.append("require_plan_before_edit")
    if contract.get("require_verify_after_edit"):
        rules.append("require_verify_after_edit")
    if (contract.get("max_tool_errors") or 0) > 0:
        rules.append("max_tool_errors")
    if (contract.get("max_tool_calls_per_turn") or 0) > 0:
        rules.append("max_tool_calls_per_turn")
    return rules


def contract_prompt_block(contract: Dict[str, Any]) -> str:
    """The ``## Contract`` system-prompt block for the active rules ("" when
    no rules are configured)."""
    rules = active_rules(contract)
    if not rules:
        return ""
    bullets = "\n".join(_RULE_TEXT[r] for r in rules)
    return _CONTRACT_PROMPT_TEMPLATE.format(bullets=bullets)


# ── Tool classification ──────────────────────────────────────────────────────

def is_edit_tool(name: str) -> bool:
    return name in EDIT_TOOLS


def is_commit_tool(name: str) -> bool:
    return name in COMMIT_TOOLS


def is_verify_tool(name: str, tool_args: Any = None) -> bool:
    """True when the call reads/verifies rather than mutates — satisfies
    ``require_verify_after_edit``. run_command / git_tool only count with
    read-only arguments (mirrors the guardrail per-arg exemptions)."""
    if name in _VERIFY_TOOL_NAMES:
        return True
    args = tool_args if isinstance(tool_args, dict) else {}
    if name == "git_tool":
        op = str(args.get("operation", "")).strip().lower()
        return op in _SAFE_GIT_OPERATIONS
    if name == "run_command":
        return _is_safe_shell_command(args.get("command", ""))
    if name == "http_request":
        return str(args.get("method", "GET")).strip().lower() in ("get", "head", "options")
    if name == "browser_action":
        return str(args.get("action", "")).strip().lower() in (
            "navigate", "get_text", "get_html", "screenshot", "wait", "title", "url", "close",
        )
    return False


def _is_safe_shell_command(command: Any) -> bool:
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
        if not any(low == p or low.startswith(p + " ") for p in _SAFE_SHELL_PREFIXES):
            return False
    return True


# ── Approval detection ───────────────────────────────────────────────────────

def user_approved_plan(messages: List[Dict[str, Any]]) -> bool:
    """True when the user's LATEST message is an explicit approval/continuation
    sent AFTER the original request.

    The first user message is never approval (the plan-mode rule: the original
    request predates any plan). Only a newer user message that reads as
    approval counts. Word-boundary matching so "ok" can't match inside
    "looking at" — same discipline as loop._check_user_confirmed.
    """
    last_user_idx: Optional[int] = None
    first_user_idx: Optional[int] = None
    for i, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        if first_user_idx is None:
            first_user_idx = i
        last_user_idx = i
    if last_user_idx is None or first_user_idx is None:
        return False
    if last_user_idx == first_user_idx:
        return False  # the original request alone is never approval
    text = (messages[last_user_idx].get("content") or "").lower()
    return any(
        re.search(r"\b" + re.escape(kw) + r"\b", text)
        for kw in _CONFIRM_KEYWORDS
    )


# ── Mechanical checks (read state, return a violation message or None) ──────

def check_plan_before_edit(
    contract: Dict[str, Any],
    state: Dict[str, Any],
    tool_name: str,
    execution_mode: str,
) -> Optional[str]:
    """Block the first edit in AUTO mode until the user approved the plan.

    Plan/Ask modes pass automatically (the guardrails gate already enforces
    confirmation there). In Auto, the first edit needs a NEW user approval
    after the original request — the machine analogue of the plan gate with
    zero LLM cost. Only the FIRST edit is gated; once the plan is approved
    (or an edit has run), the rule is satisfied for the rest of the run.
    """
    if not contract.get("require_plan_before_edit"):
        return None
    if not is_edit_tool(tool_name):
        return None
    if execution_mode in ("plan", "ask"):
        return None  # the mode gate owns confirmation here
    if state.get("plan_approved") or state.get("edit_executed"):
        return None
    return (
        "Contract: no plan approval on record for this edit. You are in AUTO "
        "mode with the 'plan before edit' contract rule enabled — present your "
        "plan and get the user's explicit go-ahead (a NEW message approving "
        "it) before making file changes. The original request does not count "
        "as approval. I did not run this edit."
    )


def check_verify_after_edit(
    contract: Dict[str, Any],
    state: Dict[str, Any],
    tool_name: str,
    tool_args: Any,
) -> Optional[str]:
    """Block non-verify tools after an edit until a verify tool runs."""
    if not contract.get("require_verify_after_edit"):
        return None
    if not state.get("awaiting_verify"):
        return None
    if is_verify_tool(tool_name, tool_args):
        return None
    return (
        "Contract: you edited a file but have not verified the change. Run a "
        "verify step first — read the file back, run a test, or check a diff "
        "(read_source / run_command with a read-only command / git_tool diff). "
        "I did not run this tool."
    )


def check_tool_cap(
    contract: Dict[str, Any],
    state: Dict[str, Any],
) -> Optional[str]:
    """Global per-turn tool-call cap (0 = disabled)."""
    cap = int(contract.get("max_tool_calls_per_turn") or 0)
    if cap <= 0:
        return None
    if int(state.get("tool_calls_this_turn") or 0) >= cap:
        return (
            f"Contract: you have reached the per-turn tool-call budget of "
            f"{cap}. Stop calling tools this turn — synthesize what you have, "
            f"or give the user your best answer. I did not run this tool."
        )
    return None


def error_strike(
    contract: Dict[str, Any],
    state: Dict[str, Any],
) -> Optional[str]:
    """ONE-TIME nudge when consecutive tool errors cross the threshold.

    Returns a message only once per run (guarded by ``error_strike_sent``,
    set here when the strike fires); the stall guard owns the hard stop, this
    is a soft course-correction.
    """
    threshold = int(contract.get("max_tool_errors") or 0)
    if threshold <= 0:
        return None
    if state.get("error_strike_sent"):
        return None
    if int(state.get("consecutive_errors") or 0) >= threshold:
        state["error_strike_sent"] = True
        return (
            f"Contract: {state.get('consecutive_errors')} consecutive tool "
            f"calls have failed (limit {threshold}). Stop retrying the same "
            f"approach — change tactics, ask a clarifying question, or report "
            f"the blocker to the user."
        )
    return None


def mechanical_check(
    contract: Dict[str, Any],
    state: Dict[str, Any],
    *,
    tool_name: str,
    tool_args: Any = None,
    execution_mode: str = "ask",
) -> Optional[str]:
    """Run every mechanical rule for one tool call, in order.

    Returns the first violation message (the loop blocks the call and feeds
    the message back as an actionable tool_result) or None when the call may
    proceed. Pure and synchronous — no LLM, no I/O.
    """
    msg = check_plan_before_edit(contract, state, tool_name, execution_mode)
    if msg:
        return msg
    msg = check_verify_after_edit(contract, state, tool_name, tool_args)
    if msg:
        return msg
    msg = check_tool_cap(contract, state)
    if msg:
        return msg
    return error_strike(contract, state)


# ── State mutations (called by the loop at execution time) ──────────────────

def new_contract_state() -> Dict[str, Any]:
    """Fresh per-run contract state (dies with the run — the 'short state')."""
    return {
        "plan_approved": False,       # user approved the plan (or an edit ran)
        "edit_executed": False,       # at least one edit has run
        "awaiting_verify": False,     # an edit ran; verify required before next tool
        "consecutive_errors": 0,      # consecutive failed tool results
        "error_strike_sent": False,   # one-time soft strike delivered
        "tool_calls_this_turn": 0,    # executed tool calls this turn
    }


def mark_tool_scheduled(state: Dict[str, Any]) -> None:
    """Called when a tool call passes validation/guardrails (counts toward the
    per-turn cap; the cap blocks BEFORE execution, so counting here is exact)."""
    state["tool_calls_this_turn"] = int(state.get("tool_calls_this_turn") or 0) + 1


def mark_tool_outcome(state: Dict[str, Any], success: bool) -> None:
    """Called after a tool executes — tracks the consecutive-error counter."""
    if success:
        state["consecutive_errors"] = 0
    else:
        state["consecutive_errors"] = int(state.get("consecutive_errors") or 0) + 1


def mark_edit_executed(state: Dict[str, Any]) -> None:
    """Called after an edit tool succeeds."""
    state["edit_executed"] = True
    state["plan_approved"] = True   # an edit ran ⇒ the plan is de facto approved
    state["awaiting_verify"] = True


def mark_verify_executed(state: Dict[str, Any]) -> None:
    """Called after a verify tool succeeds — clears the verify-await flag."""
    state["awaiting_verify"] = False

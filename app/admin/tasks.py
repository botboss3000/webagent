"""
Admin endpoints for TASK grouping inside a chat session.

A "task" is a run of consecutive turns that belong together: one request, the
agent's plan + execution for it, and any short approval/feedback turns the user
adds along the way. The chat DB only knows two levels — the session and the turn
(a `role='user'` row plus every assistant/tool row that hangs off it). There is
NO `task_id` anywhere. This module adds the missing middle level WITHOUT a
migration, exactly like the Optimizer Runs dashboard derives runs at read time
(see app/admin/optimizer.py): we reconstruct the turns of a session and then run
an INFERENCE layer over them to decide where one task ends and the next begins.

The first pass of boundary inference is intentionally **text-only and stateless**:
a new user turn CONTINUES the current task only when it reads as a short reaction
/ approval / feedback ("yes go ahead", "thanks", "no, redo it") or an explicit
continuation phrase ("also…", "and…", "now…"). Everything else would, on the
keyword rule alone, open a new task. Timing is NOT used — the same task is very
often resumed after a long gap, so an idle gap must never split a task.

**LLM tie-breaker (the fast second pass).** The keyword rule is deliberately
high-precision, so it over-splits: a follow-up like "make that bigger" or "why
did it do that" reads as a brand-new request and gets its own task even though it
clearly belongs to the previous one. To glue those back together, whenever the
keyword rule would OPEN A NEW TASK we run one cheap, single-shot LLM call that
looks at the last few **user messages only** (never the assistant's replies or
tool output) plus the new message and decides SAME vs NEW. SAME folds the turn
into the current task; NEW (or any failure / disabled / no-credentials) keeps the
keyword verdict. The prompt lives in ``app/defaults/app-prompts.json`` under
``app_level_prompts.task_grouping_classifier`` and the call is bounded (short
timeout, no retries) so grouping never hangs and degrades cleanly to the text
rule. Results are memoised by message text so re-opening a session is free.

The per-session detail endpoint uses the LLM tie-breaker; the multi-session
overview stays text-only (one read fans out over many sessions — an LLM call per
boundary there would be a storm).

`_decide_boundary` (text rule) and `_llm_decide_related` (LLM tie-breaker) are
kept isolated from the turn reconstruction and the endpoints so either can evolve
on its own. NOTE: the chat-side task-frame overlay
(``ui/chat-side-panel/js/chat-task-frames.js``) still mirrors the text-only rule
client-side for an instant draw, so its frames can differ from this endpoint's
LLM-refined grouping; wiring it to this endpoint is a possible follow-up.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Query

from app.util.paths import app_prompts_path

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/settings", tags=["admin"])

# app-prompts catalog (bundled default under app/defaults/, or a data/ override).
_APP_PROMPTS_PATH = app_prompts_path()


# Short reactions / approvals / feedback — these ATTACH to the prior task, even
# the negative ones ("no, redo it" is still feedback ON that task, not a new one).
_REACTION_PHRASES = {
    "yes", "yep", "yeah", "y", "ok", "okay", "k", "sure", "go", "go ahead",
    "proceed", "continue", "do it", "do that", "please do", "yes please",
    "sounds good", "looks good", "lgtm", "perfect", "great", "nice", "cool",
    "thanks", "thank you", "ty", "approved", "approve", "confirm", "confirmed",
    "make it so", "ship it", "no", "nope", "stop", "wait", "cancel", "redo",
    "try again", "fix it", "not quite", "that's wrong", "thats wrong",
}
_REACTION_PREFIXES = (
    "yes", "yep", "yeah", "ok", "okay", "sure", "go ahead", "proceed",
    "do it", "do that", "please do", "thanks", "thank you", "perfect",
    "great", "no ", "nope", "redo", "try again", "fix ", "looks good",
    "sounds good", "approved", "confirm",
)

# Connectives that signal the user is adding to the SAME task ("also do X").
# High-precision only: bare "and / then / next / now" are deliberately excluded —
# "Now write a backup script" is a NEW task, not a continuation of the last one.
_CONTINUATION_PREFIXES = (
    "also", "and also", "plus", "additionally", "what about", "how about",
    "one more", "another", "btw", "by the way", "oh and", "can you also",
    "could you also",
)


# Some ``role='user'`` rows are NOT genuine user requests — they are synthetic
# messages the runtime injects to drive the agent (an orchestration spawn finishing,
# a follow-up timer, an event trigger, an optimizer kick). They must NEVER open a
# task boundary, or every background wake-up would shatter a session into one task
# per event. Detected by content marker or by source. Kept in sync with the chat
# frontend's mirror in ui/chat-side-panel/js/chat-task-frames.js.
_SYNTHETIC_USER_PREFIXES = (
    "[orchestration event]",
    "[event trigger]",
    "[automation event]",
    "[scheduled event]",
)
_SYNTHETIC_USER_SOURCES = ("optimizer:trigger", "optimizer:init", "automation", "event", "scheduler")


def _is_synthetic_user(content: str, source: Optional[str]) -> bool:
    low = (content or "").lstrip().lower()
    if any(low.startswith(p) for p in _SYNTHETIC_USER_PREFIXES):
        return True
    src = (source or "").strip().lower()
    return bool(src) and src in _SYNTHETIC_USER_SOURCES


def _tokens_from_meta(meta_raw: Optional[str]) -> int:
    if not meta_raw:
        return 0
    try:
        md = json.loads(meta_raw)
    except Exception:
        return 0
    if not isinstance(md, dict):
        return 0
    return int(md.get("input_tokens") or 0) + int(md.get("output_tokens") or 0)


# ── Turn reconstruction ────────────────────────────────────────────────────────
def _load_turns(conn, session_id: str) -> List[Dict[str, Any]]:
    """Rebuild the ordered turns of a session.

    A new turn opens at every GENUINE ``role='user'`` row; assistant/tool/system
    rows attach to the open turn. System rows before the first user message (e.g.
    an optimizer:init seed) are ignored — they don't start a user turn. Synthetic
    user rows (orchestration/event/optimizer injections — see _is_synthetic_user)
    do NOT open a turn either: they attach to the open turn like an assistant row,
    so a background wake-up never splits a session into a new task.
    """
    rows = conn.execute(
        "SELECT id, role, content, tool_name, metadata, created_at, session_seq, source "
        "FROM interactions WHERE session_id = ? "
        "ORDER BY created_at ASC, COALESCE(session_seq, 999999999) ASC",
        (session_id,),
    ).fetchall()

    turns: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for r in rows:
        row = dict(r)
        role = (row.get("role") or "").lower()
        content = row.get("content") or ""
        created = row.get("created_at")

        if role == "user" and not _is_synthetic_user(content, row.get("source")):
            cur = {
                "index": len(turns),
                "root_id": row.get("id"),
                "prompt": content,
                "started_at": created,
                "ended_at": created,
                "tools": [],
                "tokens": _tokens_from_meta(row.get("metadata")),
                "msg_count": 1,
                "last_assistant": "",
            }
            turns.append(cur)
            continue

        if cur is None:
            continue  # stray assistant/system before any user turn

        cur["ended_at"] = created or cur["ended_at"]
        cur["msg_count"] += 1
        cur["tokens"] += _tokens_from_meta(row.get("metadata"))
        tn = row.get("tool_name")
        if tn and tn not in cur["tools"]:
            cur["tools"].append(tn)
        if role == "assistant" and content:
            cur["last_assistant"] = content
    return turns


# ── Boundary inference (text-only; the part an LLM could later sharpen) ─────────
def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _classify_user_turn(text: str) -> Dict[str, Any]:
    low = _norm(text)
    stripped = low.strip(" .!?,")
    words = len(low.split())

    is_reaction = (
        words <= 2
        or stripped in _REACTION_PHRASES
        or any(stripped == p or stripped.startswith(p) for p in _REACTION_PREFIXES)
    ) and words <= 6

    is_continuation = any(low.startswith(p) for p in _CONTINUATION_PREFIXES)

    return {
        "words": words,
        "is_reaction": bool(is_reaction),
        "is_continuation": bool(is_continuation),
    }


def _decide_boundary(curr_turn: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """Decide if ``curr_turn`` STARTS A NEW TASK (True) or continues (False).

    Text-only and stateless — depends solely on the wording of this user turn,
    never on timing. Returns (is_new, reason, signals); the signals dict is kept
    so the frontend can show "why" and so a future LLM classifier can be slotted
    in on the ambiguous fall-through branch without changing anything else.
    """
    c = _classify_user_turn(curr_turn.get("prompt", ""))

    # Short reaction / approval / feedback always attaches to the prior task.
    if c["is_reaction"]:
        return False, "short reaction or approval", c

    # An explicit continuation phrase ("also…", "and…") continues the task.
    if c["is_continuation"]:
        return False, "continuation phrase", c

    # Keyword rule says NEW — but this is the ambiguous case the LLM tie-breaker
    # (`_llm_decide_related`) gets to override. Flag it so the orchestrator knows
    # an LLM second opinion is worth taking here.
    c["ambiguous"] = True
    return True, "new request", c


# ── LLM tie-breaker (the fast second pass over the ambiguous boundaries) ────────
# On by default; set TASK_GROUPING_LLM=0 to force the text-only rule everywhere.
_LLM_DEFAULT_ENABLED = os.environ.get("TASK_GROUPING_LLM", "1").strip().lower() not in ("0", "false", "no", "off")
# How many prior USER messages the classifier sees as context.
_LLM_PRIOR_USER_MSGS = max(1, int(os.environ.get("TASK_GROUPING_LLM_CONTEXT", "5") or 5))
_LLM_TIMEOUT = 12.0

# Memoise verdicts by (recent user msgs, new msg) so re-reading a session is free
# and we never pay twice for the same boundary. Text-keyed ⇒ safe to cache.
_llm_cache: Dict[Tuple[str, ...], bool] = {}
_LLM_CACHE_MAX = 4000

_FALLBACK_CLASSIFIER_PROMPT = (
    "You decide whether a new user message continues the SAME task as the recent "
    "ones, or starts a NEW task. You are shown only the user's own recent messages "
    "(not the assistant's replies). SAME = it refers to, builds on, refines, or "
    "asks about the same thing; NEW = it switches to an unrelated subject. When "
    "unsure, prefer SAME.\n\nRecent user messages (oldest first):\n"
    "{recent_user_messages}\n\nNew user message:\n{new_message}\n\n"
    "Reply with ONE word only: SAME or NEW."
)


def _load_classifier_prompt() -> str:
    """Read the task-grouping classifier template from app-prompts.json.

    Falls back to a built-in template if the file is missing/broken so grouping
    never breaks on a config error.
    """
    try:
        data = json.loads(_APP_PROMPTS_PATH.read_text(encoding="utf-8"))
        entry = (data.get("app_level_prompts") or {}).get("task_grouping_classifier") or {}
        tpl = entry.get("template") or entry.get("text")
        if isinstance(tpl, str) and tpl.strip():
            return tpl
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("task grouping: could not read classifier prompt: %s", e)
    return _FALLBACK_CLASSIFIER_PROMPT


def _classifier_model() -> str:
    return (
        os.environ.get("LLM_MODEL")
        or os.environ.get("OPENROUTER_MODEL")
        or "deepseek/deepseek-v4-flash"
    )


def _classifier_client():
    """Bounded LLM client for the grouping tie-breaker — same provider config the
    chat path uses, but with a short timeout and NO retries so a flaky call fails
    once and we fall back to the keyword verdict instead of stalling a read."""
    base_url = (
        os.environ.get("LLM_BASE_URL")
        or os.environ.get("OPENROUTER_BASE_URL")
        or "https://openrouter.ai/api/v1"
    )
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or ""
    if not api_key:
        return None
    try:
        from openai import AsyncOpenAI
        return AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=_LLM_TIMEOUT, max_retries=0)
    except ImportError:  # pragma: no cover
        try:
            from app.openai_compat import AsyncOpenAI
            return AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=_LLM_TIMEOUT)
        except Exception:
            return None
    except Exception:
        return None


async def _llm_decide_related(prior_user_msgs: List[str], new_msg: str) -> Optional[bool]:
    """Ask the fast model: does ``new_msg`` continue the same task as the recent
    user messages? Returns True (SAME / keep in current task), False (NEW), or
    None when the call can't be made (disabled, no creds, error) so the caller
    falls back to the keyword verdict. Best-effort: never raises.
    """
    if not _LLM_DEFAULT_ENABLED:
        return None
    # App-level on/off: Task Grouping is an app_function (App Settings ▸ App
    # Functions). When an admin turns it off, skip the LLM tie-breaker entirely
    # and fall back to the keyword verdict — no model calls. Checked live so the
    # toggle takes effect immediately (no restart). Fails ON (never silently
    # disables a default-on function if the catalog/config can't be read).
    try:
        from app.abilities import ability_app_enabled
        if not ability_app_enabled("task_grouping"):
            return None
    except Exception:
        pass
    recent = [m.strip() for m in prior_user_msgs if m and m.strip()][-_LLM_PRIOR_USER_MSGS:]
    new_msg = (new_msg or "").strip()
    if not recent or not new_msg:
        return None

    cache_key = tuple(recent) + (new_msg,)
    if cache_key in _llm_cache:
        return _llm_cache[cache_key]

    client = _classifier_client()
    if client is None:
        return None

    recent_block = "\n".join(f"- {m}" for m in recent)
    prompt = _load_classifier_prompt().format(
        recent_user_messages=recent_block, new_message=new_msg
    )
    try:
        resp = await client.chat.completions.create(
            model=_classifier_model(),
            messages=[{"role": "user", "content": prompt}],
            # Generous cap on purpose: the answer is one word, but the app's
            # configured model may be a REASONING model that spends hidden tokens
            # thinking first — too small a cap returns empty content (finish
            # reason "length") and the verdict would silently fall back. Only the
            # tokens actually used are billed, so this just removes a ceiling.
            max_tokens=512,
            temperature=0.0,
        )
        raw = (resp.choices[0].message.content or "").strip().upper()
        try:
            _u = getattr(resp, "usage", None)
            if _u:
                from plugins.billing.usage import record_background_usage
                await record_background_usage(
                    model=_classifier_model(),
                    input_tokens=getattr(_u, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(_u, "completion_tokens", 0) or 0,
                    label="task-group",
                )
        except Exception:
            pass
    except Exception as e:
        logger.warning("task grouping: classifier call failed: %s", e)
        return None

    if "SAME" in raw:
        verdict = True
    elif "NEW" in raw:
        verdict = False
    else:
        return None  # unparseable → fall back to keyword verdict

    if len(_llm_cache) < _LLM_CACHE_MAX:
        _llm_cache[cache_key] = verdict
    return verdict


# ── Title / excerpt helpers ────────────────────────────────────────────────────
def _title_from(text: str, limit: int = 60) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    line = re.sub(r"\s+", " ", line).strip()
    # Drop a leading politeness wrapper so the title leads with the verb/topic.
    line = re.sub(r"^(please\s+|can you\s+|could you\s+|can u\s+|i (?:want|need|would like) (?:you )?to\s+)",
                  "", line, flags=re.IGNORECASE).strip()
    if not line:
        return "(empty prompt)"
    if len(line) <= limit:
        return line
    cut = line[:limit].rsplit(" ", 1)[0]
    return (cut or line[:limit]).rstrip() + "…"


def _excerpt(text: str, limit: int = 160) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t if len(t) <= limit else t[:limit].rstrip() + "…"


# ── Grouping orchestration ─────────────────────────────────────────────────────
async def group_session_tasks(conn, session_id: str,
                        include_turns: bool = False,
                        use_llm: bool = True) -> Dict[str, Any]:
    turns = _load_turns(conn, session_id)
    tasks: List[Dict[str, Any]] = []

    def _new_task(turn: Dict[str, Any], reason: str) -> Dict[str, Any]:
        return {
            "index": len(tasks),
            "title": _title_from(turn.get("prompt", "")),
            "prompt": _excerpt(turn.get("prompt", ""), 280),
            "started_at": turn.get("started_at"),
            "ended_at": turn.get("ended_at"),
            "turn_count": 0,
            "turn_indices": [],
            "root_ids": [],
            "tools": [],
            "tokens": 0,
            "boundary_reason": reason,
            "last_assistant": "",
            "_turns": [],
        }

    cur_task: Optional[Dict[str, Any]] = None

    def _absorb(task: Dict[str, Any], turn: Dict[str, Any], cont_reason: str) -> None:
        task["turn_count"] += 1
        task["turn_indices"].append(turn["index"])
        if turn.get("root_id"):
            task["root_ids"].append(turn["root_id"])
        task["ended_at"] = turn.get("ended_at") or task["ended_at"]
        task["tokens"] += int(turn.get("tokens") or 0)
        for t in turn.get("tools", []):
            if t not in task["tools"]:
                task["tools"].append(t)
        if turn.get("last_assistant"):
            task["last_assistant"] = _excerpt(turn["last_assistant"])
        task["_turns"].append({
            "index": turn["index"],
            "root_id": turn.get("root_id"),
            "prompt": _excerpt(turn.get("prompt", ""), 200),
            "started_at": turn.get("started_at"),
            "tools": turn.get("tools", []),
            "tokens": int(turn.get("tokens") or 0),
            "msg_count": turn.get("msg_count", 1),
            "continued_reason": cont_reason,
        })

    for i, turn in enumerate(turns):
        if cur_task is None:
            cur_task = _new_task(turn, "session start")
            tasks.append(cur_task)
            _absorb(cur_task, turn, "first turn")
            continue
        is_new, reason, sig = _decide_boundary(turn)

        # The keyword rule says NEW, but this is the ambiguous fall-through — give
        # the fast LLM a vote, showing it only the prior USER messages. SAME folds
        # the turn into the current task; NEW (or no verdict) keeps the keyword
        # decision. The overview view passes use_llm=False to stay text-only.
        if is_new and use_llm and sig.get("ambiguous"):
            prior_user_msgs = [t.get("prompt", "") for t in turns[:i]]
            related = await _llm_decide_related(prior_user_msgs, turn.get("prompt", ""))
            if related is True:
                is_new, reason = False, "llm: same task"
            elif related is False:
                reason = "llm: new task"

        if is_new:
            cur_task = _new_task(turn, reason)
            tasks.append(cur_task)
            _absorb(cur_task, turn, "first turn")
        else:
            _absorb(cur_task, turn, reason)

    if not include_turns:
        for t in tasks:
            t.pop("_turns", None)
    else:
        for t in tasks:
            t["turns"] = t.pop("_turns", [])

    return {
        "session_id": session_id,
        "task_count": len(tasks),
        "turn_count": len(turns),
        "tasks": tasks,
    }


# ── Endpoints ──────────────────────────────────────────────────────────────────
@router.get("/tasks/session/{session_id}")
async def get_session_tasks(
    session_id: str,
    include_turns: bool = Query(True, description="Include the per-turn breakdown inside each task"),
):
    """Group one session's turns into inferred tasks (derived at read time)."""
    try:
        from app.db import get_db
        db = get_db()
        raw = getattr(db, "_get_conn", None)
        if not raw:
            return {"error": "no db", "session_id": session_id, "tasks": []}
        conn = raw()
        try:
            return await group_session_tasks(conn, session_id, include_turns=include_turns, use_llm=True)
        finally:
            conn.close()
    except Exception as e:
        logger.error("Failed to group tasks for session %s: %s", session_id, e)
        return {"error": str(e), "session_id": session_id, "tasks": []}


@router.get("/tasks/overview")
async def get_tasks_overview(
    user_id: str = Query(..., description="Owner user id"),
    limit: int = Query(50, ge=1, le=200),
):
    """One row per recent session with its inferred task count — for a list view."""
    try:
        from app.db import get_db
        db = get_db()
        raw = getattr(db, "_get_conn", None)
        if not raw:
            return []
        conn = raw()
        try:
            rows = conn.execute(
                "SELECT id, title, created_at, updated_at FROM sessions "
                "WHERE user_id = ? AND (status IS NULL OR status = 'active') "
                "ORDER BY updated_at DESC NULLS LAST LIMIT ?",
                (user_id, limit),
            ).fetchall()
            out = []
            for r in rows:
                row = dict(r)
                # Text-only here: one read fans out over many sessions, so an LLM
                # call per boundary would be a storm. The detail endpoint refines.
                grouped = await group_session_tasks(conn, row["id"], include_turns=False, use_llm=False)
                out.append({
                    "session_id": row["id"],
                    "title": row.get("title") or (row["id"][:12]),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "task_count": grouped["task_count"],
                    "turn_count": grouped["turn_count"],
                    "tasks": grouped["tasks"],  # already turn-free
                })
            return out
        finally:
            conn.close()
    except Exception as e:
        logger.error("Failed tasks overview for %s: %s", user_id, e)
        return []

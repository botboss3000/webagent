"""Chat endpoint for webAgent."""

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from typing import List, Any, Dict, Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from app.models.schemas import ChatRequest, ChatResponse
from app.db import get_db
from app.agent.prompts import (
    build_system_prompt,
    append_skills_section,
    build_user_message_content,
    format_attachments_for_prompt,
    CONTEXT_SECTION_TYPES,
)

from app.agent.loop import run_agent_loop_buffered, stream_agent_events
from app.agent.loop_executor import LoopConfig
from app.agent.session_history import build_openai_history_from_session, trim_history_for_resume
from app.agent.run_buffer import get_registry as get_run_buffer_registry
from app.agent.run_manager import get_run_manager
from app.optimizer.runner import run_optimizer_async
from app.agent import trigger_index
from plugins.billing.enforcement import check_access as billing_check_access

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

# ── Fire-and-forget background tasks ──
# asyncio only keeps a WEAK reference to a bare ``create_task`` result, so a task
# whose handle isn't stored can be garbage-collected before it runs — silently.
# Keep a strong reference for the task's lifetime and drop it (logging any
# exception) when it finishes, so post-turn work (e.g. the Session Namer turn
# hook) actually executes instead of vanishing.
_BG_TASKS: set = set()


def _spawn_bg(coro, *, label: str = "bg") -> None:
    task = asyncio.ensure_future(coro)
    _BG_TASKS.add(task)

    def _done(t: "asyncio.Task") -> None:
        _BG_TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.warning("background task %r failed: %s", label, exc, exc_info=exc)

    task.add_done_callback(_done)

# ── Memory skip gate ──
# Skip memory_search for trivial messages (greetings, affirmations, commands).
_SKIP_MEMORY_PATTERN = re.compile(
    r"^(hi|hello|hey|sup|yo|thanks|thank you|ok|okay|got it|cool|sure|"
    r"yes|no|go ahead|keep going|continue|next|and\??|more|elaborate|"
    r"good (morning|afternoon|evening)|what'?s up|how are you|how'?s it going|"
    r"check my email|list my files|show my messages|"
    r"read my email|get my mail|open my inbox)[\s!?.]*$",
    re.IGNORECASE,
)


def _should_skip_memory(message: str) -> bool:
    """Return True if message is trivial and doesn't need brain context."""
    stripped = (message or "").strip()
    return bool(not stripped or _SKIP_MEMORY_PATTERN.match(stripped))


def _should_skip_vector(message: str) -> bool:
    """Return True for short messages that should use keyword-only memory search.

    These still get a memory lookup (unlike _should_skip_memory), but skip the
    remote query-embedding round-trip and rely on the porter-stemmed FTS index.
    Short follow-ups ("who offered?", "what's the floor?", "and the bike?") match
    fine on keywords, so paying for an embedding just adds latency to the turn.
    Longer, more semantic prompts keep the full hybrid (FTS + vector) search.
    """
    stripped = (message or "").strip()
    return len(stripped.split()) <= 4


def _session_title_from_message(message: str, max_words: int = 6) -> str:
    """Extract a concise 3–6 word session title from the user's first message."""
    words = (message or "").strip().split()
    if not words:
        return "New Session"
    title = " ".join(words[:max_words]).rstrip(".,!?;: ")
    return title[:60] or "New Session"


async def _enforce_agent_access_policy(db, agent: dict, user_id: str) -> None:
    """Raise 403 if user is not allowed to chat with this agent under its user_mode policy."""
    mode = (agent or {}).get("user_mode") or "anonymous"
    if mode == "anonymous":
        return
    # Global admin always allowed
    try:
        if await db.is_user_admin(user_id):
            return
    except Exception:
        pass
    roles = await db.get_agent_roles(agent["id"])
    if user_id in (roles.get("admin_users") or []):
        return
    if mode == "register":
        # Look up the channel identity for this user_id. If anonymous tier, refuse.
        conn = db._get_conn()
        try:
            row = conn.execute(
                "SELECT user_tier FROM channel_identities WHERE user_id = ? ORDER BY updated_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        finally:
            conn.close()
        tier = (row["user_tier"] if row else None) or "anonymous"
        if tier == "anonymous":
            raise HTTPException(status_code=403, detail="This agent requires a registered account.")
        return
    if mode == "authorized":
        if user_id not in (roles.get("authorized_users") or []):
            raise HTTPException(status_code=403, detail="This agent requires admin authorization for new users.")


async def _enforce_billing_access(db, agent: dict, user_id: str) -> None:
    """Gate chat on billing state — credits, subscription, or trial.

    Agents with no billing config (or strategy='free', or where the user is
    exempt) pass through. Otherwise we raise HTTP 402 with a structured
    reason so the frontend can show the right paywall."""
    try:
        decision = await billing_check_access(agent, user_id, db)
    except Exception as e:
        logger.debug("billing access check failed (allowing): %s", e)
        return
    if decision.allow:
        return
    raise HTTPException(status_code=402, detail=decision.to_dict())


async def _ensure_session(db, user_id: str, session_id: str, title: str = None) -> None:
    """Create the session row if it doesn't exist yet, and update its title on first real message."""
    conn = db._get_conn()
    try:
        row = conn.execute("SELECT title FROM sessions WHERE id=?", (session_id,)).fetchone()
    finally:
        conn.close()

    if row is None:
        # Session doesn't exist — create it
        try:
            meta = {}
            try:
                from app.devices import identity as _identity
                meta["device"] = {"id": _identity.device_id(), "label": _identity.device_label()}
            except Exception:
                pass
            raw = db.get_raw_client()
            raw.table("sessions").insert({
                "id": session_id,
                "user_id": user_id,
                "title": title or "New Session",
                "pinned": 1,
                "metadata": json.dumps(meta),
            }).execute()
            logger.info(f"Created session {session_id[:12]} for user {user_id[:12]}")
        except Exception as create_err:
            logger.warning(f"Session creation failed (may already exist): {create_err}")
    elif title and row[0] in (None, "New Session", session_id[:12]):
        # Session exists with placeholder title — update to first real message
        conn = db._get_conn()
        try:
            conn.execute("UPDATE sessions SET title=? WHERE id=?", (title, session_id))
            conn.commit()
        finally:
            conn.close()


# Canonical Ask/Plan/Auto, accepting the legacy Read/Write names (mirrors
# loop.py's _MODE_ALIASES) so saved DB values and the TUI bridge stay valid.
_CHAT_MODE_ALIASES = {'read': 'plan', 'write': 'ask', 'plan': 'plan', 'ask': 'ask', 'auto': 'auto'}


async def _record_session_execution_mode(db, session_id: str, raw_mode) -> None:
    """Stamp the mode each message actually runs in onto the session.

    The chat pill (Ask/Plan/Auto) is the user's per-message control, but it
    lives in the browser. Recording the mode server-side makes the SESSION the
    source of truth, so the pill can be restored correctly on any device / after
    a reload (db_viewer.get_session_messages returns it; the chat panel applies
    it on load) — closing the "pill says Ask but the run went Auto" desync. The
    agent's own set_execution_mode writes the same field, so last-run-wins stays
    correct. Best-effort: a failure here must never block the send.
    """
    try:
        mode = _CHAT_MODE_ALIASES.get(str(raw_mode or '').strip().lower(), 'ask')
        await db.set_session_execution_mode(session_id, mode)
    except Exception as _mode_err:
        logger.debug("record session execution mode failed for %s: %s", session_id, _mode_err)


class InterruptRequest(BaseModel):
    session_id: str


class ResumeRequest(BaseModel):
    session_id: str

def _match_slash_command(message: str):
    """Match message against all slash_command triggers from the trigger index.

    Returns (trigger_key, arg, template_id) if matched, else None.
    trigger_key is e.g. '/optimize', arg is the text after the command.
    """
    stripped = (message or "").strip()
    if not stripped.startswith("/"):
        return None
    slash_cmds = trigger_index.get_slash_commands()
    for trigger_key, template_id in slash_cmds.items():
        # Require the command to be followed by whitespace or end-of-string, so a
        # longer word can't partial-match a shorter command (e.g. "/optimizer"
        # must NOT match "/optimize" and silently pass the trailing "r" as an arg).
        pattern = re.compile(
            r"^" + re.escape(trigger_key) + r"(?:\s+(.*))?$",
            re.IGNORECASE | re.DOTALL,
        )
        m = pattern.match(stripped)
        if m:
            return trigger_key, (m.group(1) or "").strip(), template_id
    return None


async def _handle_generic_slash_command(
    template_id: str,
    trigger_key: str,
    arg: str,
    user_id: str,
    session_id: str,
    channel: str,
    db,
) -> str:
    """Generic handler for slash commands that don't have a custom runner.

    Creates a new session bound to the matched agent template and returns
    a user-facing confirmation message.
    """
    new_sid = f"slash-{uuid.uuid4().hex[:12]}"
    try:
        raw = db.get_raw_client()
        raw.table("sessions").insert({
            "id": new_sid,
            "user_id": user_id,
            "title": f"{trigger_key} session",
            "metadata": json.dumps({"trigger_key": trigger_key, "arg": arg}),
        }).execute()
    except Exception as e:
        logger.warning("Could not create session for %s: %s", trigger_key, e)
        return f"Could not start `{trigger_key}` — session creation failed."

    return (
        f"**{trigger_key}** session started.\n"
        f"Session ID: `{new_sid}`\n"
        + (f"Input: {arg}\n" if arg else "")
        + f"\nOpen the session to continue."
    )


async def _handle_optimize_command(
    user_id: str,
    session_id: str,
    feedback: str,
    channel: str,
    db,
) -> str:
    """Run the optimizer against the user's current session.
    feedback is the text after the slash command (may be empty).
    Returns a user-facing message."""

    # Target THIS session — the one the user typed /optimize in — provided it is a
    # real chat (not itself an optimizer/worker/closer session). This is what the
    # user means by "optimize this conversation". Only when the command is run from
    # a non-real session (or an unknown id) do we fall back to the user's most
    # recent real session.
    def _is_real_session(sid: str) -> bool:
        return bool(sid) and not (
            sid.startswith('optimizer-') or sid.startswith('worker-') or sid.startswith('closer-')
        )

    target_session = ""
    try:
        from app.db import get_db as _get_db
        conn = _get_db()._get_conn()
        try:
            if _is_real_session(session_id):
                exists = conn.execute(
                    "SELECT 1 FROM sessions WHERE id=? LIMIT 1", (session_id,)
                ).fetchone()
                if exists:
                    target_session = session_id
            if not target_session:
                row = conn.execute(
                    "SELECT id FROM sessions WHERE user_id=? AND id NOT LIKE 'optimizer-%' "
                    "AND id NOT LIKE 'worker-%' AND id NOT LIKE 'closer-%' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (user_id,)
                ).fetchone()
                target_session = row[0] if row else ""
        finally:
            conn.close()
    except Exception:
        target_session = session_id if _is_real_session(session_id) else ""

    if not target_session:
        return "No chat session found to optimize. Send a few messages first, then try /optimize."

    # Run optimizer inline (fast: no LLM calls, just prefilter + session setup)
    opt_sid = await run_optimizer_async(
        user_id=user_id,
        session_id=target_session,
        channel=channel,
        feedback=feedback,
        force=True,
    )

    msg = f"⚡ **Optimization session created!**\n"
    msg += f"• Target: `{target_session[:8]}`\n"
    msg += f"• Optimizer: `{opt_sid}`\n" if opt_sid else ""
    msg += f"• Feedback: {feedback}\n" if feedback else ""
    msg += f"\nOpen the optimizer session in the UI to review the analysis and discuss changes with the Planner."
    return msg


def _is_compact_command(message: str) -> bool:
    """True if the message is the built-in ``/compact`` command (args ignored).

    ``/compact`` is a *system* command, not an agent — it is handled directly
    here rather than through the template-driven trigger index, so it works in any
    chat session without registering an agent. Matches ``/compact`` alone or
    followed by whitespace+text, but not ``/compactfoo``."""
    s = (message or "").strip()
    return bool(re.match(r"^/compact(?:\s+.*)?$", s, re.IGNORECASE | re.DOTALL))


async def _handle_compact_command(user_id: str, session_id: str, db) -> str:
    """Force a compaction of the CURRENT session now (the ``/compact`` command).

    Deterministic, user-driven sibling of the agent's ``compact_context`` tool: it
    folds everything older than the verbatim 'hot tail' (Keep Verbatim) into frozen
    summary parts immediately — regardless of how full the context is — honouring
    the agent's Context Control settings (part size etc.). Nothing is deleted; the
    raw turns stay searchable. Failure-safe: returns a user-facing message either
    way, never raising into the chat."""
    try:
        agent_id = await db.get_session_agent_id(session_id)
    except Exception:
        agent_id = None
    if not agent_id:
        # No agent is bound to this session yet (e.g. nothing has run in it). This is
        # NOT the same as "nothing old enough to fold" — say so honestly so the user
        # isn't told to send messages they may already have sent.
        return (
            "Couldn't compact — this session isn't linked to an agent yet, so there's "
            "nothing to summarise. Send a message to the agent first, then run "
            "`/compact`."
        )

    # An alternate engine (e.g. Local Claude Code) keeps its memory OUTSIDE webAgent,
    # so /compact means "compact & restart" for it: after the fold below, hand off to
    # the engine's hook to reshape what happens next. Generic lookup — no per-engine
    # branching here (the core stays small; engines are drop-in plug-ins).
    compact_hook = None
    try:
        agent_rec = await db.get_agent_by_id(agent_id)
        _eng_meta = (agent_rec or {}).get("metadata") or {}
        if isinstance(_eng_meta, str):
            _eng_meta = json.loads(_eng_meta or "{}")
        _engine_id = str((_eng_meta or {}).get("engine") or "").strip()
        if _engine_id and _engine_id != "default":
            from plugins.engines import get_engine_compact_hook
            compact_hook = get_engine_compact_hook(_engine_id)
    except Exception:
        compact_hook = None

    try:
        from app.agent.context_control import get_context_settings
        from app.agent.compaction import maybe_compact
        settings = await get_context_settings(db, agent_id, session_id, user_id)
        if not settings.get("enabled"):
            return (
                "Context Control isn't active for this agent, so there's nothing to "
                "compact. Enable the Context Control ability to use `/compact`."
            )
        info = await maybe_compact(db, user_id, session_id, settings, force=True)
    except Exception as e:
        logger.warning("/compact failed for %s: %s", session_id, e)
        return f"Couldn't compact this session — {e}"

    # Alternate engine: hand the fold result to its /compact behaviour. The Claude
    # Code engine uses this to compact-and-restart even when nothing was folded (a
    # short chat still seeds a fresh, clean session), so this runs before the
    # native "nothing to compact" short-circuit below.
    if compact_hook is not None:
        try:
            return await compact_hook(db, user_id, session_id, agent_id, info)
        except Exception as e:
            logger.warning("/compact engine hook failed for %s: %s", session_id, e)
            return f"Couldn't restart this session — {e}"

    if not info:
        return (
            "Nothing to compact right now — this conversation already fits inside "
            "the verbatim recent tail (Keep Verbatim), so there are no older turns "
            "to fold. No summary was created."
        )
    folded = info.get("summarised_rows") or 0
    new_cars = info.get("new_cars") or 0
    parts = info.get("segments") or 0
    return (
        f"✅ **Compacted this conversation.** Folded {folded} older message(s) into "
        f"{new_cars} new summary part(s) ({parts} part(s) total). The most recent "
        "turns are kept word-for-word; everything older is now summarized and stays "
        "searchable. This takes effect on the next turn."
    )


@router.post("/interrupt")
async def interrupt_chat(request: InterruptRequest):
    """Request a graceful interruption for an ongoing chat session.

    Interrupt is the ONLY thing (besides finishing or a server restart) that
    stops a supervised run. Sets the DB flag the agent loop polls; the loop
    finalizes its partial answer as 'interrupted' and flips run-state."""
    try:
        db = get_db()
        was_running = await get_run_manager().interrupt(request.session_id, db)
        return {"status": "ok", "message": "Interrupt requested.", "was_running": was_running}
    except Exception as e:
        logger.error(f"Error setting interrupt: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/resume")
async def resume_chat(request: ResumeRequest):
    """Manually re-ignite a stopped run (the one-click path for a run held as
    'needs_manual_resume' by the auto-resume opt-out, or any interrupted/failed
    turn the user wants to continue). Backend-driven — works even with no live
    WebSocket. The resumed turn streams into the chat via the normal event path."""
    try:
        from app.agent.runner import manual_resume
        ok = await manual_resume(request.session_id)
        return {"status": "ok" if ok else "noop",
                "resumed": ok,
                "message": "Resuming." if ok else "Nothing to resume (already running or not resumable)."}
    except Exception as e:
        logger.error("Error resuming run %s: %s", request.session_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/terminal-chat/activate")
async def terminal_chat_activate(fastapi_request: Request):
    """Activate a terminal for the Terminal Chat agent — spawns the PTY and
    emits a terminal_chat_state event so the frontend mounts xterm.js
    immediately, without waiting for a user message."""
    from app.auth.identity import assert_caller_is

    user_id = await assert_caller_is(fastapi_request, None)
    body = await fastapi_request.json()
    session_id = body.get("session_id", "")
    agent_id = body.get("agent_id", "")
    
    if not session_id or not agent_id:
        return {"status": "error", "error": "session_id and agent_id required"}
    
    from app.db import get_db
    db = get_db()
    
    # Get agent record to read terminal_chat config
    agent_rec = await db.get_agent_by_id(agent_id)
    if not agent_rec:
        return {"status": "error", "error": "Agent not found"}
    
    # Read engine from metadata
    meta = {}
    try:
        meta_raw = agent_rec.get("metadata", "{}")
        if isinstance(meta_raw, str):
            meta = json.loads(meta_raw)
        elif isinstance(meta_raw, dict):
            meta = meta_raw
    except Exception:
        pass
    
    engine = meta.get("engine", "")
    if engine != "terminal_chat":
        return {"status": "error", "error": "Agent is not a Terminal Chat agent"}
    
    tc_cfg = meta.get("terminal_chat", {}) if isinstance(meta.get("terminal_chat"), dict) else {}
    command = str(tc_cfg.get("command") or "").strip()
    
    # Spawn the PTY and emit the state event
    from plugins.engines.terminal_chat.terminal_chat import _get_or_create_pty
    tsid = await _get_or_create_pty(session_id, user_id, command=command)
    
    # Emit terminal_chat_state on the WS so the frontend mounts xterm.js
    await _emit_to_visualizers(session_id, {
        "type": "terminal_chat_state",
        "state": "active",
        "terminal_session_id": tsid,
        "command": command,
    }, user_id=user_id)
    
    return {"status": "ok", "terminal_session_id": tsid}


@router.get("/self-heal/status")
async def self_heal_status():
    """Observability: the liveness watchdog's status + counters, plus the list of
    runs currently awaiting a manual one-click resume."""
    try:
        from app.agent.watchdog import get_watchdog
        wd = await get_watchdog().get_status()
    except Exception as e:
        wd = {"error": str(e)}
    manual: List[Dict[str, Any]] = []
    try:
        db = get_db()
        conn = db._get_conn()
        try:
            rows = conn.execute(
                "SELECT session_id, user_id, origin, resume_attempts, error, updated_at "
                "FROM session_runs WHERE stop_cause='needs_manual_resume' "
                "ORDER BY updated_at DESC LIMIT 100"
            ).fetchall()
            manual = [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        manual = []
    return {"watchdog": wd, "awaiting_manual_resume": manual}


# ── Suggested replies (silent user-impersonator) ────────────────────────────
# These power the tappable suggestion chips above the chat pill. They run a
# single, hidden LLM call — never the agent loop, never persisted to the chat,
# never streamed over the agent WebSocket. See app/agent/suggestions.py.

class SuggestionsRequest(BaseModel):
    user_id: str
    session_id: Optional[str] = None
    count: Optional[int] = None


class SuggestionsConfigRequest(BaseModel):
    mode: Optional[str] = None          # "off" | "on" | "scheduler"
    count: Optional[int] = None
    idle_seconds: Optional[int] = None


@router.post("/suggestions")
async def chat_suggestions(req: SuggestionsRequest):
    """Return up to N suggested next user-messages for the chat pill chips.

    Best-effort: returns an empty list (HTTP 200) when the engine is off,
    credentials are missing, or generation fails — the UI just shows no chips."""
    from app.agent.suggestions import generate_suggestions, load_runtime_config
    cfg = load_runtime_config()
    if cfg.get("mode") == "off":
        return {"suggestions": [], "mode": "off"}
    try:
        db = get_db()
        items = await generate_suggestions(
            db, req.user_id, req.session_id, count=req.count
        )
    except Exception as e:
        logger.warning("chat_suggestions failed: %s", e)
        items = []
    return {"suggestions": items, "mode": cfg.get("mode"), "idle_seconds": cfg.get("idle_seconds")}


@router.get("/suggestions/config")
async def get_suggestions_config():
    """Read the Suggested-Replies runtime config (mode / count / idle seconds)."""
    from app.agent.suggestions import load_runtime_config
    return load_runtime_config()


# ── On-demand skills (active list + activate/deactivate) ───────────────────
# Power the active-skill chips below the chat box AND the skill-selector panel
# anchored to the right edge of the chat pill footer. The active list lives in
# the session's metadata; load_skill adds to it, these endpoints modify it.

class SkillActivateRequest(BaseModel):
    user_id: str
    session_id: str
    name: str


class SkillDeactivateRequest(BaseModel):
    user_id: str
    session_id: str
    name: str


@router.get("/skills")
async def chat_skills(user_id: str, session_id: str, agent_id: Optional[str] = None):
    """Return the active (loaded) selectable skills for a session, plus the
    agent's full selectable skill catalog (authored + ability-bundled) when
    `agent_id` is supplied. Each catalog entry carries a display_name for the
    UI panel and an `active` flag so the panel shows engaged skills.

    The active list alone needs no agent (it lives in session metadata), so the
    chat chips work even if agent_id isn't passed; the panel passes agent_id to
    also get descriptions + modes + display_name."""
    db = get_db()
    active = await db.get_session_active_skills(session_id)
    active_set = set(active)

    all_skills = []

    # ── 1. Authored skills from the agent's __skills__ slot ──
    if agent_id:
        try:
            all_skills.extend(await db.get_agent_skills(agent_id) or [])
        except Exception:
            pass

    # ── 2. Ability-bundled skills — directly scan every ability file ──
    # Do NOT go through collect_ability_skills (which requires agent connections
    # and admin config). Instead, scan all ability modules for skill declarations
    # so the panel shows every discoverable skill regardless of how the agent is
    # configured. The agent's abilities are already gated by the admin; showing
    # all ability skills here is OK because the user activates them manually.
    try:
        from app.abilities import all_raw, _resolve_skill_body
        for ability_id, feat in all_raw().items():
            try:
                body = _resolve_skill_body(feat, ability_id)
                if not body:
                    continue
                from app.agent.ability_skills import _skill_from_feature
                skill = _skill_from_feature({**feat, "skill": body}, ability_id)
                if skill:
                    all_skills.append(skill)
            except Exception:
                continue
    except Exception:
        pass

    catalog = [
        {
            "name": s.get("handle") or s["name"],
            "display_name": s.get("display_name") or s.get("source") or s["name"],
            "description": s.get("description", ""),
            "mode": s.get("mode", "selectable"),
            "active": True if s.get("mode") == "always_on" else (s.get("handle") or s["name"]) in active_set,
        }
        for s in all_skills
        if s.get("enabled", True)
    ]
    return {"active": active, "skills": catalog}


@router.get("/abilities")
async def chat_abilities(user_id: str, session_id: str, agent_id: Optional[str] = None):
    """Return the agent's enabled abilities + which are active for this session.

    Drives the chat-side abilities panel + its counter. An ability is **active**
    (highlighted; counted) when it is `visible` (its tools + skill are shown to
    the agent now) OR the agent has pulled it in this session via `load_ability`.
    """
    db = get_db()
    loaded = set(await db.get_session_active_abilities(session_id))
    suppressed = set(await db.get_session_suppressed_abilities(session_id))
    out = []
    if agent_id:
        try:
            from app.tools.tool_modes import resolve_ability_mode
            from app.abilities import ui_catalog
            modes = await db.get_agent_ability_modes(agent_id)
            cat_abilities = (ui_catalog() or {}).get("abilities", {})
            rows = await db.get_agent_connections(agent_id)
            for r in rows:
                if r.get("section") != "ability" or not r.get("enabled"):
                    continue
                aid = r.get("connection_type")
                if not aid:
                    continue
                meta = cat_abilities.get(aid, {})
                mode = resolve_ability_mode(aid, modes)
                # Active = revealed to the agent this turn: visible-by-config OR
                # loaded this session, AND not suppressed from this panel.
                active = ((mode == "visible") or (aid in loaded)) and (aid not in suppressed)
                out.append({
                    "id": aid,
                    "display_name": meta.get("display_name") or aid,
                    "icon": meta.get("icon") or "plug",
                    "mode": mode,
                    "loaded": aid in loaded,
                    "suppressed": aid in suppressed,
                    "active": active,
                })
        except Exception as e:
            logger.debug("chat_abilities failed for %s: %s", agent_id, e)
    out.sort(key=lambda a: (a["display_name"] or a["id"]).lower())
    active_ids = [a["id"] for a in out if a["active"]]
    return {"active": active_ids, "abilities": out}


class AbilityToggleRequest(BaseModel):
    user_id: str
    session_id: str
    ability_id: str
    agent_id: Optional[str] = None


@router.post("/abilities/activate")
async def chat_ability_activate(req: AbilityToggleRequest):
    """Arm an ability for this conversation from the chat panel. Clears any
    suppression and — for a ``discoverable`` ability — adds it to the session's
    active list (same effect as the agent calling ``load_ability``), so its tools
    + skill flow into the next turn. A ``visible`` ability just needs un-suppressing."""
    db = get_db()
    await _ensure_session(db, req.user_id, req.session_id)
    await db.set_session_suppressed_ability(req.session_id, req.ability_id, False)
    mode = "visible"
    if req.agent_id:
        try:
            from app.tools.tool_modes import resolve_ability_mode
            modes = await db.get_agent_ability_modes(req.agent_id)
            mode = resolve_ability_mode(req.ability_id, modes)
        except Exception as e:
            logger.debug("ability_activate mode-resolve failed for %s: %s", req.ability_id, e)
    if mode != "visible":
        await db.set_session_active_ability(req.session_id, req.ability_id, True)
    return {"ability_id": req.ability_id, "active": True}


@router.post("/abilities/deactivate")
async def chat_ability_deactivate(req: AbilityToggleRequest):
    """Turn an ability OFF for this conversation from the chat panel. Drops it
    from the session's active list and — for a ``visible`` ability — records a
    suppression so it's withheld even though the agent's config makes it visible.
    Its tools + skill leave the model's context on the next turn."""
    db = get_db()
    await _ensure_session(db, req.user_id, req.session_id)
    await db.set_session_active_ability(req.session_id, req.ability_id, False)
    # Only a `visible`-by-config ability needs an explicit suppression to stay
    # off; for a discoverable one, dropping it from the active list is enough —
    # and suppressing it would wrongly block a later `load_ability` by the agent.
    mode = "visible"
    if req.agent_id:
        try:
            from app.tools.tool_modes import resolve_ability_mode
            modes = await db.get_agent_ability_modes(req.agent_id)
            mode = resolve_ability_mode(req.ability_id, modes)
        except Exception as e:
            logger.debug("ability_deactivate mode-resolve failed for %s: %s", req.ability_id, e)
    await db.set_session_suppressed_ability(req.session_id, req.ability_id, mode == "visible")
    return {"ability_id": req.ability_id, "active": False}


@router.post("/skills/activate")
async def chat_skill_activate(req: SkillActivateRequest):
    """Manually activate a selectable skill from the UI panel. Calls load_skill
    on behalf of the user so it counts as loaded the same way as if the agent
    called load_skill itself."""
    db = get_db()
    await _ensure_session(db, req.user_id, req.session_id)
    active = await db.set_session_active_skill(req.session_id, req.name, True)
    return {"active": active, "name": req.name}


@router.post("/skills/deactivate")
async def chat_skill_deactivate(req: SkillDeactivateRequest):
    """Drop a loaded skill from the conversation: remove it from the session's
    active list and neutralize its stored load result so the body leaves the
    model's context on the next turn."""
    db = get_db()
    await _ensure_session(db, req.user_id, req.session_id)
    active = await db.set_session_active_skill(req.session_id, req.name, False)
    try:
        await db.neutralize_skill_load(req.session_id, req.name)
    except Exception as e:
        logger.debug("neutralize_skill_load failed for %s: %s", req.name, e)
    return {"active": active, "name": req.name}


# ── Per-session model override ────────────────────────────────────────────────
# The chat footer model picker saves the chosen model HERE (per session) rather
# than on the agent, so each conversation remembers its own model. The agent loop
# layers this over the agent/app default on every turn (app-default → agent →
# session). An empty model clears the override.

class SessionModelRequest(BaseModel):
    user_id: str
    session_id: str
    model: Optional[str] = None


@router.post("/session-model")
async def set_session_model(req: SessionModelRequest):
    """Set (or clear, with an empty model) this session's model override. Takes
    effect on the next turn — the loop re-resolves the effective model per run."""
    db = get_db()
    # A brand-new chat has no session row until its first message; create it so a
    # model picked before sending anything still persists.
    await _ensure_session(db, req.user_id, req.session_id)
    cfg = await db.set_session_llm_override(req.session_id, (req.model or "").strip() or None)
    return {"llm_config": cfg, "model": (cfg or {}).get("model", "")}


class SessionEffortRequest(BaseModel):
    user_id: str
    session_id: str
    model: str
    reasoning_effort: Optional[str] = None


@router.post("/session-model-effort")
async def set_session_model_effort(req: SessionEffortRequest):
    """Set (or clear, with 'default'/empty) the reasoning-effort level for a
    specific model on THIS session. Each model remembers its own level (the footer
    picker shows an effort selector per model row, and the Model Switcher ability
    writes here too). Doesn't change which model is active — takes effect on the
    next turn for whichever model is running."""
    db = get_db()
    await _ensure_session(db, req.user_id, req.session_id)
    cfg = await db.set_session_model_effort(
        req.session_id, (req.model or "").strip(), (req.reasoning_effort or "").strip() or None)
    effort_map = (cfg or {}).get("model_effort") or {}
    return {"llm_config": cfg, "model": (req.model or "").strip(),
            "reasoning_effort": effort_map.get((req.model or "").strip(), "default")}


@router.put("/suggestions/config")
async def update_suggestions_config(req: SuggestionsConfigRequest):
    """Update the Suggested-Replies runtime config. Used by the impersonator
    agent's config panel on the Agents page."""
    from app.agent.suggestions import save_runtime_config
    updates = {k: v for k, v in req.dict().items() if v is not None}
    return save_runtime_config(updates)



@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest, fastapi_request: Request):
    """
    Process a chat message with the agent.

    Uses the simple agent loop with tool-calling support.
    """
    try:
        # Tenant isolation: the JWT subject must match the user_id the
        # client says it's chatting as. Every tool wrapper down the call
        # graph closes over this user_id, so getting it wrong here lets one
        # authenticated user impersonate another for the whole session.
        from app.auth.identity import assert_caller_is
        request.user_id = await assert_caller_is(fastapi_request, request.user_id)
        db = get_db()

        # ── Temp DB resolution for optimizer/closer sessions ──
        _temp_db_path = None
        if request.session_id.startswith('optimizer-') or request.session_id.startswith('closer-'):
            _meta_conn = db._get_conn()
            try:
                _meta_row = _meta_conn.execute(
                    "SELECT metadata FROM sessions WHERE id=?", (request.session_id,)
                ).fetchone()
                if _meta_row and _meta_row[0]:
                    _meta = json.loads(_meta_row[0])
                    _temp_db_path = _meta.get('temp_db_path')
            finally:
                _meta_conn.close()
            if _temp_db_path:
                from app.db.local import LocalBackend as _OptBackend
                db = _OptBackend(db_path=_temp_db_path)
                logger.info("Using temp DB for %s session: %s", request.session_id[:12], _temp_db_path)

        # ── Terminal tunnel: the user is driving a program directly ──
        # If this chat session is bound to a live terminal, the message is a
        # keystroke line for that program, not a prompt for the agent. Route it
        # to the PTY (persisted but context-excluded) and return — the slash
        # parser and the whole agent loop are bypassed. The user ends the tunnel
        # with the UI "Hand back" button, never by typing.
        try:
            from app.agent.terminal_tunnel import route_user_line as _route_tunnel
            if await _route_tunnel(db, request.user_id, request.session_id, request.message or ""):
                return ChatResponse(reply="", response="", session_id=request.session_id)
        except Exception as _tun_err:
            logger.warning("tunnel routing failed for %s: %s", request.session_id, _tun_err)

        # ── Handle slash commands ──
        # Built-in system commands first (not template-driven). /compact folds the
        # session's older turns now; see _handle_compact_command.
        if _is_compact_command(request.message or ""):
            result = await _handle_compact_command(request.user_id, request.session_id, db)
            return ChatResponse(reply=result, response=result, session_id=request.session_id)
        _slash_match = _match_slash_command(request.message or "")
        if _slash_match:
            _slash_key, _slash_arg, _slash_tid = _slash_match
            if _slash_tid == "opt_planner":
                result = await _handle_optimize_command(
                    request.user_id, request.session_id,
                    _slash_arg, "web_portal", db,
                )
            else:
                result = await _handle_generic_slash_command(
                    _slash_tid, _slash_key, _slash_arg,
                    request.user_id, request.session_id, "web_portal", db,
                )
            return ChatResponse(reply=result, response=result, session_id=request.session_id)

        # Ensure the session exists before inserting interactions
        _session_title = _session_title_from_message(request.message) if (request.message or "").strip() else None
        await _ensure_session(db, request.user_id, request.session_id, title=_session_title)

        # ── Optimizer / Closer session: route to dedicated agent ──
        agent = None
        opt_role = None
        opt_template_id = None
        opt_metadata = {}
        if request.session_id.startswith('optimizer-') or request.session_id.startswith('closer-'):
            conn = db._get_conn()
            try:
                meta_row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id=?", (request.session_id,)
                ).fetchone()
                opt_metadata = json.loads(meta_row[0]) if meta_row and meta_row[0] else {}
                if request.session_id.startswith('closer-'):
                    opt_role = 'closer'
                    opt_template_id = trigger_index.get('tool_call', 'handoff_to_closer') or 'opt_closer'
                else:
                    opt_role = opt_metadata.get('opt_role', 'planner')
                    opt_template_id = (
                        trigger_index.get('tool_call', 'run_optimizer') or 'opt_planner'
                        if opt_role == 'planner'
                        else trigger_index.get('tool_call', 'handoff_to_closer') or 'opt_closer'
                    )
            finally:
                conn.close()

            # Resolve agent in main local.db so it is accessible for UI edits,
            # then bind the session in temp DB if one is active.
            _agent_db = get_db() if _temp_db_path else db
            agent = await _agent_db.get_or_resolve_session_agent(
                session_id=request.session_id,
                user_id=request.user_id,
                template_id=opt_template_id,
            )
            if _temp_db_path and agent and agent.get("id"):
                try:
                    await db.bind_session_to_agent(request.session_id, agent["id"])
                except Exception:
                    pass
            if not agent:
                raise RuntimeError(
                    f"Failed to resolve optimizer agent (role={opt_role}) for user {request.user_id}. "
                    f"Check that agent template '{opt_template_id}' exists."
                )

        # ── Assign agent first (context rows are keyed by agent_id) ──
        if agent is None and getattr(request, 'agent_id', None):
            agent = await db.get_agent_by_id(request.agent_id)
        if agent is None:
            agent = await db.get_agent_for_user(request.user_id)
        if agent is None:
            raise HTTPException(
                status_code=400,
                detail="No agent assigned. Create an agent before chatting.",
            )

        # ── Bind session to agent ──
        # The streaming send path does this in _prepare_send; the buffered path must
        # too, or sessions.agent_id stays NULL and anything that resolves the session's
        # agent later (e.g. /compact via get_session_agent_id) silently misfires.
        _existing_agent_id = await db.get_session_agent_id(request.session_id)
        if _existing_agent_id is None:
            await db.bind_session_to_agent(request.session_id, agent["id"])
        elif _existing_agent_id != agent["id"]:
            raise RuntimeError(
                f"Session {request.session_id[:8]} bound to agent {_existing_agent_id[:8]}, "
                f"but resolved agent is {agent['id'][:8]}. Cannot respond."
            )

        # ── Agent access policy enforcement ──
        await _enforce_agent_access_policy(db, agent, request.user_id)
        await _enforce_billing_access(db, agent, request.user_id)

        # ── Participants enforcement ──
        # Ensure the user and agent are registered as participants
        if not await db.is_session_participant(request.session_id, request.user_id, 'user'):
            await db.add_session_participant(request.session_id, request.user_id, 'user')
        if not await db.is_session_participant(request.session_id, agent["id"], 'agent'):
            await db.add_session_participant(request.session_id, agent["id"], 'agent')

        # Save user message and get its ID for parent linking
        # Optimizer/Closer sessions get source='optimizer' to distinguish from normal chats
        is_opt = request.session_id.startswith('optimizer-') or request.session_id.startswith('closer-')
        user_interaction_id = await db.insert_interaction(
            request.user_id, request.session_id, role="user", content=request.message,
            channel="web_portal",
            metadata=json.dumps({"source": "optimizer" if is_opt else "web_portal_chat"}),
            input_data=json.dumps(request.model_dump(), default=str),
            sender_id=request.user_id,
            receiver_id=agent["id"],
            source="optimizer" if is_opt else None,
        )

        # Record the mode this turn runs in so the pill is restorable server-side.
        await _record_session_execution_mode(db, request.session_id, getattr(request, 'execution_mode', 'ask'))

        # ── Start a run buffer for this turn ──
        _run_buffer = await get_run_buffer_registry().start_turn(
            session_id=request.session_id,
            user_id=request.user_id,
            turn_id=user_interaction_id,
            db=db,
        )
        try:
            _user_ss, _user_ts = _run_buffer.next_seq()
            _conn = db._get_conn()
            try:
                _conn.execute(
                    "UPDATE interactions SET session_seq=?, turn_id=?, turn_seq=? WHERE id=?",
                    (_user_ss, user_interaction_id, _user_ts, user_interaction_id),
                )
                _conn.commit()
            finally:
                _conn.close()
        except Exception as _seqerr:
            logger.debug("Failed to backfill seq on user row (buffered): %s", _seqerr)

        # ── Emit user message to visualizer listeners ──
        await _emit_to_visualizers(request.session_id, {
            "type": "user_message", "level": "user",
            "content": request.message, "id": user_interaction_id,
        })

        # ── Build loop config for pre-loop gating ──
        loop_config = LoopConfig.from_agent(agent)

        # ── Agent context docs (already included by get_or_resolve_session_agent / get_agent_for_user) ──
        context_docs = agent.get("context_documents", [])

        # Non-optimizer agents: copy defaults if no context docs exist
        if not context_docs and not is_opt and loop_config.is_enabled("copy_defaults"):
            copied = await db.copy_defaults_to_agent(agent["id"], template_id='default')
            if copied > 0:
                agent = await db.fetch_agent_by_id_with_context(agent["id"], CONTEXT_SECTION_TYPES, user_id=request.user_id)
                context_docs = agent.get("context_documents", [])

        # ── Pipeline: context loaded ──
        doc_types = list(set(
            (d.get("context_type") or d.get("doc_type") or "")
            for d in context_docs if d.get("context_type") or d.get("doc_type")
        ))
        await _emit_to_visualizers(request.session_id, {
            "type": "pipeline", "level": "pipeline",
            "step": "load_context", "count": len(context_docs),
            "types": doc_types,
        })

        # ── PHASE 1: Brain-first lookup (visible as tool interaction) ──
        # The lookup is LAUNCHED here but (when it runs) awaited later, just
        # before the system prompt is assembled — so its embedding round-trip
        # overlaps with attachment/image prep instead of stalling the turn (#2).
        brain_results = []
        brain_context = None
        parent_id = None
        _brain_task = None
        if not loop_config.is_enabled("memory_search") or _should_skip_memory(request.message):
            _skip_reason = "node_disabled" if not loop_config.is_enabled("memory_search") else "greeting_or_cmd"
            await _emit_to_visualizers(request.session_id, {
                "type": "pipeline", "level": "pipeline",
                "step": "memory_search_skip", "reason": _skip_reason,
            })
            parent_id = await db.insert_interaction(
                request.user_id, request.session_id, role="tool",
                content=json.dumps({"skipped": True, "reason": _skip_reason}),
                parent_id=user_interaction_id,
                tool_name="memory_search",
                channel="web_portal",
                metadata=json.dumps({"brain": True, "skipped": True, "reason": _skip_reason}),
                input_data=json.dumps({"query": request.message, "skipped": True}),
                sender_id=agent["id"],
                receiver_id=agent["id"],
            )
            await _emit_to_visualizers(request.session_id, {
                "type": "pipeline", "level": "pipeline",
                "step": "memory_search_end", "results_count": 0, "results": [],
                "skipped": True,
            })
        else:
            await _emit_to_visualizers(request.session_id, {
                "type": "pipeline", "level": "pipeline",
                "step": "memory_search_start", "query": request.message, "limit": 5,
            })
            # Short messages take the keyword-only fast path (no embedding) (#3).
            _use_vector = not _should_skip_vector(request.message)
            _brain_task = asyncio.create_task(
                db.memory_search(request.user_id, request.message, limit=5, vector=_use_vector)
            )

        # ── Resolve attachment references ──
        attachment_context = None
        attachment_docs: List[Dict[str, Any]] = []
        if request.attachment_ids:
            for att_id in request.attachment_ids:
                att = await db.get_attachment(att_id)
                if att:
                    attachment_docs.append(att)
            if attachment_docs:
                attachment_context = format_attachments_for_prompt(attachment_docs)
                await _emit_to_visualizers(request.session_id, {
                    "type": "attachment", "level": "agent",
                    "attachments": [
                        {"id": a["id"], "original_name": a["original_name"],
                         "mime_type": a["mime_type"], "size_bytes": a["size_bytes"],
                         "storage_path": a.get("storage_path", "")}
                        for a in attachment_docs
                    ],
                })
        _desc_out = {}
        async for _dev in _maybe_describe_images(
            db, request.user_id, request.message, user_interaction_id,
            loop_config, attachment_docs, _desc_out,
            agent_id=agent["id"], session_id=request.session_id,
        ):
            await _emit_to_visualizers(request.session_id, _dev)
        # App Control point-and-share fingerprint → foldable chip + this-turn fold.
        async for _dev in _maybe_emit_app_control(
            db, request, user_interaction_id, _desc_out,
            agent_id=agent["id"], session_id=request.session_id, channel="web_portal",
        ):
            await _emit_to_visualizers(request.session_id, _dev)
        user_message_content = await build_user_message_content(
            _desc_out.get("message_text", request.message),
            _desc_out.get("inline_docs", attachment_docs),
        )

        # ── PHASE 1 (cont.): await the memory lookup launched above ──
        # By awaiting here — after the attachment/image work — the embedding
        # round-trip has been overlapping with that prep, so it rarely adds
        # visible latency. Results are folded into the prompt and recorded as
        # the memory_search tool interaction (parent for the run that follows).
        if _brain_task is not None:
            brain_results = await _brain_task

            await _emit_to_visualizers(request.session_id, {
                "type": "pipeline", "level": "pipeline",
                "step": "memory_search_end", "results_count": len(brain_results),
                "results": [{"slug": r["slug"], "title": r.get("title", r["slug"]),
                             "score": round(r.get("rank", 0), 2)}
                            for r in (brain_results or [])],
            })

            # Format brain context for system prompt injection
            if brain_results:
                lines = []
                for r in brain_results:
                    slug = r.get("slug", "?")
                    title = r.get("title", slug)
                    ct = r.get("compiled_truth", "")[:300]
                    rank = r.get("rank", 0)
                    lines.append(f"## {slug} — {title} (score: {rank:.2f})")
                    if ct:
                        lines.append(ct)
                    lines.append("")
                brain_context = "\n".join(lines)

            # Save memory_search as tool interaction
            search_content = json.dumps({
                "query": request.message,
                "results": [
                    {"slug": r["slug"], "title": r.get("title",""),
                     "score": round(r.get("rank", 0), 2),
                     "snippet": r.get("compiled_truth", "")[:150]}
                    for r in (brain_results or [])
                ],
                "count": len(brain_results or []),
            }, indent=2)
            parent_id = await db.insert_interaction(
                request.user_id, request.session_id, role="tool",
                content=search_content,
                parent_id=user_interaction_id,
                tool_name="memory_search",
                channel="web_portal",
                metadata=json.dumps({
                    "count": len(brain_results or []),
                    "brain": True,
                    "has_results": bool(brain_results),
                }),
                input_data=json.dumps({"query": request.message}),
                output_data=search_content,
                sender_id=agent["id"],
                receiver_id=agent["id"],
            )

            # Emit memory_search as a tool result
            await _emit_to_visualizers(request.session_id, {
                "type": "tool_result", "level": "agent",
                "tool": "memory_search",
                "result": search_content[:2000],
                "duration_ms": 0,
                "error": False,
            })

        # Build system prompt with brain context + dynamic tools
        # context_docs is already the resolved per-caller slot list.
        _agent_id_for_prompt = agent.get("id") if agent else None
        system_prompt = await build_system_prompt(
            context_docs, brain_context, request.user_id,
            agent_id=_agent_id_for_prompt,
        )
        if attachment_context:
            system_prompt = system_prompt + "\n\n" + attachment_context
        system_prompt = await append_skills_section(system_prompt, agent, request.session_id, caller_user_id=request.user_id)

        # ── Pipeline: prompt built ──
        from app.tools.loader import load_tools
        tools = await load_tools(request.user_id,
                                 agent_id=_agent_id_for_prompt or "",
                                 agent_template_id=agent.get("template_id") if agent else None,
                                 gate_caller_access=True)
        tool_count_for_prompt = len(tools)
        section_names = ["SYSTEM"]  # Simplified section count — actual sections are dynamic

        await _emit_to_visualizers(request.session_id, {
            "type": "pipeline", "level": "pipeline",
            "step": "build_prompt", "sections": section_names,
            "brain_injected": bool(brain_context),
            "tool_count_in_prompt": tool_count_for_prompt,
        })

        # Emit data_src_load telemetry so the loop node lights up.
        try:
            if _agent_id_for_prompt:
                _ds_attached = await db.agent_data_source_list(_agent_id_for_prompt, enabled_only=True)
            else:
                _ds_attached = []
        except Exception:
            _ds_attached = []
        await _emit_to_visualizers(request.session_id, {
            "type": "pipeline", "level": "pipeline",
            "step": "data_src_loaded",
            "attached_count": len(_ds_attached),
            "sources": [
                {"name": a.get("name"), "type": a.get("type"), "tool_alias": a.get("tool_alias")}
                for a in _ds_attached
            ],
        })

        # DB-backed conversation history (same session survives browser refresh)
        exclude_ids = {user_interaction_id} if user_interaction_id else set()
        # Build conversation history from DB. For closer sessions, all context
        # (judging criteria, baseline, trial transcripts) is pre-injected as real
        # interaction rows in the temp DB by handoff_to_closer, so the standard
        # history builder works for both planner and closer sessions.
        history = await build_openai_history_from_session(
            db, request.user_id, request.session_id,
            exclude_interaction_ids=exclude_ids,
            agent_id=agent.get("id"),
        )

        # Create event callback that pushes to visualizer and user listeners
        async def event_callback(event: Dict[str, Any]):
            await _emit_to_visualizers(request.session_id, event, user_id=request.user_id)

        # Run the agent loop (with 5-minute timeout)
        # Resolve allowed_tools from agent config (may be list or JSON string)
        _raw_allowed = agent.get("allowed_tools", [])
        if isinstance(_raw_allowed, str):
            import json as _json
            try:
                _raw_allowed = _json.loads(_raw_allowed)
            except Exception:
                _raw_allowed = []
        assistant_reply = await run_agent_loop_buffered(
            user_id=request.user_id,
            session_id=request.session_id,
            user_message=user_message_content,
            system_prompt=system_prompt,
            agent_id=agent["id"],
            history=history,
            parent_interaction_id=parent_id,
            event_callback=event_callback,
            max_turns=agent.get("max_turn_count", 0),
            channel="web_portal",
            timeout_seconds=300,
            db=db,
            agent_template_id=agent.get("template_id"),
            allowed_tools=_raw_allowed or None,
            execution_mode=getattr(request, 'execution_mode', 'ask') or 'ask',
        )

        # ── PHASE 3: Background memory save (visible tool interaction) ──
        # Skip if agent has disabled memory_save via allowed_tools or loop_logic
        if 'memory_save' not in set(_raw_allowed or []) and loop_config.is_enabled("memory_save"):
            asyncio.create_task(_save_chat_to_memory(
                db, request.user_id, request.session_id,
                request.message, assistant_reply, agent["id"], parent_id,
            ))
            # ── Pipeline: memory save notification ──
            await _emit_to_visualizers(request.session_id, {
                "type": "pipeline", "level": "pipeline",
                "step": "memory_save_start", "slug": f"chat/{request.session_id[:8]}",
            })

        # End the run buffer for this turn — starts the retention countdown.
        try:
            await get_run_buffer_registry().end_turn(request.session_id)
        except Exception as _eb:
            logger.debug("end_turn failed (buffered) for session %s: %s", request.session_id, _eb)

        return ChatResponse(
            reply=assistant_reply,
            response=assistant_reply,
            session_id=request.session_id,
        )

    except Exception as e:
        # Make sure we mark the run buffer ended even on error path.
        try:
            await get_run_buffer_registry().end_turn(request.session_id)
        except Exception:
            pass
        logger.error(f"Chat endpoint error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def _prepare_send(request: ChatRequest, fastapi_request: Request) -> Dict[str, Any]:
    """Synchronous prep shared by /send and /stream.

    Does everything that must happen *before* we hand the turn to the Run
    Manager: auth, agent resolution + access/billing gating, session + participant
    setup, persisting the user message, and emitting the user_message event so it
    shows on every device instantly. Returns a dict the background turn executor
    needs, or ``{"slash_result": "..."}`` when the message was a slash command.

    NOTE: the RunBuffer + run-state are started inside the turn coroutine (not
    here), so that when a new message INTERRUPTS an active run, the prior run's
    finalize and the new run's begin can't race on the single session_runs row.
    A new message never refuses — it interrupts the current run (see
    RunManager.start_or_replace).
    """
    from app.auth.identity import assert_caller_is
    request.user_id = await assert_caller_is(fastapi_request, request.user_id)
    db = get_db()
    channel = "web_portal"

    # ── Temp DB resolution for optimizer/closer sessions ──
    _temp_db_path = None
    if request.session_id.startswith('optimizer-') or request.session_id.startswith('closer-'):
        _meta_conn = db._get_conn()
        try:
            _meta_row = _meta_conn.execute(
                "SELECT metadata FROM sessions WHERE id=?", (request.session_id,)
            ).fetchone()
            if _meta_row and _meta_row[0]:
                _meta = json.loads(_meta_row[0])
                _temp_db_path = _meta.get('temp_db_path')
        finally:
            _meta_conn.close()
        if _temp_db_path:
            from app.db.local import LocalBackend as _OptBackend
            db = _OptBackend(db_path=_temp_db_path)
            logger.info("Using temp DB for %s session: %s", request.session_id[:12], _temp_db_path)

    # ── Terminal tunnel: the user is driving a program directly ──
    # If this chat is bound to a live terminal, the message is a keystroke line
    # for that program, not a prompt for the agent. Route it to the PTY
    # (persisted but context-excluded) and stop — no slash parsing, no agent run.
    # The user ends the tunnel with the UI "Hand back" button, never by typing.
    try:
        from app.agent.terminal_tunnel import route_user_line as _route_tunnel
        if await _route_tunnel(db, request.user_id, request.session_id, request.message or ""):
            return {"tunnel_handled": True}
    except Exception as _tun_err:
        logger.warning("tunnel routing failed for %s: %s", request.session_id, _tun_err)

    # ── Handle slash commands ──
    # Built-in system commands first (not template-driven). /compact folds the
    # session's older turns now; see _handle_compact_command.
    if _is_compact_command(request.message or ""):
        result = await _handle_compact_command(request.user_id, request.session_id, db)
        return {"slash_result": result}
    _slash_match = _match_slash_command(request.message or "")
    if _slash_match:
        _slash_key, _slash_arg, _slash_tid = _slash_match
        if _slash_tid == "opt_planner":
            # Pass the parsed argument (text AFTER the command) as feedback — not the
            # whole raw message. Passing request.message smuggled the literal
            # "/optimize" in as user feedback to the Planner.
            result = await _handle_optimize_command(
                request.user_id, request.session_id, _slash_arg, channel, db,
            )
        else:
            result = await _handle_generic_slash_command(
                _slash_tid, _slash_key, _slash_arg,
                request.user_id, request.session_id, channel, db,
            )
        return {"slash_result": result}

    # Ensure the session exists before inserting interactions
    _session_title = _session_title_from_message(request.message) if (request.message or "").strip() else None
    await _ensure_session(db, request.user_id, request.session_id, title=_session_title)

    # ── Optimizer / Closer session: route to dedicated agent ──
    opt_template_id = None
    opt_metadata = {}
    if request.session_id.startswith('optimizer-') or request.session_id.startswith('closer-'):
        conn = db._get_conn()
        try:
            meta_row = conn.execute(
                "SELECT metadata FROM sessions WHERE id=?", (request.session_id,)
            ).fetchone()
            opt_metadata = json.loads(meta_row[0]) if meta_row and meta_row[0] else {}
            if request.session_id.startswith('closer-'):
                opt_role = 'closer'
                opt_template_id = trigger_index.get('tool_call', 'handoff_to_closer') or 'opt_closer'
            else:
                opt_role = opt_metadata.get('opt_role', 'planner')
                opt_template_id = (
                    trigger_index.get('tool_call', 'run_optimizer') or 'opt_planner'
                    if opt_role == 'planner'
                    else trigger_index.get('tool_call', 'handoff_to_closer') or 'opt_closer'
                )
        finally:
            conn.close()

        _agent_db = get_db() if _temp_db_path else db
        agent = await _agent_db.get_or_resolve_session_agent(
            session_id=request.session_id,
            user_id=request.user_id,
            template_id=opt_template_id,
        )
        if _temp_db_path and agent and agent.get("id"):
            try:
                await db.bind_session_to_agent(request.session_id, agent["id"])
            except Exception:
                pass
        if not agent:
            raise RuntimeError(
                f"Failed to resolve optimizer agent (role={opt_role}) for user {request.user_id}. "
                f"Check that agent template '{opt_template_id}' exists."
            )
    else:
        req_agent_id = getattr(request, 'agent_id', None)
        req_template = getattr(request, 'agent_template_id', None)
        if req_template in ('admin-agent', 'integration-admin-agent', 'web-agent-tui'):
            if not await db.is_user_admin(request.user_id):
                raise HTTPException(status_code=403, detail="This agent is only available to admin users.")
            agent = await db.get_or_resolve_session_agent(
                session_id=request.session_id,
                user_id=request.user_id,
                template_id=req_template,
            )
        elif req_agent_id:
            agent = await db.get_agent_by_id(req_agent_id)
            if agent:
                admin_users = agent.get("admin_users") or []
                if isinstance(admin_users, str):
                    try:
                        admin_users = json.loads(admin_users)
                    except Exception:
                        admin_users = []
                if request.user_id not in admin_users:
                    await db.add_agent_member(req_agent_id, request.user_id)
        else:
            agent = await db.get_agent_for_user(request.user_id)
        if agent is None:
            raise HTTPException(
                status_code=400,
                detail="No agent assigned. Create an agent before chatting.",
            )

    # ── Access policy + billing enforcement ──
    await _enforce_agent_access_policy(db, agent, request.user_id)
    await _enforce_billing_access(db, agent, request.user_id)

    # ── Bind session to agent ──
    existing_agent_id = await db.get_session_agent_id(request.session_id)
    if existing_agent_id is None:
        await db.bind_session_to_agent(request.session_id, agent["id"])
    elif existing_agent_id != agent["id"]:
        raise RuntimeError(
            f"Session {request.session_id[:8]} bound to agent {existing_agent_id[:8]}, "
            f"but resolved agent is {agent['id'][:8]}. Cannot respond."
        )

    # ── Participants ──
    if not await db.is_session_participant(request.session_id, request.user_id, 'user'):
        await db.add_session_participant(request.session_id, request.user_id, 'user')
    if not await db.is_session_participant(request.session_id, agent["id"], 'agent'):
        await db.add_session_participant(request.session_id, agent["id"], 'agent')

    # ── Persist the user message ──
    user_interaction_id = await db.insert_interaction(
        request.user_id, request.session_id, role="user", content=request.message,
        channel=channel,
        metadata=json.dumps({"source": "web_portal_chat"}),
        input_data=json.dumps(request.model_dump(), default=str),
        sender_id=request.user_id,
        receiver_id=agent["id"],
    )

    # Record the mode this turn runs in so the pill is restorable server-side.
    await _record_session_execution_mode(db, request.session_id, getattr(request, 'execution_mode', 'ask'))

    # ── Emit the user message so all subscribed devices render it instantly ──
    # (The RunBuffer + run-state for the new turn are started inside the turn
    # coroutine. If an old run is still active, its buffer stamps this event;
    # seq stays monotonic across the interrupt.)
    await _emit_to_visualizers(request.session_id, {
        "type": "user_message", "level": "user",
        "content": request.message, "id": user_interaction_id,
    }, user_id=request.user_id)

    # ── TUI bridge check: if the agent is a TUI bridge agent, forward the
    # message to the TUI instead of running the normal agent loop.
    is_tui_bridge = (
        agent.get("trigger_type") == "tui_bridge"
        or agent.get("template_id") == "web-agent-tui"
    )

    return {
        "db": db,
        "agent": agent,
        "user_interaction_id": user_interaction_id,
        "channel": channel,
        "is_tui_bridge": is_tui_bridge,
    }


async def _run_turn_background(
    db, request: ChatRequest, agent: Dict[str, Any],
    user_interaction_id: str, channel: str = "web_portal",
    replaced: bool = False,
) -> None:
    """Execute one agent turn to completion, fully decoupled from any client
    connection. Owned by the Run Manager — survives the sender leaving, closing
    the browser, switching sessions/devices. Every event is emitted via
    ``_emit_to_visualizers`` (→ RunBuffer stamp + per-user WS broadcast), and the
    agent loop streams its answer into the DB. On finish, run-state is flipped to
    its terminal status and the RunBuffer retention countdown begins.

    ``replaced`` is True when this turn is replacing a run the user just
    interrupted by sending a new message; the agent is told so it can read the
    new message as a course-correction / stop / addition relative to its
    interrupted partial answer."""
    session_id = request.session_id
    user_id = request.user_id
    final_status = "complete"
    _last_seq_persist = 0.0

    # Start the RunBuffer + durable run-state for THIS turn. Done here (not in
    # _prepare_send) so a replaced run's begin happens strictly after the prior
    # run's finalize — no race on the single session_runs row.
    _run_buffer = await get_run_buffer_registry().start_turn(
        session_id=session_id, user_id=user_id, turn_id=user_interaction_id, db=db,
    )
    try:
        # origin='web' + a relaunch recipe so the self-healing layer can re-ignite
        # this turn headlessly (boot recovery / watchdog) if it dies involuntarily.
        _web_relaunch_ctx = json.dumps({
            "origin": "web", "session_id": session_id, "user_id": user_id,
            "agent_id": agent.get("id"), "channel": channel,
            "turn_id": user_interaction_id,
        })
        await db.run_state_begin(
            session_id, user_id, agent.get("id"), user_interaction_id,
            origin="web", relaunch_ctx=_web_relaunch_ctx,
        )
    except Exception as _rse:
        logger.debug("run_state_begin failed: %s", _rse)
    # Backfill seq on the already-saved user row from the buffer's first slot.
    try:
        _user_ss, _user_ts = _run_buffer.next_seq()
        _conn = db._get_conn()
        try:
            _conn.execute(
                "UPDATE interactions SET session_seq=?, turn_id=?, turn_seq=? WHERE id=?",
                (_user_ss, user_interaction_id, _user_ts, user_interaction_id),
            )
            _conn.commit()
        finally:
            _conn.close()
    except Exception as _seqerr:
        logger.debug("Failed to backfill seq on user row: %s", _seqerr)

    async def event_callback(event: Dict[str, Any]):
        nonlocal final_status, _last_seq_persist
        await _emit_to_visualizers(session_id, event, user_id=user_id)
        et = event.get("type")
        if et == "interrupted":
            final_status = "interrupted"
        elif et == "error":
            final_status = "error"
        # Throttled advance of the durable latest_session_seq (drives WS resume
        # for cold devices). The RunBuffer holds the real events; this is just a
        # cheap pointer so a fresh device knows where the live stream is up to.
        ss = event.get("session_seq")
        if ss is not None:
            now = time.monotonic()
            if now - _last_seq_persist > 1.0:
                _last_seq_persist = now
                try:
                    await db.run_state_update_seq(session_id, int(ss))
                except Exception:
                    pass

    try:
        # Re-fetch agent with context documents if missing.
        nonlocal_agent = agent
        if not nonlocal_agent.get("context_documents"):
            _fetched = await db.fetch_agent_by_id_with_context(
                nonlocal_agent["id"], CONTEXT_SECTION_TYPES, user_id=user_id)
            if _fetched is not None:
                nonlocal_agent = _fetched
        agent = nonlocal_agent

        loop_config = LoopConfig.from_agent(agent)

        # Local Claude Code (and any alternate-runtime) agent hands its WHOLE turn
        # to its own engine adapter (see the engine seam in app/agent/loop.py) — it
        # runs `claude` directly, not webAgent's loop. None of the normal turn
        # plumbing applies, so skip the memory nodes: a Claude run should show no
        # memory_search / memory_save bubbles around it.
        _engine_id = ""
        try:
            _eng_meta = agent.get("metadata")
            if isinstance(_eng_meta, str):
                _eng_meta = json.loads(_eng_meta or "{}")
            if isinstance(_eng_meta, dict):
                _engine_id = str(_eng_meta.get("engine") or "").strip()
        except Exception:
            _engine_id = ""
        _is_engine_agent = bool(_engine_id) and _engine_id != "default"

        await event_callback({
            "type": "pipeline", "level": "pipeline", "step": "agent_assigned",
            "agent_id": agent["id"], "max_turn_count": agent.get("max_turn_count", 0),
        })

        if not agent.get("context_documents") and loop_config.is_enabled("copy_defaults"):
            copied = await db.copy_defaults_to_agent(agent["id"], template_id='default')
            if copied > 0:
                agent = await db.fetch_agent_by_id_with_context(
                    agent["id"], CONTEXT_SECTION_TYPES, user_id=user_id)

        context_docs = agent.get("context_documents", [])
        doc_types = list(set(
            (d.get("context_type") or d.get("doc_type") or "")
            for d in context_docs if d.get("context_type") or d.get("doc_type")
        ))
        await event_callback({
            "type": "pipeline", "level": "pipeline", "step": "load_context",
            "count": len(context_docs), "types": doc_types,
        })

        # ── PHASE 1: Brain-first lookup ──
        # Launched here, awaited just before the prompt is built, so the
        # embedding round-trip overlaps with attachment/image prep (#2).
        brain_results = []
        brain_context = None
        parent_id = None
        _brain_task = None
        if _is_engine_agent:
            # No memory plumbing for engine agents — the adapter owns the whole
            # turn. The adapter's persisted rows parent to the user message.
            parent_id = user_interaction_id
        elif not loop_config.is_enabled("memory_search") or _should_skip_memory(request.message):
            _skip_reason = "node_disabled" if not loop_config.is_enabled("memory_search") else "greeting_or_cmd"
            await event_callback({"type": "pipeline", "level": "pipeline",
                                  "step": "memory_search_skip", "reason": _skip_reason})
            parent_id = await db.insert_interaction(
                user_id, session_id, role="tool",
                content=json.dumps({"skipped": True, "reason": _skip_reason}),
                parent_id=user_interaction_id, tool_name="memory_search", channel=channel,
                metadata=json.dumps({"brain": True, "skipped": True, "reason": _skip_reason}),
                input_data=json.dumps({"query": request.message, "skipped": True}),
                sender_id=agent["id"], receiver_id=agent["id"],
            )
            await event_callback({"type": "pipeline", "level": "pipeline",
                                  "step": "memory_search_end", "results_count": 0,
                                  "results": [], "skipped": True})
        else:
            await event_callback({"type": "pipeline", "level": "pipeline",
                                  "step": "memory_search_start", "query": request.message, "limit": 5})
            # Short messages take the keyword-only fast path (no embedding) (#3).
            _use_vector = not _should_skip_vector(request.message)
            _brain_task = asyncio.create_task(
                db.memory_search(request.user_id, request.message, limit=5, vector=_use_vector)
            )

        # ── Resolve attachments + vision fallback ──
        attachment_context = None
        attachment_docs: List[Dict[str, Any]] = []
        if request.attachment_ids:
            for att_id in request.attachment_ids:
                att = await db.get_attachment(att_id)
                if att:
                    attachment_docs.append(att)
            if attachment_docs:
                attachment_context = format_attachments_for_prompt(attachment_docs)
                await event_callback({
                    "type": "attachment", "level": "agent",
                    "attachments": [
                        {"id": a["id"], "original_name": a["original_name"],
                         "mime_type": a["mime_type"], "size_bytes": a["size_bytes"],
                         "storage_path": a.get("storage_path", "")}
                        for a in attachment_docs
                    ],
                })
        _desc_out = {}
        # Engine agents (e.g. Local Claude Code) read attached images as real
        # files with their own vision-capable tools, so skip the vision-describe
        # router entirely — it would otherwise burn a vision-model call, or (on a
        # non-vision brain) fold a "you can't see this image" note that flatly
        # contradicts the image file the engine is about to hand Claude.
        if not _is_engine_agent:
            async for _dev in _maybe_describe_images(
                db, request.user_id, request.message, user_interaction_id,
                loop_config, attachment_docs, _desc_out,
                agent_id=agent["id"], session_id=session_id,
            ):
                await event_callback(_dev)
        # App Control point-and-share fingerprint → foldable chip + this-turn fold.
        async for _dev in _maybe_emit_app_control(
            db, request, user_interaction_id, _desc_out,
            agent_id=agent["id"], session_id=session_id, channel=channel,
        ):
            await event_callback(_dev)
        user_message_content = await build_user_message_content(
            _desc_out.get("message_text", request.message),
            _desc_out.get("inline_docs", attachment_docs),
        )

        # ── PHASE 1 (cont.): await the memory lookup launched above, fold into prompt ──
        if _brain_task is not None:
            brain_results = await _brain_task
            await event_callback({
                "type": "pipeline", "level": "pipeline", "step": "memory_search_end",
                "results_count": len(brain_results),
                "results": [{"slug": r["slug"], "title": r.get("title", r["slug"]),
                             "score": round(r.get("rank", 0), 2)} for r in (brain_results or [])],
            })
            if brain_results:
                lines = []
                for r in brain_results:
                    slug = r.get("slug", "?")
                    title = r.get("title", slug)
                    ct = r.get("compiled_truth", "")[:300]
                    rank = r.get("rank", 0)
                    lines.append(f"## {slug} — {title} (score: {rank:.2f})")
                    if ct:
                        lines.append(ct)
                    lines.append("")
                brain_context = "\n".join(lines)
            search_content = json.dumps({
                "query": request.message,
                "results": [
                    {"slug": r["slug"], "title": r.get("title", ""), "score": round(r.get("rank", 0), 2),
                     "snippet": r.get("compiled_truth", "")[:150]}
                    for r in (brain_results or [])
                ],
                "count": len(brain_results or []),
            }, indent=2)
            parent_id = await db.insert_interaction(
                user_id, session_id, role="tool", content=search_content,
                parent_id=user_interaction_id, tool_name="memory_search", channel=channel,
                metadata=json.dumps({"count": len(brain_results or []), "brain": True,
                                     "has_results": bool(brain_results)}),
                input_data=json.dumps({"query": request.message}), output_data=search_content,
                sender_id=agent["id"], receiver_id=agent["id"],
            )
            await event_callback({"type": "tool_result", "level": "agent", "tool": "memory_search",
                                  "result": search_content[:2000], "duration_ms": 0, "error": False})

        _agent_id_for_prompt = agent.get("id") if agent else None
        system_prompt = await build_system_prompt(
            context_docs, brain_context, request.user_id, agent_id=_agent_id_for_prompt)
        # The attachment summary tells the model to call the `read_attachment`
        # tool by attachment_id — meaningful only to the default loop. An engine
        # agent (Local Claude Code) has no such tool; it gets the real file paths
        # from its own adapter instead, so don't feed it this contradictory note.
        if attachment_context and not _is_engine_agent:
            system_prompt = system_prompt + "\n\n" + attachment_context
        system_prompt = await append_skills_section(system_prompt, agent, request.session_id, caller_user_id=request.user_id)

        from app.tools.loader import load_tools
        tools = await load_tools(request.user_id, agent_id=_agent_id_for_prompt or "",
                                 agent_template_id=agent.get("template_id") if agent else None,
                                 gate_caller_access=True)
        await event_callback({
            "type": "pipeline", "level": "pipeline", "step": "build_prompt", "sections": ["SYSTEM"],
            "brain_injected": bool(brain_context), "tool_count_in_prompt": len(tools),
            "system_prompt": system_prompt[:8000],
        })

        try:
            _ds_attached = await db.agent_data_source_list(_agent_id_for_prompt, enabled_only=True) if _agent_id_for_prompt else []
        except Exception:
            _ds_attached = []
        await event_callback({
            "type": "pipeline", "level": "pipeline", "step": "data_src_loaded",
            "attached_count": len(_ds_attached),
            "sources": [{"name": a.get("name"), "type": a.get("type"), "tool_alias": a.get("tool_alias")}
                        for a in _ds_attached],
        })

        history = await build_openai_history_from_session(
            db, request.user_id, request.session_id,
            exclude_interaction_ids={user_interaction_id} if user_interaction_id else set(),
            agent_id=agent.get("id"),
        )

        # If this turn replaced one the user interrupted, tell the agent so it
        # reads the new message as a course-correction / stop / addition relative
        # to the partial answer it had started. The agent decides what to do.
        if replaced:
            history.append({
                "role": "system",
                "content": (
                    "The user sent a new message while you were still responding, so your "
                    "previous answer was interrupted (you can see your partial reply above). "
                    "Read their new message carefully and respond to it: they may be telling "
                    "you to STOP (acknowledge briefly and stop), steering you in a different "
                    "direction (adjust accordingly), or adding information (incorporate it). "
                    "Do not simply repeat your interrupted answer."
                ),
            })

        _raw_at = agent.get("allowed_tools", [])
        if isinstance(_raw_at, str):
            try:
                _raw_at = json.loads(_raw_at)
            except Exception:
                _raw_at = []

        assistant_reply = ""
        _exec_mode = getattr(request, 'execution_mode', 'ask') or 'ask'
        async for event in stream_agent_events(
            user_id=request.user_id, session_id=request.session_id,
            user_message=user_message_content, system_prompt=system_prompt,
            agent_id=agent["id"], history=history, parent_interaction_id=parent_id,
            max_turns=agent.get("max_turn_count", 0), channel=channel, db=db,
            agent_template_id=agent.get("template_id"), allowed_tools=_raw_at or None,
            execution_mode=_exec_mode,
            # Pass the resolved attachment rows so an engine agent (e.g. Local
            # Claude Code) can read pasted images / attached files off disk with
            # its own tools — the default loop already inlines images itself.
            attachment_docs=attachment_docs,
        ):
            await event_callback(event)
            if event["type"] == "response":
                assistant_reply = event["content"]
            elif event["type"] == "error" and not assistant_reply:
                assistant_reply = f"I encountered an error: {event['message']}"
            elif event["type"] == "interrupted" and not assistant_reply:
                assistant_reply = f"I was interrupted: {event['message']}"

        # ── Background memory save ── (skipped for engine agents — no plumbing)
        if not _is_engine_agent and 'memory_save' not in set(_raw_at or []) and loop_config.is_enabled("memory_save"):
            asyncio.create_task(_save_chat_to_memory(
                db, request.user_id, request.session_id,
                request.message, assistant_reply, agent["id"], parent_id,
            ))
            await event_callback({"type": "pipeline", "level": "pipeline",
                                  "step": "memory_save_start", "slug": f"chat/{request.session_id[:8]}"})

        # ── Background turn-lifecycle hooks (ability TURN_HOOK) ──
        # Fire-and-forget after every turn. The primary consumer is the session
        # titler ability which auto-names the session from its first few turns.
        try:
            from app.abilities import turn_hooks_for_agent
            for _hook in await turn_hooks_for_agent(agent.get("id", "")):
                _spawn_bg(
                    _hook(
                        db, request.user_id, request.session_id,
                        lambda ev: _emit_to_user_listeners(request.user_id, ev),
                    ),
                    label=f"turn_hook:{session_id[:8]}",
                )
        except Exception as _th:
            logger.warning("turn hooks dispatch failed for %s: %s",
                           session_id[:12], _th)
    except asyncio.CancelledError:
        # Hard-cancelled (replace grace timeout, or watchdog frozen-cancel). Mark
        # interrupted so it isn't wrongly recorded 'complete'; the stop_cause was
        # already tagged by whoever requested the stop (replaced / frozen).
        final_status = "interrupted"
        raise
    except Exception as e:
        final_status = "error"
        logger.error("Background turn failed for session %s: %s", session_id, e, exc_info=True)
        try:
            await _emit_to_visualizers(session_id, {
                "type": "error", "level": "agent", "message": str(e),
            }, user_id=user_id)
        except Exception:
            pass
    finally:
        # Derive the machine cause from the terminal status. A voluntary cause
        # (user_stop / replaced) already on the row is preserved by run_state_finish.
        _web_cause = ("complete" if final_status == "complete"
                      else "crash" if final_status == "error" else None)
        try:
            await db.run_state_finish(session_id, status=final_status, stop_cause=_web_cause)
        except Exception as _rsf:
            logger.debug("run_state_finish failed for %s: %s", session_id, _rsf)
        try:
            await get_run_buffer_registry().end_turn(session_id)
        except Exception as _eb:
            logger.debug("end_turn failed for session %s: %s", session_id, _eb)


async def _resume_web_turn(rc: Dict[str, Any], replaced: bool):
    """Self-healing resume builder for the 'web' origin. Re-ignites an
    involuntarily-stopped interactive turn from durable history, emitting through
    the SAME RunBuffer + WebSocket path a live turn uses, so an attached chat sees
    the continuation stream in. Run-state begin/finish are owned by the runner;
    this only executes the turn and returns the outcome."""
    from app.agent.runner import RunOutcome, RESUME_NUDGE
    db = get_db()
    session_id = rc.get("session_id")
    user_id = rc.get("user_id")
    # Use the session's CURRENT agent — a mid-session delegation may have rebound it.
    agent_id = await db.get_session_agent_id(session_id) or rc.get("agent_id")
    agent = None
    if agent_id:
        agent = await db.fetch_agent_by_id_with_context(
            agent_id, CONTEXT_SECTION_TYPES, user_id=user_id)
        if agent is None:
            agent = await db.get_agent_by_id(agent_id)
    if not agent:
        return RunOutcome(status="error", stop_cause="failed",
                          error="agent not found for web resume")

    final_status = "complete"
    reply = ""
    _run_buffer = await get_run_buffer_registry().start_turn(
        session_id=session_id, user_id=user_id, turn_id=rc.get("turn_id"), db=db,
    )

    async def event_callback(event: Dict[str, Any]):
        nonlocal final_status
        await _emit_to_visualizers(session_id, event, user_id=user_id)
        et = event.get("type")
        if et == "interrupted":
            final_status = "interrupted"
        elif et == "error":
            final_status = "error"
        ss = event.get("session_seq")
        if ss is not None:
            try:
                await db.run_state_update_seq(session_id, int(ss))
            except Exception:
                pass

    try:
        loop_config = LoopConfig.from_agent(agent)
        context_docs = agent.get("context_documents", [])
        system_prompt = await build_system_prompt(
            context_docs, None, user_id, agent_id=agent_id)
        system_prompt = await append_skills_section(system_prompt, agent, session_id, caller_user_id=user_id)
        # Resume replays a BOUNDED checkpoint, not the whole transcript: the
        # recent steps + the task anchor, so a re-ignition doesn't grow the
        # working set with conversation length (the runaway-memory cause).
        history = await build_openai_history_from_session(db, user_id, session_id, agent_id=agent_id)
        history = trim_history_for_resume(history)
        _raw_at = agent.get("allowed_tools", [])
        if isinstance(_raw_at, str):
            try:
                _raw_at = json.loads(_raw_at)
            except Exception:
                _raw_at = []
        await event_callback({
            "type": "resumed", "level": "agent",
            "reason": rc.get("resume_reason", "server_restart"),
        })
        async for event in stream_agent_events(
            user_id=user_id, session_id=session_id, user_message=RESUME_NUDGE,
            system_prompt=system_prompt, agent_id=agent_id, history=history,
            max_turns=agent.get("max_turn_count", 0), channel=rc.get("channel"), db=db,
            agent_template_id=agent.get("template_id"), allowed_tools=_raw_at or None,
        ):
            await event_callback(event)
            if event["type"] == "response":
                reply = event["content"]
    except asyncio.CancelledError:
        final_status = "interrupted"
        raise
    except Exception as e:
        final_status = "error"
        logger.error("web resume failed for session %s: %s", session_id, e, exc_info=True)
    finally:
        try:
            await get_run_buffer_registry().end_turn(session_id)
        except Exception:
            pass

    from app.agent.runner import RunOutcome as _RO
    cause = ("complete" if final_status == "complete"
             else "crash" if final_status == "error" else None)
    return _RO(status=final_status, stop_cause=cause, reply=reply)


# Register the web-origin resume builder so the self-healing layer (boot recovery
# + liveness watchdog) can re-ignite interactive turns with UI streaming intact.
try:
    from app.agent.runner import register_resume_builder as _rrb
    _rrb("web", _resume_web_turn)
except Exception as _rrb_err:  # pragma: no cover
    logger.debug("Could not register web resume builder: %s", _rrb_err)


async def _sse_tail_run(session_id: str):
    """SSE fallback: tail the RunBuffer for a session and yield events as they
    appear, then stop when the run completes. Fully decoupled from the run — if
    this client disconnects, the supervised run keeps going. The WebSocket
    subscriber is the primary live path; this exists so the old streaming
    endpoint keeps working."""
    reg = get_run_buffer_registry()
    rm = get_run_manager()
    last = 0
    idle = 0.0
    while True:
        buf = reg.get(session_id)
        if buf is not None:
            missed = buf.replay_after(last)
            for ev in missed:
                last = ev.get("session_seq", last)
                yield f"data: {json.dumps(ev)}\n\n"
            if buf.completed_at is not None and not buf.replay_after(last):
                break
        else:
            if not rm.is_running(session_id):
                break
        if not rm.is_running(session_id):
            # run ended; drain any final buffered events then stop
            buf = reg.get(session_id)
            if buf is None or not buf.replay_after(last):
                break
        await asyncio.sleep(0.08)
        idle += 0.08
        if idle >= 20:
            idle = 0.0
            yield ": ping\n\n"


# ── TUI Bridge registration ──────────────────────────────────────────────────

class TuiBridgeRegisterRequest(BaseModel):
    port: int
    user_id: str


@router.post("/tui-bridge/register")
async def tui_bridge_register(request: TuiBridgeRegisterRequest, fastapi_request: Request):
    """Register a TUI bridge server for the current user.
    
    Called by the TUI app when it starts its bridge server. The web app uses
    this to forward messages to the TUI agent when the user chats with the
    "Web Agent TUI" agent.
    
    Side effect: ensures the admin user has a materialized "web-agent-tui"
    agents row so it appears in the frontend agent dropdown as a selectable
    agent.
    """
    from app.auth.identity import assert_caller_is
    user_id = await assert_caller_is(fastapi_request, request.user_id)
    from app.tui_bridge import register_bridge
    register_bridge(user_id, request.port)

    # Ensure the admin has a materialized web-agent-tui agent
    try:
        db = get_db()
        agent = await db.get_or_resolve_session_agent(
            session_id=f"tui-init-{request.port}",
            user_id=user_id,
            template_id="web-agent-tui",
        )
        # Clean up the init session — it was just a vehicle for materialization
        try:
            conn = db._get_conn()
            conn.execute("DELETE FROM sessions WHERE id = ?", (f"tui-init-{request.port}",))
            conn.commit()
            conn.close()
        except Exception:
            pass
        logger.info("TUI bridge agent ready for user %s (agent_id=%s)", user_id[:12], agent.get("id", "?")[:12] if agent else "?")
    except Exception as e:
        logger.warning("Could not materialize web-agent-tui agent: %s", e)

    return {"status": "ok", "port": request.port}


@router.post("/tui-bridge/unregister")
async def tui_bridge_unregister(fastapi_request: Request):
    """Unregister the TUI bridge for the current user."""
    from app.auth.identity import assert_caller_is
    user_id = await assert_caller_is(fastapi_request, None)
    if user_id:
        from app.tui_bridge import unregister_bridge
        unregister_bridge(user_id)
    return {"status": "ok"}


@router.get("/tui-bridge/status")
async def tui_bridge_status(fastapi_request: Request):
    """Check if a TUI bridge is registered for the current user."""
    from app.auth.identity import assert_caller_is
    user_id = await assert_caller_is(fastapi_request, None)
    from app.tui_bridge import is_bridge_alive, get_bridge_port
    return {
        "alive": is_bridge_alive(user_id) if user_id else False,
        "port": get_bridge_port(user_id) if user_id else None,
    }


@router.post("/send")
async def chat_send(request: ChatRequest, fastapi_request: Request):
    """Fire-and-forget send. Saves the user message, starts the agent turn as a
    supervised background run, and returns immediately. All output (including for
    the sending device) is rendered from the DB + the per-user WebSocket — see
    /api/v1/agent/ws and /api/v1/db/session-messages. Leaving, closing the
    browser, switching sessions/devices does NOT interrupt the run.

    A new message sent while the agent is still working **interrupts** the
    current run and starts a fresh one that includes the interrupted partial +
    the new message; the agent then decides whether to stop, steer, or continue.
    """
    prep = await _prepare_send(request, fastapi_request)
    if prep.get("tunnel_handled"):
        # Routed straight to the bound terminal — no agent run. Output streams
        # back over the embedded terminal; nothing for the chat bubble flow.
        return {"status": "tunnelled", "session_id": request.session_id}
    if "slash_result" in prep:
        return {"status": "ok", "session_id": request.session_id, "reply": prep["slash_result"]}

    # ── Cross-device hand-off ──
    # If this turn targets another device, enqueue it for that device's worker to
    # run inside THIS session. The user turn is already saved (above), and the
    # reply is appended here over the shared DB when the target finishes. The
    # atomic claim guarantees exactly one device runs it. Targeting our own
    # device id just falls through to the normal local run.
    target_device = getattr(request, "target_device", None)
    if target_device:
        from app.devices import dispatch as _dispatch
        from app.devices import identity as _identity
        dev = await _dispatch.resolve_target(target_device)
        target_instance = (dev or {}).get("instance_id") or target_device
        target_label = (dev or {}).get("label") or target_device
        if target_instance != _identity.device_id():
            job_id = await _dispatch.enqueue(
                owner_user_id=request.user_id,
                prompt=request.message or "",
                agent_id=prep["agent"]["id"],
                target_instance=target_instance,
                target_label=target_label,
                payload={"run_in_session": request.session_id,
                         "execution_mode": getattr(request, "execution_mode", "ask") or "ask",
                         "source": "chat"},
            )
            logger.info("Chat turn handed to device %s (job %s, session %s)",
                        target_label, job_id[:8], request.session_id[:12])
            return {"status": "queued", "session_id": request.session_id,
                    "turn_id": prep["user_interaction_id"], "job_id": job_id,
                    "device": target_label}

    # ── TUI bridge path: forward to the TUI agent instead of running the
    # normal agent loop. The TUI processes the message and streams the reply
    # back. This is synchronous (the TUI bridge returns the full reply).
    # The web app does NOT apply its guardrails (tool gates, turn limits,
    # billing enforcement, memory save, context loading) — the TUI handles
    # all of that internally. Only execution_mode is passed through so the
    # frontend's read/write/auto toggle still works.
    if prep.get("is_tui_bridge"):
        from app.tui_bridge import forward_to_bridge, is_bridge_alive

        if is_bridge_alive(request.user_id):
            reply = await forward_to_bridge(
                request.user_id, request.session_id, request.message or "",
                execution_mode=getattr(request, 'execution_mode', 'ask') or 'ask',
            )
            if reply is not None:
                # Persist the assistant reply
                try:
                    await prep["db"].insert_interaction(
                        request.user_id, request.session_id,
                        role="assistant", content=reply,
                        parent_id=prep["user_interaction_id"],
                        channel=prep["channel"],
                        sender_id=prep["agent"]["id"],
                        receiver_id=request.user_id,
                    )
                except Exception as e:
                    logger.warning("Failed to persist TUI bridge reply: %s", e)

                # Emit the response event so the UI renders it
                await _emit_to_visualizers(request.session_id, {
                    "type": "response", "level": "agent",
                    "content": reply, "turn_id": prep["user_interaction_id"],
                }, user_id=request.user_id)

                return {
                    "status": "complete",
                    "session_id": request.session_id,
                    "turn_id": prep["user_interaction_id"],
                    "reply": reply,
                }
            else:
                # Bridge not available — fall through to error
                pass

        # Bridge not available — return an error message
        error_msg = ("The TUI agent is not running. "
                     "Start the Server Manager (TUI) to use this agent.")
        await _emit_to_visualizers(request.session_id, {
            "type": "error", "level": "agent",
            "message": error_msg, "turn_id": prep["user_interaction_id"],
        }, user_id=request.user_id)
        return {
            "status": "error",
            "session_id": request.session_id,
            "turn_id": prep["user_interaction_id"],
            "error": error_msg,
        }

    status = await get_run_manager().start_or_replace(
        session_id=request.session_id,
        user_id=request.user_id,
        turn_id=prep["user_interaction_id"],
        db=prep["db"],
        run_factory=lambda replaced: _run_turn_background(
            prep["db"], request, prep["agent"], prep["user_interaction_id"],
            prep["channel"], replaced=replaced),
    )
    return {
        "status": status,  # "running" or "replacing"
        "session_id": request.session_id,
        "turn_id": prep["user_interaction_id"],
    }


@router.post("/stream")
async def chat_stream(request: ChatRequest, fastapi_request: Request):
    """
    SSE fallback for sending a message. Behaves like /send (saves the message and
    starts a supervised, connection-independent run) but also tails the run's
    events back over Server-Sent Events for this client. A disconnect here never
    interrupts the run — it keeps going server-side and is viewable from any
    device via the DB + WebSocket. Prefer /send + the WebSocket for new clients.
    """
    prep = await _prepare_send(request, fastapi_request)
    if prep.get("tunnel_handled"):
        async def _tunnel_noop():
            yield f"data: {json.dumps({'type': 'response', 'level': 'agent', 'content': ''})}\n\n"
        return StreamingResponse(_tunnel_noop(), media_type="text/event-stream")
    if "slash_result" in prep:
        result = prep["slash_result"]
        async def _slash_events():
            yield f"data: {json.dumps({'type': 'stream', 'level': 'agent', 'content': result})}\n\n"
            yield f"data: {json.dumps({'type': 'response', 'level': 'agent', 'content': result})}\n\n"
        return StreamingResponse(_slash_events(), media_type="text/event-stream")

    # ── TUI bridge path ──
    if prep.get("is_tui_bridge"):
        from app.tui_bridge import forward_to_bridge, is_bridge_alive

        async def _tui_bridge_events():
            if is_bridge_alive(request.user_id):
                reply = await forward_to_bridge(
                    request.user_id, request.session_id, request.message or "",
                    execution_mode=getattr(request, 'execution_mode', 'ask') or 'ask',
                )
                if reply is not None:
                    # Persist the assistant reply
                    try:
                        await prep["db"].insert_interaction(
                            request.user_id, request.session_id,
                            role="assistant", content=reply,
                            parent_id=prep["user_interaction_id"],
                            channel=prep["channel"],
                            sender_id=prep["agent"]["id"],
                            receiver_id=request.user_id,
                        )
                    except Exception as e:
                        logger.warning("Failed to persist TUI bridge reply: %s", e)

                    yield f"data: {json.dumps({'type': 'stream', 'level': 'agent', 'content': reply})}\n\n"
                    yield f"data: {json.dumps({'type': 'response', 'level': 'agent', 'content': reply})}\n\n"
                else:
                    error_msg = "The TUI agent is not running. Start the Server Manager (TUI) to use this agent."
                    yield f"data: {json.dumps({'type': 'error', 'level': 'agent', 'message': error_msg})}\n\n"
            else:
                error_msg = "The TUI agent is not running. Start the Server Manager (TUI) to use this agent."
                yield f"data: {json.dumps({'type': 'error', 'level': 'agent', 'message': error_msg})}\n\n"

        return StreamingResponse(_tui_bridge_events(), media_type="text/event-stream")

    await get_run_manager().start_or_replace(
        session_id=request.session_id,
        user_id=request.user_id,
        turn_id=prep["user_interaction_id"],
        db=prep["db"],
        run_factory=lambda replaced: _run_turn_background(
            prep["db"], request, prep["agent"], prep["user_interaction_id"],
            prep["channel"], replaced=replaced),
    )

    async def safe_event_generator():
        try:
            async for chunk in _sse_tail_run(request.session_id):
                yield chunk
        except Exception as e:
            logger.error("SSE tail unhandled error: %s", e, exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'level': 'agent', 'message': str(e)})}\n\n"

    return StreamingResponse(safe_event_generator(), media_type="text/event-stream")



async def _save_chat_to_memory(
    db, user_id: str, session_id: str,
    user_message: str, assistant_reply: str, agent_id: str,
    parent_interaction_id: Optional[str] = None,
) -> None:
    """Save chat conversation to memory as visible tool interaction."""
    slug = f"chat/{session_id[:8]}"
    # Describe the save up-front so the memory_save_end event can carry it as the
    # tool-result body for the LIVE chat bubble even if the upsert below fails
    # (the persisted row uses the same content; reload renders from that).
    save_content = json.dumps({
        "action": "upserted",
        "slug": slug,
        "summary": f"Saved chat: {user_message[:60]}...",
    }, indent=2)
    save_args = {"user_message": user_message[:200], "assistant_reply": assistant_reply[:200]}
    save_ok = False
    try:
        # Save chat as a memory page
        result = await db.memory_upsert(
            user_id, slug, "meeting",
            title=f"Session {session_id[:8]}",
            compiled_truth=assistant_reply[:500],
            timeline=user_message[:200],
        )

        # Save memory_save as visible tool interaction
        await db.insert_interaction(
            user_id, session_id, role="tool",
            content=save_content,
            parent_id=parent_interaction_id,
            tool_name="memory_save",
            channel="web_portal",
            metadata=json.dumps({"brain": True, "slug": slug}),
            input_data=json.dumps(save_args),
            output_data=save_content,
            sender_id=agent_id,
            receiver_id=agent_id,
        )

        await _emit_to_visualizers(session_id, {
            "type": "db", "level": "db",
            "op": "memory_upsert", "slug": slug, "page_type": "meeting",
        }, user_id=user_id)
        save_ok = True
        logger.debug("Saved chat to memory: %s", slug)
    except Exception as e:
        logger.warning("Failed to save chat to memory: %s", e)
    finally:
        # Always tell the activity chip the save has FINISHED — on the error path
        # too — so the post-turn "Saving memory" note clears the moment the work is
        # actually done, rather than hanging until the frontend's settle backstop
        # (the bar treats this *_end as a terminal-for-housekeeping signal). The
        # args + result body let the live chat render this as its own foldable
        # memory_save tool bubble (mirroring the persisted reload render).
        try:
            await _emit_to_visualizers(session_id, {
                "type": "pipeline", "level": "pipeline",
                "step": "memory_save_end", "slug": slug,
                "args": save_args, "result": save_content, "ok": save_ok,
            }, user_id=user_id)
        except Exception:
            pass


# ── Visualizer listener registry ──
# WebSocket subscribers that receive pipeline events for HTTP chat sessions.
_visualizer_listeners: Dict[str, List[Any]] = {}  # session_id → list of WebSocket objects


def register_visualizer_listener(session_id: str, websocket: Any):
    """Register a WebSocket as a visualizer listener for a session."""
    if session_id not in _visualizer_listeners:
        _visualizer_listeners[session_id] = []
    _visualizer_listeners[session_id].append(websocket)


def unregister_visualizer_listener(session_id: str, websocket: Any):
    """Remove a WebSocket from the visualizer listeners."""
    if session_id in _visualizer_listeners:
        _visualizer_listeners[session_id] = [
            ws for ws in _visualizer_listeners[session_id] if ws is not websocket
        ]
        if not _visualizer_listeners[session_id]:
            del _visualizer_listeners[session_id]


async def _maybe_describe_images(db, user_id, message, user_interaction_id,
                                 loop_config, attachment_docs, out,
                                 agent_id="", session_id=""):
    """Attachment type-router step (async generator).

    Routes each attached image by the agent's actual model capability (see
    app/agent/attachment_router.py + plugins/abilities/image_vision.json):

      • inline     — the turn model can see images → leave them inlined.
      • describe   — blind brain + Image Vision enabled + a vision worker exists →
                     describe each image once via a one-shot vision model with a
                     context-tailored prompt, fold the description + guidance into
                     the user message, cache it on the attachment row.
      • unreadable — Image Vision off or no vision model configured → fold an
                     anti-hallucination note ("you can't read this; tell the user")
                     and DO NOT inline the image to the blind model.

    Yields pipeline event dicts for the caller to emit on its own transport, and
    writes results into ``out``:
      out["message_text"]  → text to send as the user message
      out["inline_docs"]   → attachments to pass to build_user_message_content
    """
    from app.agent.prompts import _VISION_INLINE_MIMES, describe_image_attachment
    from app.agent.attachment_router import plan_image_attachments

    out["message_text"] = message
    out["inline_docs"] = attachment_docs

    image_atts = [a for a in (attachment_docs or [])
                  if (a.get("mime_type") or "").lower() in _VISION_INLINE_MIMES]
    if not image_atts or not loop_config.is_enabled("attachment_describe"):
        return

    from app.admin.settings import load_llm_capabilities_for_user, media_routing
    try:
        caps = await load_llm_capabilities_for_user(user_id)
    except Exception as e:
        logger.warning("attachment route: capability read failed: %s", e)
        return
    routing = media_routing(caps)

    # Short conversation tail so the worker can tailor the description to context.
    context = ""
    if session_id:
        try:
            recs = await db.fetch_interactions(user_id, session_id)
            tail = [r for r in recs if getattr(r, "role", "") in ("user", "assistant")][-6:]
            context = "\n".join(
                f"{getattr(r, 'role', '')}: {(getattr(r, 'content', '') or '')[:500]}"
                for r in tail
            )
        except Exception:
            context = ""

    plan = await plan_image_attachments(
        agent_id=agent_id, user_id=user_id, image_atts=image_atts,
        caps=caps, routing=routing, context=context, request=message or "",
    )
    mode = plan.get("mode")
    if mode == "inline":
        return  # a turn model can see the image natively → leave it inlined

    # From here the image is NOT inlined to the (blind) brain; other types pass through.
    out["inline_docs"] = [a for a in (attachment_docs or [])
                          if (a.get("mime_type") or "").lower() not in _VISION_INLINE_MIMES]
    parts = [message] if message else []
    guidance = plan.get("guidance") or ""

    if mode == "unreadable":
        # The directive is an instruction to the AGENT, not content the user wrote.
        # Surface it as an inspectable tool row (foldable, like process_image) and
        # deliver it to the model for THIS turn via message_text — but DO NOT write
        # it into the user's interaction row, so it never shows in the chat bubble.
        names = ", ".join(a.get("original_name", "image") for a in image_atts) or "an image"
        reason = plan.get("reason") or "unreadable"
        yield {"type": "tool_call", "level": "agent", "tool": "route_attachment",
               "args": {"attachment": names, "decision": "unreadable", "reason": reason},
               "turn": 1}
        yield {"type": "tool_result", "level": "agent", "tool": "route_attachment",
               "result": guidance or "(no guidance)", "error": True, "turn": 1}
        # Persist the route decision as an inspectable tool row (same rationale as
        # the process_image row below) so this "unreadable" tool call survives a
        # reload instead of being a live-only event. tool_call_id stays None →
        # dropped from model history (the guidance reaches the model this turn via
        # message_text, and is intentionally NOT folded into the user turn).
        try:
            await db.insert_interaction(
                user_id, session_id, role="tool",
                content=guidance or "(no guidance)",
                parent_id=user_interaction_id,
                tool_name="route_attachment",
                channel="web_portal",
                metadata=json.dumps({"attachment": names, "decision": "unreadable",
                                     "reason": reason, "error": True}),
                input_data=json.dumps({"attachment": names, "decision": "unreadable",
                                       "reason": reason}),
                output_data=guidance or "",
                sender_id=agent_id or None,
                receiver_id=agent_id or None,
            )
        except Exception as e:
            logger.debug("attachment route: route_attachment row persist failed: %s", e)
        if guidance:
            parts.append(f"\n\n{guidance}")
        out["message_text"] = "".join(parts).strip() or (message or "")
        yield {"type": "pipeline", "level": "pipeline", "step": "attachment_describe_end",
               "image_count": len(image_atts), "status": "unreadable", "reason": reason}
        return

    # mode == "describe"
    describer = plan.get("describer") or {}
    import time as _t
    from datetime import datetime, timezone
    yield {"type": "pipeline", "level": "pipeline", "step": "attachment_describe_start",
           "image_count": len(image_atts), "vision_model": describer.get("model", "")}
    _start = _t.time()
    described = 0
    cached = 0
    for a in image_atts:
        name = a.get("original_name", "image")
        meta = a.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        # Surface the vision-worker call as an inspectable tool call so the user
        # can expand it to see the EXACT prompt we generated and the worker's
        # response. Tagged turn=1 so it attaches to this exchange's reply bubble.
        tool_args = {
            "attachment": name,
            "vision_model": describer.get("model", ""),
            "system_prompt": plan.get("worker_system") or "(built-in default vision-describe prompt)",
            "instruction": plan.get("worker_instruction") or "Describe this image in detail.",
            "user_message": message or "",
        }
        yield {"type": "tool_call", "level": "agent", "tool": "process_image",
               "args": tool_args, "turn": 1}
        _img_start = _t.time()
        desc = None
        was_cached = False
        if isinstance(meta, dict) and meta.get("vision_description") \
                and meta.get("vision_describer_model") == describer.get("model"):
            desc = meta.get("vision_description")
            cached += 1
            was_cached = True
        if not desc:
            desc = await describe_image_attachment(
                a, describer, user_text_hint=message,
                system_prompt=plan.get("worker_system") or None,
                instruction=plan.get("worker_instruction"),
            )
            if desc:
                described += 1
                try:
                    await db.update_attachment_metadata(a.get("id"), {
                        "vision_description": desc,
                        "vision_describer_model": describer.get("model", ""),
                        "vision_described_at": datetime.now(timezone.utc).isoformat(),
                    })
                except Exception as e:
                    logger.debug("attachment route: metadata cache failed: %s", e)
        _img_ms = int((_t.time() - _img_start) * 1000)
        yield {"type": "tool_result", "level": "agent", "tool": "process_image",
               "result": desc or "(the vision model could not describe this image)",
               "duration_ms": _img_ms,
               "error": not bool(desc), "turn": 1}
        # Persist the vision-describe step as an inspectable tool interaction —
        # mirrors how memory_search is recorded (role=tool, parent=the user turn).
        # Without this the foldable process_image row is a LIVE-ONLY event that
        # vanishes on reload, and the description was only recoverable from the
        # text folded into the user message (which then wrongly rendered inside
        # the user's chat bubble). tool_call_id stays None so the model-history
        # builder drops this synthetic row (a real model-issued process_image call
        # has a tool_call_id and is kept); the model still receives the
        # description via the fold persisted on the user turn below.
        try:
            await db.insert_interaction(
                user_id, session_id, role="tool",
                content=desc or "(the vision model could not describe this image)",
                parent_id=user_interaction_id,
                tool_name="process_image",
                channel="web_portal",
                metadata=json.dumps({
                    "attachment": name,
                    "vision_model": describer.get("model", ""),
                    "cached": was_cached,
                    "error": not bool(desc),
                    "duration_ms": _img_ms,
                }),
                input_data=json.dumps(tool_args),
                output_data=desc or "",
                sender_id=agent_id or None,
                receiver_id=agent_id or None,
            )
        except Exception as e:
            logger.debug("attachment route: process_image row persist failed: %s", e)
        if desc:
            parts.append(f"\n\n[Attached image — '{name}']:\n{desc}")
        else:
            parts.append(f"\n\n[Attached image — '{name}']:\n(Image could not be described.)")

    # Persist the user turn with the image DESCRIPTION folded in (so the model keeps
    # it across turns) but NOT the guidance NOTE — the note is an instruction to the
    # agent, not user content, so it stays out of the chat bubble. The model still
    # receives the note this turn via message_text below.
    persisted = "".join(parts).strip() or (message or "")
    try:
        await db.update_interaction_content(user_interaction_id, persisted)
    except Exception as e:
        logger.debug("attachment route: persist failed: %s", e)
    if guidance:
        parts.append(f"\n\n{guidance}")
    out["message_text"] = "".join(parts).strip() or (message or "")

    yield {"type": "pipeline", "level": "pipeline", "step": "attachment_describe_end",
           "image_count": len(image_atts), "vision_model": describer.get("model", ""),
           "described": described, "cached": cached,
           "duration_ms": int((_t.time() - _start) * 1000),
           "status": "ok" if (described or cached) else "partial"}


# Default fold/chip wording if the catalog has no app_control_point.template.
# Mirrors data/config + app/defaults app-prompts.json; both the model fold and the
# foldable chip's readable summary are rendered from this exact text.
_APP_CONTROL_FALLBACK_TEMPLATE = (
    '[App Control · {intent} · pointing at "{label}" ({descriptor}) in the {region} '
    'of the {page} page, at x={x}, y={y}. Locator: {selector}. Current style: {styles}. '
    'Markup: {html}]: {text}'
)


def _app_control_template() -> str:
    """The App Control hand-off wording — backend-adjustable via app-prompts.json
    (ui_handoffs.app_control_point.template), no UI. Falls back to the built-in
    default when the catalog is unreachable or omits it."""
    try:
        from app.util.paths import app_prompts_path
        data = json.loads(app_prompts_path().read_text(encoding="utf-8"))
        tpl = ((data.get("ui_handoffs") or {}).get("app_control_point") or {}).get("template")
        if isinstance(tpl, str) and tpl.strip():
            return tpl
    except Exception:
        pass
    return _APP_CONTROL_FALLBACK_TEMPLATE


def _format_app_control(tpl: str, fields: Dict[str, Any]) -> str:
    import re
    return re.sub(r"\{(\w+)\}", lambda m: str(fields.get(m.group(1), m.group(0))), tpl)


async def _maybe_emit_app_control(db, request, user_interaction_id, desc_out,
                                  agent_id="", session_id="", channel="web_portal"):
    """App Control point-and-share hand-off (async generator).

    When the user points at a UI element and sends from the App Control panel, the
    frontend forwards a small *fingerprint* (request.app_control): the human label,
    the element's role, the page + region it sits in, a CSS locator, a slice of its
    computed style + markup, the cursor x/y, and the words the user typed. We:
      • emit a foldable ``app_control`` tool call (LIVE render — same machinery as
        process_image, see chat-activity.js _SYNTH_TOOLS) so the technical detail
        lives in its own chip instead of bloating the user's message bubble;
      • persist it as a display-only role=tool row (tool_call_id stays None → out of
        model history) so the chip survives a reload (see db_viewer _row_to_msg);
      • fold the fingerprint into the user message FOR THIS TURN ONLY (via
        desc_out["message_text"]) so the agent acts on exactly what was clicked. The
        fingerprint is NOT written into the user turn, so the visible chat bubble
        keeps only the words the user typed.
    """
    fp = getattr(request, "app_control", None)
    if not fp or not isinstance(fp, dict):
        return

    def _clip(v, n):
        s = "" if v is None else str(v)
        return s.replace("\r", " ").replace("\n", " ").strip()[:n]

    fields = {
        "intent": _clip(fp.get("intent"), 80) or "Point and ask",
        "label": _clip(fp.get("label"), 80) or "the page",
        "descriptor": _clip(fp.get("descriptor"), 40) or "area",
        "region": _clip(fp.get("region"), 60) or "main area",
        "page": _clip(fp.get("page"), 40) or "app",
        "selector": _clip(fp.get("selector"), 200) or "(unknown)",
        "styles": _clip(fp.get("styles"), 300) or "(unavailable)",
        "html": _clip(fp.get("html"), 400) or "(unavailable)",
        "x": _clip(fp.get("x"), 8),
        "y": _clip(fp.get("y"), 8),
        "text": _clip(fp.get("text"), 2000),
    }
    summary = _format_app_control(_app_control_template(), fields)

    # LIVE foldable chip (paired call + result); turn=1 sits it with this exchange.
    yield {"type": "tool_call", "level": "agent", "tool": "app_control",
           "args": fields, "turn": 1}
    yield {"type": "tool_result", "level": "agent", "tool": "app_control",
           "result": summary, "duration_ms": 0, "error": False, "turn": 1}

    # Persist as a display-only synthetic tool row (parented to the user turn, no
    # tool_call_id) so reload rebuilds the chip — mirrors the process_image row.
    try:
        await db.insert_interaction(
            request.user_id, session_id, role="tool",
            content=summary,
            parent_id=user_interaction_id,
            tool_name="app_control",
            channel=channel,
            metadata=json.dumps({"app_control": True, "region": fields["region"],
                                 "page": fields["page"], "error": False}),
            input_data=json.dumps(fields),
            output_data=summary,
            sender_id=agent_id or None,
            receiver_id=agent_id or None,
        )
    except Exception as e:
        logger.debug("app control: tool row persist failed: %s", e)

    # Deliver the fingerprint to the model THIS turn. The template already embeds
    # the user's words ({text}), so when no other fold is present the summary IS the
    # message; if an image description was folded in too, keep it and append context.
    base = desc_out.get("message_text", request.message) or ""
    words = request.message or ""
    if base.strip() == words.strip():
        desc_out["message_text"] = summary
    else:
        desc_out["message_text"] = (base + "\n\n" + summary).strip()


async def _emit_to_visualizers(session_id: str, event: Dict[str, Any], user_id: Optional[str] = None):
    """Push an event to all visualizer listeners for a session, and optionally user listeners.

    Side effect: if a RunBuffer is active for this session, the event is
    stamped with session_seq / turn_id / turn_seq / emit_time before broadcast,
    so reconnecting clients can replay events newer than their last seen seq.
    """
    import json
    # Tag the event with its originating session so per-USER WebSocket
    # subscribers (which receive events for ALL of the user's sessions) can
    # route it to the right session and NOT render it into whatever session
    # happens to be active. Without this, live events arrive untagged and the
    # frontend's session filter fails open. Set before stamp_event so the copy
    # stored in the RunBuffer (used for replay) carries it too.
    event["session_id"] = session_id
    # Stamp via the in-memory run buffer (if a turn is active for this session).
    # This mutates `event` to add session_seq / turn_id / turn_seq / emit_time.
    try:
        _reg = get_run_buffer_registry()
        _buf = _reg.get(session_id)
        if _buf is not None:
            _buf.stamp_event(event)
    except Exception as _be:
        logger.debug("RunBuffer stamp failed for session %s: %s", session_id, _be)

    # Durable in-flight-op snapshot: persist which tool is running so a refresh
    # can re-show the live "in-process" indicator (cleared when the tool finishes
    # or the run ends). Only tool events touch the DB — everything else is a no-op.
    _et = event.get("type")
    if _et in ("tool_call", "tool_result"):
        try:
            if _et == "tool_call":
                _tool = event.get("tool") or "tool"
                _op = json.dumps({
                    "tool": _tool,
                    "turn": event.get("turn"),
                    "note": "Toolcall " + _tool,
                })
            else:
                _op = None  # tool finished — back to a generic "thinking" state
            await get_db().run_state_set_op(session_id, _op)
        except Exception as _oe:
            logger.debug("run_state_set_op failed for session %s: %s", session_id, _oe)

    # Flight-recorder tap: keep interesting loop/tool events (pipeline problems,
    # tool errors) for post-hoc diagnosis. Cheap + swallows its own errors.
    try:
        from app.agent.diagnostics import tap_loop_event
        tap_loop_event(session_id, event)
    except Exception:
        pass

    listeners = _visualizer_listeners.get(session_id, [])
    disconnected = []
    for ws in listeners:
        try:
            await ws.send_text(json.dumps(event))
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        unregister_visualizer_listener(session_id, ws)
    # Also broadcast to per-user listeners if user_id provided
    if user_id:
        await _emit_to_user_listeners(user_id, event)


# ── User listener registry (per-user) ──
_user_listeners: Dict[str, List[Any]] = {}  # user_id → list of WebSocket objects


def register_user_listener(user_id: str, websocket: Any):
    """Register a WebSocket that receives events for all of a user's sessions."""
    if user_id not in _user_listeners:
        _user_listeners[user_id] = []
    _user_listeners[user_id].append(websocket)

def unregister_user_listener(user_id: str, websocket: Any):
    """Remove a WebSocket from the per-user listeners."""
    if user_id in _user_listeners:
        _user_listeners[user_id] = [
            ws for ws in _user_listeners[user_id] if ws is not websocket
        ]
        if not _user_listeners[user_id]:
            del _user_listeners[user_id]


async def _emit_to_user_listeners(user_id: str, event: Dict[str, Any]):
    """Push an event to all per-user listeners."""
    import json
    listeners = _user_listeners.get(user_id, [])
    disconnected = []
    for ws in listeners:
        try:
            await ws.send_text(json.dumps(event))
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        unregister_user_listener(user_id, ws)


async def notify_user(user_id: str, event: Dict[str, Any]) -> None:
    """Public, fire-and-forget broadcast of an out-of-band event to every live
    WebSocket a user has open (all tabs / devices). Thin wrapper over the
    per-user listener registry so other modules (agent CRUD endpoints, run-state
    bookkeeping) don't reach into the private emitter. Never raises — a delivery
    failure must not break the operation that triggered the notification."""
    if not user_id:
        return
    try:
        await _emit_to_user_listeners(user_id, event)
    except Exception:
        pass

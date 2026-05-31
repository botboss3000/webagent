"""
Unified supervised agent-run entry point + self-healing resume engine.

Why this exists
---------------
Only the interactive web-chat path used to record durable run-state
(``session_runs``) and run under the supervised ``RunManager``. Every background
path — scheduled automations, event triggers, inbound messages, webhooks — called
the agent loop directly, with no run-state row and no strong task reference. So a
server restart or a frozen await silently lost that work, and nobody re-ignited it.

This module is the single funnel that makes EVERY origin:
  1. **recorded** — a ``session_runs`` row with ``origin`` + a ``relaunch_ctx``
     recipe to rebuild the turn headlessly, and a ``stop_cause`` describing why it
     ended;
  2. **supervised** — owned by ``RunManager`` so a disconnecting client (or a
     dropped task reference) cannot kill it;
  3. **recoverable** — boot recovery and the liveness watchdog call
     :func:`resume_one` to re-ignite an *involuntary* stop (server_restart /
     zombie / frozen / crash) from durable conversation history, while NEVER
     resuming a voluntary one (user_stop / replaced) or a throwaway/opted-out run.

Resume happens at the **turn boundary**: the full conversation (including the
partial interrupted answer and the synthetic ``[interrupted — no result]`` tool
repairs from ``session_history``) is replayed with a continue-nudge, and the agent
continues. Mid-tool Python state is not snapshotted, so a side-effecting tool whose
result was never persisted MAY run again — the per-tool / per-agent opt-out is the
mitigation for that.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Tunables (Phase 3 wires these to app-settings.json; defaults until then) ──
DEFAULT_MAX_RESUME_ATTEMPTS = 3
RESUME_BACKOFF_BASE_SECONDS = 30.0
RESUME_BACKOFF_CAP_SECONDS = 1800.0
LEASE_SECONDS = 120.0
BOOT_RESUME_CONCURRENCY = 4

# Origins / sessions that must NEVER be auto-resumed (throwaway test sandboxes).
_EXCLUDED_ORIGINS = ("sandbox", "optimizer")
_THROWAWAY_PREFIXES = ("test-", "trial-")
_RESUMABLE_CAUSES = ("server_restart", "zombie", "frozen", "crash")

# The continue-instruction handed to a resumed turn. It is passed as the trailing
# user_message (which the loop does NOT persist), so it nudges this LLM call only
# and never pollutes the stored conversation.
RESUME_NUDGE = (
    "[SYSTEM] Your previous response was interrupted before it finished. Continue "
    "from where you left off based on the conversation so far — do not restart from "
    "scratch, and do not repeat work you have already completed. If your last action "
    "already finished the task, simply provide the final result."
)


@dataclass
class RunOutcome:
    """Terminal result of a supervised turn. ``stop_cause`` is the machine
    taxonomy used for resume decisions; ``reply`` is the final assistant text
    (needed by Mode-A callers that deliver it)."""
    status: str = "complete"            # complete | interrupted | error
    stop_cause: Optional[str] = None    # complete | crash | ... (None → leave existing)
    reply: Optional[str] = None
    error: Optional[str] = None


# ── Per-origin resume builders ────────────────────────────────────────────────
# Each entry knows how to REBUILD and run a turn for its origin from a stored
# relaunch_ctx. Registered at import time by the owning module (web by chat.py;
# the background origins default to the generic builder below).
ResumeBuilder = Callable[[Dict[str, Any], bool], Awaitable[RunOutcome]]
_RESUME_BUILDERS: Dict[str, ResumeBuilder] = {}


def register_resume_builder(origin: str, fn: ResumeBuilder) -> None:
    _RESUME_BUILDERS[origin] = fn


def get_resume_builder(origin: str) -> Optional[ResumeBuilder]:
    return _RESUME_BUILDERS.get(origin)


def _norm_outcome(x: Any) -> RunOutcome:
    if isinstance(x, RunOutcome):
        return x
    if x is None:
        return RunOutcome()
    if isinstance(x, str):
        return RunOutcome(reply=x)
    return RunOutcome()


def _cfg() -> Dict[str, Any]:
    """Self-healing tunables from app-settings.json (falls back to defaults)."""
    try:
        from app.admin.settings import get_self_heal_config
        return get_self_heal_config()
    except Exception:
        return {
            "max_resume_attempts": DEFAULT_MAX_RESUME_ATTEMPTS,
            "backoff_seconds": RESUME_BACKOFF_BASE_SECONDS,
        }


def _backoff_seconds(attempts: int) -> float:
    base = float(_cfg().get("backoff_seconds", RESUME_BACKOFF_BASE_SECONDS))
    return min(base * (2 ** max(0, attempts)), RESUME_BACKOFF_CAP_SECONDS)


def _effective_max(row: Dict[str, Any]) -> int:
    cap = row.get("max_resume_attempts")
    if cap:
        return int(cap)
    return int(_cfg().get("max_resume_attempts", DEFAULT_MAX_RESUME_ATTEMPTS))


def _nonresumable_tools() -> set:
    """Tools whose use holds a run for manual resume instead of auto-resuming.
    Empty by default (user choice: auto-resume with opt-out) — populated by an
    explicit ``no_auto_resume`` metadata flag and/or an app-settings list."""
    names: set = set()
    try:
        from app.tools.loader import BUILTIN_TOOL_METADATA
        names |= {n for n, m in BUILTIN_TOOL_METADATA.items() if m.get("no_auto_resume")}
    except Exception:
        pass
    return names


def _agent_opted_out(agent: Optional[Dict[str, Any]]) -> bool:
    """An agent explicitly flagged auto_resume=False is never auto-resumed."""
    if not agent:
        return False
    val = agent.get("auto_resume")
    return val is False or val == 0


def _parse_ctx(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(row.get("relaunch_ctx") or "{}") or {}
    except Exception:
        return {}


async def _current_agent_id(db, row: Dict[str, Any]) -> Optional[str]:
    """The session's CURRENT agent — which a mid-session delegation may have
    rebound (loop.py bind_session_to_agent). Resume must use this, not the
    agent captured when the run first began."""
    sid = row.get("session_id")
    try:
        aid = await db.get_session_agent_id(sid)
        if aid:
            return aid
    except Exception:
        pass
    return row.get("agent_id")


# ── Supervised launch ─────────────────────────────────────────────────────────

async def run_supervised_turn(
    *,
    session_id: str,
    user_id: str,
    agent_id: Optional[str],
    origin: str,
    build_turn: Callable[[bool], Awaitable[Any]],
    channel: Optional[str] = None,
    turn_id: Optional[str] = None,
    relaunch_ctx: Optional[Any] = None,
    max_resume_attempts: Optional[int] = None,
    fresh: bool = True,
    replace: bool = True,
    await_result: bool = False,
    result_timeout: Optional[float] = None,
) -> Optional[RunOutcome]:
    """Run one agent turn, recorded in ``session_runs`` and supervised by
    ``RunManager`` so no disconnecting client can kill it.

    ``build_turn(replaced)`` is the per-origin coroutine that actually executes
    the turn (build prompt → run loop → persist). It must NOT touch run-state;
    this function owns begin/finish.

    - ``fresh``  True for a brand-new turn (writes ``run_state_begin`` and resets
      the retry budget). False for a resume (the claim already flipped the row to
      running and bumped the attempt — begin would wrongly zero the budget).
    - ``replace`` True uses ``start_or_replace`` (a new message supersedes a live
      run — the web/inbound semantic). False uses ``start_run`` (a resume must
      yield to any live run, never clobber it).
    - ``await_result`` (Mode A) waits for and returns the ``RunOutcome`` so the
      caller can deliver the reply, while the work itself stays supervised and
      survives the caller leaving.
    """
    from app.db import get_db
    from app.agent.run_manager import get_run_manager

    db = get_db()
    rm = get_run_manager()
    rc_json = json.dumps(relaunch_ctx) if isinstance(relaunch_ctx, (dict, list)) else relaunch_ctx

    loop = asyncio.get_event_loop()
    fut: "asyncio.Future[RunOutcome]" = loop.create_future()

    def _resolve(outcome: RunOutcome) -> None:
        if not fut.done():
            fut.set_result(outcome)

    async def _do(replaced: bool) -> RunOutcome:
        if fresh:
            try:
                await db.run_state_begin(
                    session_id, user_id, agent_id, turn_id,
                    origin=origin, relaunch_ctx=rc_json,
                    max_resume_attempts=max_resume_attempts,
                )
            except Exception as e:  # noqa: BLE001
                logger.debug("run_state_begin failed for %s: %s", session_id[:12], e)
        outcome = RunOutcome()
        try:
            outcome = _norm_outcome(await build_turn(replaced))
        except asyncio.CancelledError:
            outcome = RunOutcome(status="interrupted")
            await _finish_run(db, session_id, outcome)
            _resolve(outcome)
            raise
        except Exception as e:  # noqa: BLE001 — supervisor records, never propagates a crash silently
            logger.error("supervised turn %s (origin=%s) failed: %s",
                         session_id[:12], origin, e, exc_info=True)
            outcome = RunOutcome(status="error", stop_cause="crash", error=str(e)[:500])
            await _finish_run(db, session_id, outcome)
            _resolve(outcome)
            return outcome
        await _finish_run(db, session_id, outcome)
        _resolve(outcome)
        return outcome

    if replace:
        await rm.start_or_replace(
            session_id=session_id, user_id=user_id, turn_id=turn_id, db=db,
            run_factory=lambda replaced: _do(replaced),
        )
    else:
        started = await rm.start_run(
            session_id=session_id, user_id=user_id, turn_id=turn_id,
            run_factory=lambda: _do(False),
        )
        if not started:
            # A live run already owns this session (a user message, or another
            # resume that won the claim). Do not clobber it.
            logger.info("supervised turn %s not started — already running", session_id[:12])
            _resolve(RunOutcome(status="running"))

    if await_result:
        try:
            if result_timeout:
                return await asyncio.wait_for(asyncio.shield(fut), timeout=result_timeout)
            return await fut
        except asyncio.TimeoutError:
            logger.info("await_result timed out for %s; run continues supervised", session_id[:12])
            return RunOutcome(status="running")
        except Exception:  # noqa: BLE001
            return RunOutcome(status="error", stop_cause="crash")
    return None


async def _finish_run(db, session_id: str, outcome: RunOutcome) -> None:
    """Flip run-state to its terminal status + derived cause. Voluntary causes
    (user_stop / replaced) are protected inside ``run_state_finish``."""
    cause = outcome.stop_cause
    if cause is None and outcome.status == "complete":
        cause = "complete"
    try:
        await db.run_state_finish(session_id, status=outcome.status,
                                  error=outcome.error, stop_cause=cause)
    except Exception as e:  # noqa: BLE001
        logger.debug("run_state_finish failed for %s: %s", session_id[:12], e)


# ── Resume orchestration ──────────────────────────────────────────────────────

async def _classify_resume(db, row: Dict[str, Any]) -> Tuple[str, str]:
    """Decide what to do with a stopped run: 'ok' (resume), 'manual' (hold for a
    human one-click), 'failed' (terminal), or 'skip' (not ours / not resumable)."""
    sid = row.get("session_id") or ""
    origin = row.get("origin") or "web"
    if origin in _EXCLUDED_ORIGINS:
        return ("skip", "excluded origin")
    if sid.startswith(_THROWAWAY_PREFIXES):
        return ("skip", "throwaway session")
    cause = row.get("stop_cause")
    if cause not in _RESUMABLE_CAUSES:
        return ("skip", f"cause '{cause}' not resumable")

    # Per-agent opt-out (uses the session's CURRENT agent after any delegation).
    agent_id = await _current_agent_id(db, row)
    if agent_id:
        try:
            agent = await db.get_agent_by_id(agent_id)
        except Exception:
            agent = None
        if _agent_opted_out(agent):
            return ("manual", "agent opted out of auto-resume")

    # Per-tool opt-out — a turn that touched a non-resumable tool waits for a human.
    blockset = _nonresumable_tools()
    if blockset:
        try:
            used = set(await db.run_state_session_tool_names(sid))
        except Exception:
            used = set()
        hit = used & blockset
        if hit:
            return ("manual", f"used non-resumable tool(s): {', '.join(sorted(hit))}")

    return ("ok", "")


async def resume_one(session_id: str, *, reason: Optional[str] = None) -> bool:
    """Attempt to re-ignite a single stopped run. Returns True if a resume turn
    was launched. Enforces eligibility, the retry budget + backoff, the opt-out,
    and an atomic lease so a live run is never double-started."""
    from app.db import get_db
    from app.agent.run_manager import get_run_manager

    db = get_db()
    rm = get_run_manager()

    row = await db.run_state_get(session_id)
    if not row:
        return False
    if rm.is_running(session_id):
        return False  # already alive in this process — nothing to heal

    decision, why = await _classify_resume(db, row)
    if decision == "skip":
        return False
    if decision == "manual":
        await db.run_state_mark_manual(session_id, why)
        logger.info("Holding run %s for manual resume: %s", session_id[:12], why)
        return False
    if decision == "failed":
        await db.run_state_mark_failed(session_id, why)
        return False

    effective_max = _effective_max(row)
    attempts = int(row.get("resume_attempts") or 0)
    if attempts >= effective_max:
        await db.run_state_mark_failed(session_id, f"exhausted {effective_max} resume attempts")
        logger.warning("Run %s exhausted %d resume attempts — marked failed",
                       session_id[:12], effective_max)
        return False

    origin = row.get("origin") or "web"
    builder = get_resume_builder(origin)
    if builder is None:
        await db.run_state_mark_manual(session_id, f"no resume builder for origin '{origin}'")
        logger.warning("No resume builder for origin '%s' (session %s)", origin, session_id[:12])
        return False

    # Atomic claim: flips to running, bumps the attempt, takes the lease, and sets
    # the NEXT backoff gate. Fails (returns False) if it raced another worker or
    # the run came alive.
    token = uuid.uuid4().hex
    claimed = await db.run_state_claim_for_resume(
        session_id, token, LEASE_SECONDS, _backoff_seconds(attempts), effective_max,
    )
    if not claimed:
        return False

    rc = _parse_ctx(row)
    rc.setdefault("session_id", session_id)
    rc.setdefault("user_id", row.get("user_id"))
    rc.setdefault("agent_id", row.get("agent_id"))
    logger.info("Resuming run %s (origin=%s cause=%s attempt=%d/%d reason=%s)",
                session_id[:12], origin, row.get("stop_cause"),
                attempts + 1, effective_max, reason or "watchdog")

    await run_supervised_turn(
        session_id=session_id, user_id=row.get("user_id"), agent_id=row.get("agent_id"),
        origin=origin, channel=rc.get("channel"), turn_id=row.get("turn_id"),
        relaunch_ctx=rc, fresh=False, replace=False,
        build_turn=lambda replaced: builder(rc, True),
    )
    return True


async def manual_resume(session_id: str) -> bool:
    """Resume a run the user explicitly asked to continue (the one-click path for
    a run held as ``needs_manual_resume`` by the opt-out, or any stopped run).
    Bypasses the cause/opt-out gates (the user overrode them) but keeps the
    is-running and lease guards so a live run is never double-started. The retry
    budget is relaxed by one so a manual click always gets an attempt."""
    from app.db import get_db
    from app.agent.run_manager import get_run_manager

    db = get_db()
    rm = get_run_manager()
    row = await db.run_state_get(session_id)
    if not row:
        return False
    if rm.is_running(session_id):
        return False
    origin = row.get("origin") or "web"
    if origin in _EXCLUDED_ORIGINS or session_id.startswith(_THROWAWAY_PREFIXES):
        return False
    builder = get_resume_builder(origin)
    if builder is None:
        return False

    # A still-'running' row (e.g. flagged manual without finishing) must be flipped
    # terminal so the atomic claim can take it.
    if row.get("status") == "running":
        await db.run_state_finish(session_id, status="interrupted", stop_cause="crash")

    attempts = int(row.get("resume_attempts") or 0)
    token = uuid.uuid4().hex
    claimed = await db.run_state_claim_for_resume(
        session_id, token, LEASE_SECONDS, _backoff_seconds(attempts), attempts + 1,
    )
    if not claimed:
        return False

    rc = _parse_ctx(row)
    rc.setdefault("session_id", session_id)
    rc.setdefault("user_id", row.get("user_id"))
    rc.setdefault("agent_id", row.get("agent_id"))
    rc["resume_reason"] = "manual"
    logger.info("Manual resume of run %s (origin=%s attempt=%d)",
                session_id[:12], origin, attempts + 1)
    await run_supervised_turn(
        session_id=session_id, user_id=row.get("user_id"), agent_id=row.get("agent_id"),
        origin=origin, channel=rc.get("channel"), turn_id=row.get("turn_id"),
        relaunch_ctx=rc, fresh=False, replace=False,
        build_turn=lambda replaced: builder(rc, True),
    )
    return True


async def resume_orphans_at_boot(max_concurrent: int = BOOT_RESUME_CONCURRENCY) -> int:
    """Re-ignite every eligible run the previous process left behind. Called at
    startup AFTER scheduler/events are up. Throttled so boot does not stampede."""
    from app.db import get_db
    db = get_db()
    try:
        rows = await db.run_state_list_resumable()
    except Exception as e:  # noqa: BLE001
        logger.warning("resume_orphans_at_boot: list_resumable failed: %s", e)
        return 0
    if not rows:
        return 0

    sem = asyncio.Semaphore(max(1, max_concurrent))
    launched = 0

    async def _one(sid: str) -> None:
        nonlocal launched
        async with sem:
            try:
                if await resume_one(sid, reason="boot"):
                    launched += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("boot resume failed for %s: %s", sid, e)

    await asyncio.gather(*[_one(r["session_id"]) for r in rows])
    logger.info("Boot resume: re-ignited %d/%d resumable run(s)", launched, len(rows))
    return launched


# ── Generic background resume builder (automation / event / inbound / webhook) ──

async def _deliver_reply(channel: str, recipient: str, text: str) -> None:
    """Re-send a resumed background reply through its messaging channel (Mode A
    re-delivery, e.g. Telegram). Mirrors app/scheduler/executor.py _deliver."""
    try:
        from app.communications.manager import get_plugin_manager
        pm = get_plugin_manager()
        plugin = pm.get_plugin(channel)
        if plugin and getattr(plugin, "enabled", False) and recipient:
            await plugin.send_message(recipient, text)
    except Exception as e:  # noqa: BLE001
        logger.warning("resume re-delivery failed (%s → %s): %s", channel, recipient, e)


async def _generic_background_resume(rc: Dict[str, Any], replaced: bool) -> RunOutcome:
    """Rebuild and run a headless background turn from durable history. Used for
    automation / event / inbound / webhook origins (no UI attached)."""
    from app.db import get_db
    from app.agent.prompts import build_system_prompt
    from app.agent.session_history import build_openai_history_from_session
    from app.agent.loop import run_agent_loop_buffered

    db = get_db()
    sid = rc.get("session_id")
    user_id = rc.get("user_id")
    agent_id = await db.get_session_agent_id(sid) or rc.get("agent_id")
    agent = await db.get_agent_by_id(agent_id) if agent_id else None
    if not agent:
        return RunOutcome(status="error", stop_cause="failed",
                          error="agent not found for resume")

    # Re-check ability gating for automation/event (it may have been disabled).
    origin = rc.get("origin")
    if origin in ("automation", "event"):
        try:
            from app.admin.integrations import is_ability_enabled_for_agent
            ability = "automation" if origin == "automation" else "event"
            if not await is_ability_enabled_for_agent(agent_id, ability):
                return RunOutcome(status="error", stop_cause="failed",
                                  error=f"{ability} ability disabled")
        except Exception:
            pass

    # Rebuild the system prompt the way the background executors do.
    try:
        resolved = await db.resolve_prompts(agent_id, user_id=user_id)
        context_docs = [
            {"id": s["slot_name"], "context_type": s["slot_name"],
             "title": s["slot_name"], "content": s["content"], "tags": []}
            for s in resolved
            if (s.get("content") or "").strip() and s.get("slot_name") not in ("automation",)
        ]
    except Exception:
        context_docs = []
    system_prompt = await build_system_prompt(
        context_docs, brain_context=None, user_id=user_id, agent_id=agent_id)

    history = await build_openai_history_from_session(db, user_id, sid)

    raw_allowed = agent.get("allowed_tools", [])
    if isinstance(raw_allowed, str):
        try:
            raw_allowed = json.loads(raw_allowed)
        except Exception:
            raw_allowed = []

    reply = await run_agent_loop_buffered(
        user_id=user_id, session_id=sid, user_message=RESUME_NUDGE,
        system_prompt=system_prompt, agent_id=agent_id, history=history,
        channel=rc.get("channel"), db=db, agent_template_id=agent.get("template_id"),
        allowed_tools=raw_allowed or None, max_turns=agent.get("max_turn_count", 0),
        timeout_seconds=rc.get("timeout_seconds") or 600,
    )

    # Mode-A re-delivery for inbound/webhook (the original caller is long gone).
    delivery = rc.get("delivery") or {}
    ch = delivery.get("channel")
    rcpt = delivery.get("recipient")
    if (ch and ch not in ("webchat", "silent") and not delivery.get("silent")
            and rcpt and reply):
        await _deliver_reply(ch, rcpt, reply)

    return RunOutcome(status="complete", stop_cause="complete", reply=reply)


# Default registrations for the headless background origins. The web origin
# registers its own (UI-emitting) builder from app/api/chat.py at import time.
for _bg_origin in ("automation", "event", "inbound", "webhook"):
    register_resume_builder(_bg_origin, _generic_background_resume)

"""App-wide cap on concurrently active sessions, with a FIFO queue.

When ``max_active_sessions`` (app-settings.json) is 0 (the default) the gate is
disabled and every session runs immediately — exactly the historical behaviour.
When the cap is a positive N, at most N sessions may be actively running a turn
at the same time. A session that tries to start a run while the cap is reached
waits in a first-come-first-served queue and begins only after an active session
completes (a "new session starts only after a prior session completed").

Why this lives in-process (single asyncio queue) rather than in the DB
---------------------------------------------------------------------
The cap limits CONCURRENCY, which is a property of this process's event loop.
The durable ``session_runs`` table already records what is *running* for cold
devices / the watchdog; this gate only orders who may start next. On a server
restart the boot cleanup (``cleanup_orphaned_runs``) flips stale rows to a
terminal state, so no slot bookkeeping survives a restart — matching the
existing in-process patterns (e.g. ``app/agent/turn_prewarm.py``).

Rules
-----
- Each SESSION holds at most one slot, and only while one of its turns is
  actually executing. Sending a new message mid-run (replace) re-enters the
  same session's slot without queuing again.
- ``acquire`` / ``release`` are idempotent and safe to call for a session that
  never acquired (release) or that already holds / already waits (acquire).
- A waiter whose task is cancelled mid-wait is removed from the queue, and any
  slot already granted to it is passed on to the next waiter.
- Gate failures fail OPEN: a broken gate must never block chat, so callers
  catch acquire errors and run anyway (release then no-ops).
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# session_id -> asyncio.Event, kept in FIFO (insertion) order
_waiters: "OrderedDict[str, asyncio.Event]" = OrderedDict()
# session_ids currently holding a slot (a turn is running)
_holders: "set[str]" = set()
_lock = asyncio.Lock()

# Callbacks invoked (fire-and-forget via create_task) when a session enters the
# queue. Each receives (session_id, position_in_queue, total_queued).
_on_queue_enter: List[Callable[[str, int, int], Awaitable[Any]]] = []


def _cap() -> int:
    """The configured cap, read live from app-settings.json. 0 = unlimited."""
    try:
        from app.admin.settings import get_max_active_sessions
        return get_max_active_sessions()
    except Exception:  # noqa: BLE001 — fail open on config errors
        return 0


def _fire_queue_enter(session_id: str, position: int, total: int) -> None:
    """Fire all registered queue-enter callbacks as fire-and-forget tasks."""
    if not _on_queue_enter:
        return
    for cb in _on_queue_enter:
        try:
            asyncio.create_task(cb(session_id, position, total))
        except Exception:
            pass


def _fire_queue_positions_locked() -> None:
    """Re-broadcast the current (position, total) to every remaining waiter.

    Must be called with ``_lock`` held, after the queue changed shape — a
    session was granted a slot, force-removed, or cancelled. Each still-queued
    session's callback is still registered until its ``acquire()`` returns, so
    the re-fire reaches exactly the sessions still waiting and lets the
    frontend move them up immediately instead of showing stale queue info
    until the next poll.
    """
    if not _on_queue_enter or not _waiters:
        return
    total = len(_waiters)
    for i, sid in enumerate(_waiters, start=1):
        for cb in _on_queue_enter:
            try:
                asyncio.create_task(cb(sid, i, total))
            except Exception:
                pass


def _wake_next_locked() -> None:
    """Grant a freed slot to the longest-waiting queued session (FIFO)."""
    while _waiters:
        sid, ev = _waiters.popitem(last=False)
        if ev.is_set():
            continue  # stale entry whose task is unwinding — skip it
        _holders.add(sid)
        ev.set()
        # Everyone still waiting moved up one position — re-broadcast so the
        # frontend's dropdown/bubble queue info stays current.
        _fire_queue_positions_locked()
        return


async def acquire(session_id: str) -> bool:
    """Wait until ``session_id`` may start a run, then return True.

    Returns immediately when the gate is disabled (cap 0), when the session
    already holds a slot (replace / resume of its own run), or when a slot is
    free. Otherwise the session is queued (FIFO) and this coroutine waits until
    a slot frees. The session is counted as active from the moment a slot is
    granted, so ``release`` must be called when its turn finishes.
    """
    cap = _cap()
    if cap <= 0:
        return True
    async with _lock:
        if session_id in _holders:
            return True  # already counted — this is a replace/resume of its own run
        if session_id in _waiters:
            ev = _waiters[session_id]  # already queued — wait on the same entry
        elif len(_holders) < cap:
            _holders.add(session_id)
            return True
        else:
            ev = asyncio.Event()
            _waiters[session_id] = ev
            # Fire queue-enter callbacks outside the lock's synchronous path
            pos = len(_waiters)
            _fire_queue_enter(session_id, pos, pos)
    try:
        await ev.wait()
    except asyncio.CancelledError:
        async with _lock:
            # If we were still queued, drop the queue entry. If a slot had just
            # been granted (we were moved to _holders), free it for the next
            # waiter — otherwise the slot would be lost.
            if _waiters.pop(session_id, None) is None:
                _holders.discard(session_id)
            _wake_next_locked()
        raise
    return True


async def force_acquire(session_id: str) -> bool:
    """Immediately grant ``session_id`` a run slot, bypassing the cap.

    If the session is currently queued, it is pulled out of the queue and
    granted a slot now (any waiters behind it move up one). If it already
    holds a slot this is a no-op. Returns True if the session holds a slot
    afterwards.

    This is the "force run" escape hatch a user can click on a queued
    message: the run starts immediately, temporarily running over the cap.
    The slot is still released normally when the turn finishes, so the cap
    re-applies from the next turn.
    """
    async with _lock:
        if session_id in _holders:
            return True
        ev = _waiters.pop(session_id, None)
        _holders.add(session_id)
        if ev is not None:
            ev.set()
            # The forced session is out of the queue — every waiter behind it
            # moved up one. Re-broadcast so their queue info is not stale.
            _fire_queue_positions_locked()
        return True


async def release(session_id: str) -> None:
    """Free ``session_id``'s slot (if it holds one) and grant it to the next
    queued session. Safe to call for a session that never acquired."""
    async with _lock:
        if session_id not in _holders and session_id not in _waiters:
            return
        _holders.discard(session_id)
        _wake_next_locked()


def stats() -> Tuple[int, int]:
    """Current (active, queued) session counts — for observability."""
    return len(_holders), len(_waiters)


def queued_position(session_id: str) -> Optional[int]:
    """1-based position of a queued session, or None if not queued."""
    if session_id not in _waiters:
        return None
    for i, sid in enumerate(_waiters, start=1):
        if sid == session_id:
            return i
    return None


def queue_info(session_id: str) -> Optional[Dict[str, int]]:
    """Return {position, total} for a queued session, or None if not queued.
    ``position`` is 1-based; ``total`` is the number of sessions in the queue
    (not including holders)."""
    pos = queued_position(session_id)
    if pos is None:
        return None
    return {"position": pos, "total": len(_waiters)}


def register_queue_callback(cb: Callable[[str, int, int], Awaitable[Any]]) -> None:
    """Register an async callback invoked (fire-and-forget) when a session
    enters the queue. Receives (session_id, position, total_queued)."""
    if cb not in _on_queue_enter:
        _on_queue_enter.append(cb)


def unregister_queue_callback(cb: Callable[[str, int, int], Awaitable[Any]]) -> None:
    """Remove a previously registered queue-enter callback."""
    try:
        _on_queue_enter.remove(cb)
    except ValueError:
        pass

"""
Run Manager — the server-side owner of every agent turn.

Why this exists
---------------
Before this, an agent turn was launched as a detached ``asyncio.create_task``
whose handle was never stored. Python is free to garbage-collect a task with no
live reference, which silently cancels it mid-run. Combined with the buffered
chat endpoint (which ran the loop *inline* in the request and died the instant
the client disconnected), that is why turning off a device, closing the browser,
or switching sessions could interrupt a run.

The Run Manager fixes this by holding a **strong, supervised reference** to each
running turn, keyed by ``session_id``. The run's lifetime is therefore tied to
the *server*, not to any HTTP/WebSocket connection. Nothing a client does
(leaving, refreshing, closing, switching devices) can reach it. The only things
that end a run are: it finishes, an explicit interrupt, or a server restart
(handled by orphan cleanup at boot — see ``StorageBackend.cleanup_orphaned_runs``).

Contract
--------
- ``start_run`` takes a zero-arg *factory* that returns the coroutine doing the
  actual turn (context build → agent loop → emit/persist). The manager wraps it,
  records run-state in the DB, and supervises completion.
- ``interrupt`` sets the DB interrupt flag the loop already polls, so the turn
  stops gracefully and persists its partial answer as 'interrupted'.
- ``is_running`` / ``active_sessions`` answer "is a run live right now?" from RAM
  (the DB ``session_runs`` table is the cross-process / cold-device source).
"""

import asyncio
import logging
import time
from typing import Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class _RunHandle:
    __slots__ = ("session_id", "user_id", "turn_id", "task", "started_at")

    def __init__(self, session_id: str, user_id: str, turn_id: Optional[str]):
        self.session_id = session_id
        self.user_id = user_id
        self.turn_id = turn_id
        self.task: Optional[asyncio.Task] = None
        self.started_at = time.time()


class RunManager:
    """Global registry of supervised, connection-independent agent runs."""

    # How long to let an interrupted run stop gracefully (so it persists its
    # partial answer at a clean boundary) before hard-cancelling its task.
    REPLACE_GRACE_SECONDS = 1.2

    def __init__(self) -> None:
        self._runs: Dict[str, _RunHandle] = {}
        self._lock = asyncio.Lock()
        self._session_locks: Dict[str, asyncio.Lock] = {}

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        lk = self._session_locks.get(session_id)
        if lk is None:
            lk = asyncio.Lock()
            self._session_locks[session_id] = lk
        return lk

    # ── Queries ──────────────────────────────────────────────────────────

    def is_running(self, session_id: str) -> bool:
        h = self._runs.get(session_id)
        return bool(h and h.task and not h.task.done())

    def active_sessions(self, user_id: Optional[str] = None) -> List[str]:
        out: List[str] = []
        for sid, h in self._runs.items():
            if h.task and not h.task.done():
                if user_id is None or h.user_id == user_id:
                    out.append(sid)
        return out

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start_run(
        self,
        *,
        session_id: str,
        user_id: str,
        turn_id: Optional[str],
        run_factory: Callable[[], Awaitable[None]],
    ) -> bool:
        """Start a supervised background run for ``session_id``.

        Returns False (without starting anything) if a run is already live for
        this session — a session has at most one active turn at a time.

        ``run_factory`` is a zero-arg callable returning the coroutine that does
        the whole turn. It owns all emit/persist work; the manager only supervises
        its lifetime so it cannot be killed by a disconnecting client.
        """
        async with self._lock:
            existing = self._runs.get(session_id)
            if existing and existing.task and not existing.task.done():
                logger.info("start_run rejected — run already active for session %s", session_id[:12])
                return False

            handle = _RunHandle(session_id, user_id, turn_id)

            async def _supervised() -> None:
                try:
                    await run_factory()
                except asyncio.CancelledError:
                    logger.info("Run for session %s was cancelled", session_id[:12])
                    raise
                except Exception as e:  # noqa: BLE001 — supervisor must not propagate
                    logger.error("Run for session %s failed: %s", session_id[:12], e, exc_info=True)

            # Strong reference held in self._runs — this is the whole point.
            handle.task = asyncio.create_task(_supervised(), name=f"agent-run:{session_id}")
            handle.task.add_done_callback(lambda t, sid=session_id: self._on_done(sid, t))
            self._runs[session_id] = handle
            logger.info("Started supervised run for session %s (turn %s)", session_id[:12], str(turn_id)[:12])
            return True

    async def start_or_replace(
        self,
        *,
        session_id: str,
        user_id: str,
        turn_id: Optional[str],
        db,
        run_factory: Callable[[bool], Awaitable[None]],
    ) -> str:
        """Start a run, OR — if one is already active for this session — force
        interrupt it and start the new one in its place.

        This is the entry point for a user sending a message: a new message
        always interrupts whatever the agent is doing so the agent reads it
        immediately and redoes its last step with the new context (the agent
        itself then decides whether the message means stop / steer / add info).

        ``run_factory(replaced)`` builds the turn coroutine; ``replaced`` is True
        when this turn is replacing an interrupted one (so the executor can tell
        the agent it was interrupted). Returns 'running' or 'replacing'.

        Serialized per session so rapid-fire messages can't start two runs at
        once. We wait (briefly) for the interrupted run to stop so its partial
        answer is persisted and run-state is finalized BEFORE the new turn
        begins — avoiding a race on the single session_runs row.
        """
        async with self._session_lock(session_id):
            replaced = self.is_running(session_id)
            if replaced:
                await self._interrupt_and_wait(session_id, db)
            await self.start_run(
                session_id=session_id, user_id=user_id, turn_id=turn_id,
                run_factory=lambda: run_factory(replaced),
            )
            return "replacing" if replaced else "running"

    async def _interrupt_and_wait(self, session_id: str, db) -> None:
        """Interrupt the active run and wait for it to fully stop (its finally
        runs run_state_finish + end_turn). Graceful first, then hard-cancel.

        Tags the run 'replaced' so the self-healing layer never auto-resumes it:
        a newer user message has superseded it, and the replacement turn already
        carries the interrupted context forward."""
        handle = self._runs.get(session_id)
        task = handle.task if handle else None
        try:
            await db.run_state_set_cause(session_id, "replaced")
        except Exception as e:
            logger.debug("interrupt: set_cause(replaced) failed for %s: %s", session_id[:12], e)
        try:
            await db.set_interrupt(session_id)
        except Exception as e:
            logger.warning("interrupt: set_interrupt failed for %s: %s", session_id[:12], e)
        if task and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self.REPLACE_GRACE_SECONDS)
            except asyncio.TimeoutError:
                logger.info("Run for %s didn't stop in %.1fs — hard-cancelling",
                            session_id[:12], self.REPLACE_GRACE_SECONDS)
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
            except BaseException:
                pass
        # Clear the flag so the replacement run doesn't immediately self-interrupt.
        try:
            await db.clear_interrupt(session_id)
        except Exception:
            pass

    def _on_done(self, session_id: str, task: "asyncio.Task") -> None:
        # Only drop if this is still the current handle for the session (a new
        # turn may have replaced it).
        h = self._runs.get(session_id)
        if h and h.task is task:
            self._runs.pop(session_id, None)

    async def interrupt(self, session_id: str, db, cause: str = "user_stop") -> bool:
        """Request a graceful stop. Sets the DB interrupt flag the loop polls;
        the turn finalizes its partial answer and run-state as 'interrupted'.
        Returns True if a run was live to interrupt.

        ``cause`` records intent so the self-healing layer treats it correctly.
        The default 'user_stop' (the Stop button) is NEVER auto-resumed."""
        was_running = self.is_running(session_id)
        try:
            await db.run_state_set_cause(session_id, cause)
        except Exception as e:
            logger.debug("interrupt: set_cause(%s) failed for %s: %s", cause, session_id[:12], e)
        try:
            await db.set_interrupt(session_id)
        except Exception as e:
            logger.warning("interrupt: set_interrupt failed for %s: %s", session_id[:12], e)
        return was_running

    async def cancel(self, session_id: str) -> bool:
        """Hard-cancel the live task for a session and wait for it to unwind.
        Used by the liveness watchdog on a FROZEN run (stuck inside an await the
        cooperative interrupt poll can't reach). Returns True if a task was
        cancelled. The caller is expected to have already tagged stop_cause so the
        run is reclassified for resume."""
        handle = self._runs.get(session_id)
        task = handle.task if handle else None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            return True
        return False


# ── Module-level singleton ──

_manager: Optional[RunManager] = None


def get_run_manager() -> RunManager:
    global _manager
    if _manager is None:
        _manager = RunManager()
    return _manager

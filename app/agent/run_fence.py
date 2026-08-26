"""Shared cancellation fence for parallel one-shot agent helpers.

The durable ``session_runs.turn_id`` is the run generation.  Manager, Closer,
and Starter work may outlive the coroutine that launched it, so every helper
captures the interaction's turn id and re-checks it immediately before a
side-effect.  A replacement writes a new turn id; user/global stops write a
non-resumable stop cause.  Either change makes an old helper harmless.
"""

from __future__ import annotations

from typing import Any, Optional


_TERMINAL_FENCE_CAUSES = {"user_stop", "replaced", "global_stop"}


def interaction_turn_id(db: Any, interaction_id: Optional[str]) -> Optional[str]:
    """Best-effort durable generation lookup for an interaction anchor."""
    if not interaction_id or not hasattr(db, "_get_conn"):
        return None
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT turn_id FROM interactions WHERE id = ? LIMIT 1",
            (interaction_id,),
        ).fetchone()
        return str(row["turn_id"]) if row and row["turn_id"] else None
    except Exception:
        return None
    finally:
        conn.close()


async def side_effects_allowed(
    db: Any,
    session_id: str,
    *,
    expected_turn_id: Optional[str] = None,
) -> bool:
    """Return whether a captured one-shot may still mutate or launch work.

    Missing run-state is tolerated for compatibility with throwaway/test
    backends.  An engaged application kill switch always fails closed.
    """
    try:
        from app.kill_switch import is_engaged
        if is_engaged():
            return False
    except Exception:
        # Import/read failures must not disable normal execution.
        pass

    getter = getattr(db, "run_state_get", None)
    if getter is None:
        return True
    try:
        row = await getter(session_id)
    except Exception:
        return True
    if not row:
        return True
    if str(row.get("stop_cause") or "").strip().lower() in _TERMINAL_FENCE_CAUSES:
        return False
    current_turn = str(row.get("turn_id") or "")
    if expected_turn_id and current_turn and current_turn != expected_turn_id:
        return False
    return True


def register_current_one_shot(session_id: str, turn_id: Optional[str]) -> None:
    """Register the current task with RunManager for Stop/Kill cancellation."""
    try:
        from app.agent.run_manager import get_run_manager
        get_run_manager().register_auxiliary(session_id, turn_id=turn_id)
    except Exception:
        # The durable fence remains authoritative if task registration is not
        # available (early boot, a test double, or event-loop teardown).
        pass

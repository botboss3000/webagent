"""Subagent orchestration core — the event-driven half of the mk2 manager.

This module is deliberately **Textual-free and side-effect-free** so it can be
unit-tested in isolation. It holds three things:

1. a **registry** of in-flight + finished subagents (each one runs in its own
   inspectable Store session, so the human can jump into it from /resume);
2. a **result buffer** of completed subagent outcomes waiting to be surfaced; and
3. the **delivery decision** logic that answers, on each completion, "what should
   the app do now?" — buffer silently, surface a synthetic message, and/or
   auto-trigger a new agent turn.

The Textual app owns the actual worker that *runs* each subagent and the loop
that *acts* on a decision; this module only decides. Everything here mutates on
the single asyncio event-loop thread (Textual async workers + the main loop all
run there), so no locking is needed.

Delivery modes
--------------
- ``gather``     — buffer the result; only surface (and auto-trigger) once EVERY
                   task sharing the same ``group_id`` has finished. A task with no
                   group_id is treated as a group of one (surfaces immediately).
- ``notify``     — surface this single result immediately and auto-trigger a turn
                   when the agent is idle.
- ``background`` — store the result only. Never surfaces, never triggers a turn.
                   The agent pulls it on demand with ``check_task`` and the app
                   shows a quiet transcript line. ("Pump and dump.")
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

# Terminal statuses — a task in either state counts as "done" for group-gather,
# so one erroring member can never hang the whole group forever.
RUNNING = "running"
COMPLETED = "completed"
ERROR = "error"
_TERMINAL = (COMPLETED, ERROR)

GATHER = "gather"
NOTIFY = "notify"
BACKGROUND = "background"
_MODES = (GATHER, NOTIFY, BACKGROUND)


@dataclass
class SubagentRecord:
    """One delegated subagent — its identity, scope, and live status."""

    task_id: str
    session_id: str            # the sub-session in the Store (inspectable in /resume)
    parent_session_id: str     # the MAIN session that spawned this subagent
    description: str
    tools: list[str]           # caller-scoped allowed tool names (the ONLY tools it gets)
    mode: str                  # gather | notify | background
    group_id: str = ""         # "" → standalone (a group of one)
    started_at: float = 0.0
    status: str = RUNNING      # running | completed | error
    result_summary: str = ""   # populated on completion (available to check_task)
    completed_at: float = 0.0

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL


@dataclass
class BufferedResult:
    """A finished subagent outcome awaiting delivery into the main conversation."""

    task_id: str
    session_id: str
    parent_session_id: str     # the MAIN session that spawned it
    description: str
    status: str
    summary: str
    group_id: str
    completed_at: float
    surfaced: bool = False     # the delivery decision fired (eligible for injection)
    injected: bool = False     # already written into the main session history


@dataclass
class DeliveryDecision:
    """What the app should do as a result of a completion (pure data, no effects).

    ``auto_trigger`` asks the app to start a follow-up turn when the agent is idle;
    if the agent is busy the app buffers and re-checks when the current turn
    settles. ``surfaced_task_ids`` are the buffered results this decision made
    eligible for injection (used only for logging/diagnostics).
    """

    auto_trigger: bool = False
    surfaced_task_ids: list[str] = field(default_factory=list)
    # Convenience flags for the app's transcript line:
    is_group: bool = False
    group_id: str = ""


class SubagentRegistry:
    def __init__(self) -> None:
        self._records: dict[str, SubagentRecord] = {}
        self._buffer: list[BufferedResult] = []

    # ── registration ───────────────────────────────────────────────────────
    def register(
        self,
        *,
        task_id: str,
        session_id: str,
        parent_session_id: str = "",
        description: str,
        tools: list[str],
        mode: str,
        group_id: str = "",
        now: Optional[float] = None,
    ) -> SubagentRecord:
        if mode not in _MODES:
            mode = GATHER
        rec = SubagentRecord(
            task_id=task_id,
            session_id=session_id,
            parent_session_id=parent_session_id,
            description=description,
            tools=list(tools or []),
            mode=mode,
            group_id=group_id or "",
            started_at=now if now is not None else time.time(),
        )
        self._records[task_id] = rec
        return rec

    # ── lookups ─────────────────────────────────────────────────────────────
    def get(self, task_id: str) -> Optional[SubagentRecord]:
        return self._records.get(task_id)

    def all(self) -> list[SubagentRecord]:
        return list(self._records.values())

    def running(self) -> list[SubagentRecord]:
        return [r for r in self._records.values() if r.status == RUNNING]

    def group_members(self, group_id: str) -> list[SubagentRecord]:
        return [r for r in self._records.values() if r.group_id == group_id]

    def group_complete(self, group_id: str) -> bool:
        """True when every member of a non-empty group has reached a terminal state."""
        members = self.group_members(group_id)
        return bool(members) and all(m.terminal for m in members)

    # ── completion + delivery decision ──────────────────────────────────────
    def complete(
        self,
        task_id: str,
        *,
        status: str = COMPLETED,
        summary: str = "",
        now: Optional[float] = None,
    ) -> DeliveryDecision:
        """Mark a subagent terminal and decide what the app should do next.

        Always records the summary on the registry record (so ``check_task`` can
        read it later, even for background tasks) and pushes a BufferedResult.
        Returns a :class:`DeliveryDecision`.
        """
        ts = now if now is not None else time.time()
        rec = self._records.get(task_id)
        if rec is None:
            # Unknown task (already reaped?) — nothing to deliver.
            return DeliveryDecision()
        rec.status = status if status in _TERMINAL else COMPLETED
        rec.result_summary = summary
        rec.completed_at = ts

        self._buffer.append(BufferedResult(
            task_id=rec.task_id,
            session_id=rec.session_id,
            parent_session_id=rec.parent_session_id,
            description=rec.description,
            status=rec.status,
            summary=summary,
            group_id=rec.group_id,
            completed_at=ts,
        ))

        if rec.mode == BACKGROUND:
            # Pump-and-dump: store only. Never surface, never trigger.
            return DeliveryDecision()

        if rec.mode == NOTIFY:
            self._surface([task_id])
            return DeliveryDecision(auto_trigger=True, surfaced_task_ids=[task_id])

        # GATHER -------------------------------------------------------------
        if rec.group_id:
            if not self.group_complete(rec.group_id):
                return DeliveryDecision()          # wait for the rest of the group
            ids = [m.task_id for m in self.group_members(rec.group_id)]
            self._surface(ids)
            return DeliveryDecision(auto_trigger=True, surfaced_task_ids=ids,
                                    is_group=True, group_id=rec.group_id)
        # Standalone gather behaves like a group of one.
        self._surface([task_id])
        return DeliveryDecision(auto_trigger=True, surfaced_task_ids=[task_id])

    def _surface(self, task_ids: list[str]) -> None:
        wanted = set(task_ids)
        for b in self._buffer:
            if b.task_id in wanted:
                b.surfaced = True

    # ── injection into the main conversation ────────────────────────────────
    def has_surfaced_pending(self, parent_session_id: str = "") -> bool:
        """Any results that have surfaced but not yet been injected into history?

        If ``parent_session_id`` is given, only results whose subagent has that
        parent session id are counted — so a running turn in one session never
        loops forever waiting for another session's subagent results.
        """
        if parent_session_id:
            return any(
                b.surfaced and not b.injected
                and b.parent_session_id == parent_session_id
                for b in self._buffer
            )
        return any(b.surfaced and not b.injected for b in self._buffer)

    def drain_surfaced(self, parent_session_id: str = "") -> list[BufferedResult]:
        """Return every surfaced-but-not-injected result and mark it injected.

        If ``parent_session_id`` is given, only results whose originating
        subagent has that parent session id are returned — so a running turn in
        one session never absorbs another session's subagent results.

        Called at the start of a turn: the returned items become synthetic
        user-role messages the agent sees as having "arrived."
        """
        ready = [b for b in self._buffer if b.surfaced and not b.injected]
        if parent_session_id:
            ready = [b for b in ready
                     if b.parent_session_id == parent_session_id]
        for b in ready:
            b.injected = True
        return ready

    # ── snapshots for the situation block / UI ──────────────────────────────
    def pending_count(self) -> int:
        return len(self.running())

    def describe_pending(self) -> str:
        """One-line-per-task summary of in-flight subagents for the situation block."""
        rows = self.running()
        if not rows:
            return ""
        out = []
        for r in rows:
            grp = f" [group: {r.group_id}]" if r.group_id else ""
            out.append(f"  - {r.task_id} ({r.mode}{grp}): {r.description}")
        return "Pending subagents (still running):\n" + "\n".join(out)


# ── synthetic-message formatting ────────────────────────────────────────────
def format_single(result: BufferedResult) -> str:
    """The synthetic user message for one delivered result (notify / standalone)."""
    tag = "completed" if result.status == COMPLETED else "ended with an error"
    return (
        f"[subagent {result.task_id} {tag}] Task: {result.description}\n"
        f"Result: {result.summary or '(no output)'}"
    )


def format_group(results: list[BufferedResult]) -> str:
    """The single synthetic message summarizing a whole gathered group."""
    if len(results) == 1:
        return format_single(results[0])
    group = results[0].group_id or "(none)"
    lines = [f"[subagent group '{group}' complete — {len(results)} tasks finished]"]
    for r in results:
        tag = "" if r.status == COMPLETED else " (ERROR)"
        lines.append(f"  - {r.task_id}{tag}: {r.description}\n      → {r.summary or '(no output)'}")
    return "\n".join(lines)


def format_delivery(results: list[BufferedResult]) -> str:
    """Turn a batch of drained results into one synthetic message.

    A batch that all shares one group_id is rendered as a group; a mixed batch
    (e.g. several independent notify results landed together) is concatenated.
    """
    if not results:
        return ""
    groups = {r.group_id for r in results}
    if len(groups) == 1 and next(iter(groups)):
        return format_group(results)
    return "\n\n".join(format_single(r) for r in results)

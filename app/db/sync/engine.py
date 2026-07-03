"""The sync engine — pusher (local outbox → remote) + puller (remote → local).

Scope: the SYNCED tier only (shared content/config). Money and coordination are
written straight to Postgres by the hybrid backend and never appear here. Conflict
policy is last-writer-wins on the table's ``updated_at`` watermark, which is
acceptable for config-like rows that are edited rarely and from one active device
at a time (handoff §8).

This engine is deliberately backend-agnostic: it moves rows through the portable
raw-client query builder (``select/in_/gt/order/upsert``), so it works whether the
remote is real Postgres or (in tests) a second SQLite file.

STATUS: the mechanics are exercised by temp/test_hybrid.py against a two-SQLite
harness. Wiring ``start()`` into the app lifespan and flipping the hybrid's Synced
writes to local-first+enqueue is the ACTIVATION step — it needs a real Postgres
remote to validate end-to-end and is intentionally left off by default.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.db.sync.outbox import Outbox

logger = logging.getLogger(__name__)


@dataclass
class SyncedSpec:
    """How one Synced-tier table is moved between local and remote.

    ``key`` is the conflict/identity column; ``watermark`` is the monotonic
    column the puller compares against (usually ``updated_at``); ``fat_cols`` are
    columns whose value is stripped to NULL on the remote skeleton when it exceeds
    ``strip_over`` chars (the full copy stays in the local hot store); a small
    value at/under the gate rides along inline. ``push_only`` marks a table the
    engine only pushes local→remote and never pulls back (used for the
    ``interactions`` transcript, which has no ``updated_at`` watermark and whose
    reads are served by the hybrid's warm-on-open mirror, not the puller)."""
    table: str
    key: str = "id"
    watermark: str = "updated_at"
    fat_cols: Tuple[str, ...] = ()
    strip_over: int = 0
    push_only: bool = False


# The Synced tier (handoff §6). Extend as more tables are moved local-first; each
# addition is data, not new control flow.
#
# ``interactions`` is the transcript (Stage 2): written local-first + pushed as a
# SKELETON — the fat ``input`` column over the split gate is stripped to NULL on
# the remote (the full copy stays local). It is push-only: the remote has no
# per-row ``updated_at`` to watermark on, and a second device gets the transcript
# through the hybrid's warm-on-open mirror instead of the puller.
#
# REMOTE-CONTROL CONTINUITY: only ``input`` is fat-stripped, NOT ``output``. The
# ``output`` column carries each assistant turn's tool_calls (see
# app/agent/session_history.py `_extract_tool_calls_from_output`) — it is what
# pairs an assistant's tool calls to their result rows when history is rebuilt on
# ANOTHER device (Remote Control takeover / session roaming). Stripping it broke
# takeover on tool-heavy turns: the remote skeleton kept the tool RESULT rows but
# lost the assistant's tool_calls, so a second device rebuilt an invalid history
# (a tool result with no preceding call) that the model API rejects. ``input`` is
# ~99% of the transcript's byte weight and is never read on rebuild, so it stays
# stripped; ``output`` (small) always rides along on the remote. Pushing ``input``
# in full also stalls the event loop (megabyte upserts run synchronously on it)
# and, when a row carries a stray NUL, wedges the queue in a permanent retry.
from app.db.hybrid import _FAT_THRESHOLD_CHARS  # noqa: E402  (source of the gate)

SYNCED_SPECS: Dict[str, SyncedSpec] = {
    "interactions": SyncedSpec(
        "interactions", fat_cols=("input",),
        strip_over=_FAT_THRESHOLD_CHARS, push_only=True),
    "sessions": SyncedSpec("sessions"),
    "session_summaries": SyncedSpec("session_summaries"),
    "session_summary_segments": SyncedSpec("session_summary_segments"),
    "genui": SyncedSpec("genui"),
    "webhook_registrations": SyncedSpec("webhook_registrations"),
    "agent_automations": SyncedSpec("agent_automations"),
    "agent_event_subscriptions": SyncedSpec("agent_event_subscriptions"),
}


class SyncEngine:
    """Drains the local outbox to the remote authority and pulls remote changes
    back into the local replica. Operates on a :class:`HybridBackend`'s two halves.
    """

    def __init__(self, hybrid, specs: Optional[Dict[str, SyncedSpec]] = None,
                 interval_seconds: float = 5.0, pull_enabled: bool = True) -> None:
        self._hb = hybrid
        self._local = hybrid.local
        self._remote = hybrid.remote
        self._specs = specs if specs is not None else SYNCED_SPECS
        self._outbox = Outbox(self._local)
        self._interval = interval_seconds
        # Remote Control live-stream cadence: while rows are actively flowing (a
        # live turn — most importantly one running on THIS device that a viewer on
        # ANOTHER device has open and is polling), the pusher runs at this faster
        # interval so the streaming assistant row reaches the shared DB in ~1s and
        # the viewer's reconcile poll renders it near-live instead of in 5s steps.
        # It relaxes back to _interval once the flow stops (see _run).
        self._fast_interval = 1.0
        self._active_grace = 3.0  # stay fast this long after the last non-empty push
        # When False the loop only PUSHES the local outbox to the remote and never
        # pulls remote→local. Stage 2 runs push-only; the puller is turned on in
        # Stage 4 after the "no authz/identity/billing read resolves from local"
        # security audit (handoff §6). Direct pull_table() calls still work (tests).
        self._pull_enabled = pull_enabled
        self._watermarks: Dict[str, Optional[str]] = {}
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    @property
    def outbox(self) -> Outbox:
        return self._outbox

    # ── Pusher: local outbox → remote ────────────────────────────────────────

    async def push_pending(self, batch: int = 500) -> int:
        """Flush the outbox to the remote authority. Returns rows pushed.

        Entries are grouped by table, the CURRENT row is re-read from local (so
        repeated edits coalesce), fat columns are stripped, and each row is
        upserted on its key. Only entries that push cleanly are cleared, so a
        remote outage simply leaves them queued for the next pass."""
        pending = await self._outbox.pending(batch)
        if not pending:
            return 0

        # Preserve arrival order but de-dup (table,row_id) to the latest need.
        by_table: Dict[str, List[str]] = {}
        seqs_by_key: Dict[Tuple[str, str], List[int]] = {}
        for e in pending:
            t, rid, seq = e["table_name"], e["row_id"], e["seq"]
            seqs_by_key.setdefault((t, rid), []).append(seq)
            ids = by_table.setdefault(t, [])
            if rid not in ids:
                ids.append(rid)

        pushed = 0
        cleared: List[int] = []
        for table, ids in by_table.items():
            spec = self._specs.get(table)
            if not spec:
                # Unknown table — drop the marker so it doesn't wedge the queue.
                for rid in ids:
                    cleared.extend(seqs_by_key.get((table, rid), []))
                logger.warning("sync: no spec for table %r; dropping %d outbox entries",
                               table, len(ids))
                continue
            try:
                rows = self._local_rows(table, spec.key, ids)
            except Exception as e:
                # Reading the local batch failed as a whole — transient; retry next tick.
                logger.warning("sync: read of table %r failed (%s); leaving queued", table, e)
                continue
            # Push each row on its own so one unpushable row can't wedge the batch.
            present = set()
            for row in rows:
                rid = str(row.get(spec.key))
                present.add(rid)
                try:
                    # The remote upsert is a synchronous network round-trip; run it
                    # on a worker thread so a slow remote can't freeze the main event
                    # loop (which also serves every HTTP request + the LLM stream).
                    await asyncio.to_thread(self._push_row, table, spec, row)
                    pushed += 1
                    cleared.extend(seqs_by_key.get((table, rid), []))
                except Exception as e:
                    # Leave this one row queued (a transient remote error will
                    # succeed on a later tick); the rest of the batch still clears,
                    # so a single stuck row no longer starves the whole queue.
                    logger.warning("sync: push of %r row %r failed (%s); leaving row queued",
                                   table, rid, e)
            # Ids enqueued but already gone from local — clear their stale markers.
            for rid in ids:
                if rid not in present:
                    cleared.extend(seqs_by_key.get((table, rid), []))

        await self._outbox.clear(cleared)
        return pushed

    def _local_rows(self, table: str, key: str, ids: List[str]) -> List[dict]:
        res = self._local.get_raw_client().table(table).select("*").in_(key, ids).execute()
        return list(res.data or [])

    def _push_row(self, table: str, spec: SyncedSpec, row: dict) -> None:
        payload = dict(row)
        for col in spec.fat_cols:
            val = payload.get(col)
            if val is None:
                continue
            # Over the split gate → strip to NULL on the remote skeleton (the full
            # value stays in the local hot store). A small value rides along inline
            # so cross-device readers and JSON consumers still see it.
            if spec.strip_over <= 0 or len(str(val)) > spec.strip_over:
                payload[col] = None
        # PostgreSQL text columns reject NUL (0x00) bytes outright, so a single
        # transcript row carrying a stray null (garbled tool output, a bad paste)
        # would otherwise fail forever and wedge this table's push queue. Strip
        # NUL from every text value — the readable content is preserved and the
        # full copy still lives in the local hot store.
        for _col, _v in payload.items():
            if isinstance(_v, str) and "\x00" in _v:
                payload[_col] = _v.replace("\x00", "")
        self._remote.get_raw_client().table(table).upsert(payload, on_conflict=spec.key).execute()

    # ── Puller: remote → local ───────────────────────────────────────────────

    def _local_max_watermark(self, spec: SyncedSpec) -> Optional[str]:
        conn = self._local._get_conn()
        try:
            row = conn.execute(
                f"SELECT MAX({spec.watermark}) FROM {spec.table}"
            ).fetchone()
            return row[0] if row and row[0] is not None else None
        except Exception:
            return None
        finally:
            conn.close()

    async def pull_table(self, spec: SyncedSpec, limit: int = 500) -> int:
        """Pull rows the remote has that are newer than our watermark into local.

        Returns the number of rows applied. Idempotent: re-applying a row we
        already have (or just pushed) is a harmless last-writer-wins upsert."""
        wm = self._watermarks.get(spec.table)
        if wm is None:
            wm = self._local_max_watermark(spec)

        q = self._remote.get_raw_client().table(spec.table).select("*")
        if wm is not None:
            q = q.gt(spec.watermark, wm)
        q = q.order(spec.watermark, desc=False).limit(limit)
        try:
            # Remote fetch is a synchronous round-trip — run it off the main event
            # loop so the per-tick pull (one per synced table) can't stall requests.
            res = await asyncio.to_thread(q.execute)
            remote_rows = list(res.data or [])
        except Exception as e:
            logger.warning("sync: pull of table %r failed: %s", spec.table, e)
            return 0

        applied = 0
        newest = wm
        for row in remote_rows:
            try:
                # Drop NULL-valued keys so LOCAL NOT-NULL defaults fill columns the
                # remote left null (e.g. sessions.hidden / sort_order) — an explicit
                # NULL would trip the local constraint and silently fail the row.
                # Same policy the hybrid's warm-on-open mirror uses.
                clean = {k: v for k, v in row.items() if v is not None}
                self._local.get_raw_client().table(spec.table).upsert(
                    clean, on_conflict=spec.key
                ).execute()
                applied += 1
                w = row.get(spec.watermark)
                if w is not None and (newest is None or str(w) > str(newest)):
                    newest = w
            except Exception as e:
                logger.debug("sync: applying pulled row to %r failed: %s", spec.table, e)
        if newest is not None:
            self._watermarks[spec.table] = newest
        return applied

    # ── Combined tick + background loop ──────────────────────────────────────

    async def sync_once(self) -> dict:
        pushed = await self.push_pending()
        pulled = 0
        if self._pull_enabled:
            for spec in self._specs.values():
                if spec.push_only:
                    continue  # transcript etc. — pushed, never pulled
                pulled += await self.pull_table(spec)
        return {"pushed": pushed, "pulled": pulled}

    async def _run(self) -> None:
        import time as _time
        logger.info("hybrid sync engine started (interval=%.1fs, fast=%.1fs)",
                    self._interval, self._fast_interval)
        active_until = 0.0
        while not self._stop.is_set():
            result = None
            try:
                result = await self.sync_once()
            except Exception as e:  # never let one bad tick kill the loop
                logger.warning("sync tick failed: %s", e)
            # Adaptive cadence: a non-empty push means rows are actively flowing —
            # stay on the fast interval for a short grace window so a live turn
            # (e.g. a Remote Control run streaming here while a viewer polls from
            # another device) syncs near-live. Idle ticks relax to the base rate.
            now = _time.monotonic()
            if result and result.get("pushed"):
                active_until = now + self._active_grace
            interval = self._fast_interval if now < active_until else self._interval
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        logger.info("hybrid sync engine stopped")

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except Exception:
                pass
            self._task = None

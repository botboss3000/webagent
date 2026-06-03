"""
Client render recorder for webAgent — the browser-side flight recorder.

The diagnostic recorder (:mod:`app.agent.diagnostics`) captures everything the
*server* sees. But many problems — a chat bubble that renders wrong, a JS
exception, UI lag, a failed fetch — happen in the *browser*, after the server's
job is done and logged clean. Those never reach the server logs.

This module is the durable sink for what the browser ships back: HTML snapshots,
DOM-mutation deltas, lag / long-task metrics, JS errors, console warnings and
failed network calls (captured by ``ui/js/recorder.js``). Every record carries
``session_seq`` — the same monotonic per-session counter stamped on WS events
and interactions — so a render moment joins exactly to the interaction and
diagnostics rows beside it.

Design mirrors the diagnostic recorder's "queue in front of the DB" approach:
:func:`RenderRecorder.ingest` is cheap and non-blocking — it appends accepted
records to a pending deque; a single background task (started at app startup)
batch-writes and prunes the durable ``render_recordings`` table. Recording is
**off by default** — it is an investigation tool, flipped on via the
``render_recording_enabled`` app setting and flipped off again when done.

Level 1 (now): periodic full HTML snapshots + signals. Level 2 (later): the
``html`` column and ``kind='mutation'`` rows are designed to also hold
serialized per-mutation deltas for full session replay, without schema change.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# ── Constants / defaults ──────────────────────────────────────────────────────

DEFAULT_RETENTION_ROWS = 8000      # max durable rows kept in the table
DEFAULT_RETENTION_HOURS = 48       # max age of durable rows
DEFAULT_MAX_HTML_BYTES = 400_000   # server-side cap on a single html payload
DEFAULT_MAX_BATCH = 200            # max records accepted in one ingest call
FLUSH_INTERVAL_SECONDS = 3         # batch-writer cadence
PRUNE_INTERVAL_SECONDS = 120       # how often the writer prunes the table
MAX_DETAIL_LEN = 16000
MAX_LABEL_LEN = 500
SETTINGS_FILE = "data/config/app-settings.json"

# Record kinds the recorder will accept. Anything else is dropped.
VALID_KINDS = frozenset({
    "snapshot", "mutation", "lag", "js_error", "console", "network", "nav", "meta",
})

# Defaults handed to the browser so it knows what to capture and how often.
# Overridable per-key via the matching app-settings.json entries.
DEFAULT_CLIENT_CONFIG: Dict[str, Any] = {
    "snapshot_min_interval_ms": 4000,   # min gap between full HTML snapshots
    "snapshot_max_bytes": 400_000,      # client trims a snapshot to this size
    "longtask_min_ms": 80,              # report long tasks at/above this duration
    "network_slow_ms": 2500,            # flag fetches slower than this
    "flush_interval_ms": 5000,          # how often the browser ships its buffer
    "capture_snapshots": True,
    "capture_lag": True,
    "capture_js_errors": True,
    "capture_console": True,
    "capture_network": True,
    "capture_whole_page": True,         # record the whole app root, not just chat
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return str(uuid.uuid4())


def _truncate(s: Optional[str], n: int) -> Optional[str]:
    if s is None:
        return None
    if not isinstance(s, str):
        try:
            s = str(s)
        except Exception:
            return None
    return s if len(s) <= n else (s[:n] + f"… [+{len(s) - n} chars]")


def _read_settings() -> Dict[str, Any]:
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}


# ── Recorder ──────────────────────────────────────────────────────────────────

class RenderRecorder:
    """Durable sink for browser render recordings. One global instance.

    Settings are re-read on each access of :meth:`enabled` / :meth:`client_config`
    so toggling ``render_recording_enabled`` in app-settings.json takes effect
    without a restart."""

    def __init__(self) -> None:
        s = _read_settings()
        self.retention_rows = int(s.get("render_recording_retention_rows", DEFAULT_RETENTION_ROWS) or DEFAULT_RETENTION_ROWS)
        self.retention_hours = float(s.get("render_recording_retention_hours", DEFAULT_RETENTION_HOURS) or DEFAULT_RETENTION_HOURS)
        self.max_html_bytes = int(s.get("render_recording_max_html_bytes", DEFAULT_MAX_HTML_BYTES) or DEFAULT_MAX_HTML_BYTES)
        self._pending: Deque[Dict[str, Any]] = collections.deque(maxlen=20000)
        self._task: Optional[asyncio.Task] = None
        self._last_prune: float = 0.0
        self._accepted = 0
        self._dropped = 0

    # ── Live settings ──────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        """Master on/off, re-read live so the toggle needs no restart."""
        return bool(_read_settings().get("render_recording_enabled", False))

    def client_config(self) -> Dict[str, Any]:
        """Capture knobs handed to the browser (defaults + app-settings overrides)."""
        s = _read_settings()
        cfg = dict(DEFAULT_CLIENT_CONFIG)
        for k in cfg:
            sk = f"render_recording_{k}"
            if sk in s and s[sk] is not None:
                cfg[k] = s[sk]
        return cfg

    # ── Ingest ───────────────────────────────────────────────────────────────

    def ingest(
        self,
        records: List[Dict[str, Any]],
        *,
        user_id: Optional[str] = None,
        recv_ts: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate + queue a batch of browser records. Cheap, never raises.

        Server-trusted fields (``recv_ts``, ``user_id``) are stamped here so the
        browser can't spoof them; everything else is taken from the payload after
        clamping. Returns a small summary."""
        if not self.enabled:
            return {"accepted": 0, "dropped": 0, "disabled": True}
        recv_ts = recv_ts or _now_iso()
        accepted = 0
        dropped = 0
        for raw in (records or [])[:DEFAULT_MAX_BATCH]:
            try:
                rec = self._normalize_incoming(raw, user_id=user_id, recv_ts=recv_ts)
                if rec is None:
                    dropped += 1
                    continue
                self._pending.append(rec)
                accepted += 1
            except Exception:
                dropped += 1
        if len(records or []) > DEFAULT_MAX_BATCH:
            dropped += len(records) - DEFAULT_MAX_BATCH
        self._accepted += accepted
        self._dropped += dropped
        if accepted:
            self._ensure_task()
        return {"accepted": accepted, "dropped": dropped}

    def _normalize_incoming(
        self, raw: Dict[str, Any], *, user_id: Optional[str], recv_ts: str
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(raw, dict):
            return None
        kind = str(raw.get("kind") or "").lower()
        if kind not in VALID_KINDS:
            return None
        html = raw.get("html")
        html_bytes = None
        if isinstance(html, str):
            if len(html) > self.max_html_bytes:
                html = html[: self.max_html_bytes] + "<!-- truncated -->"
            html_bytes = len(html)
        else:
            html = None
        detail = raw.get("detail")
        if detail is not None and not isinstance(detail, str):
            try:
                detail = json.dumps(detail, default=str)
            except Exception:
                detail = str(detail)
        detail = _truncate(detail, MAX_DETAIL_LEN)
        try:
            seq = int(raw["seq"]) if raw.get("seq") is not None else None
        except (TypeError, ValueError):
            seq = None
        try:
            session_seq = int(raw["session_seq"]) if raw.get("session_seq") is not None else None
        except (TypeError, ValueError):
            session_seq = None
        try:
            value_num = float(raw["value_num"]) if raw.get("value_num") is not None else None
        except (TypeError, ValueError):
            value_num = None
        return {
            "id": _uuid(),
            "ts": _truncate(raw.get("ts"), 40) or recv_ts,
            "recv_ts": recv_ts,
            "kind": kind,
            "session_id": _truncate(raw.get("session_id"), 80),
            "turn_id": _truncate(raw.get("turn_id"), 80),
            "session_seq": session_seq,
            "user_id": user_id or _truncate(raw.get("user_id"), 80),
            "agent_id": _truncate(raw.get("agent_id"), 80),
            "client_id": _truncate(raw.get("client_id"), 80),
            "seq": seq,
            "url": _truncate(raw.get("url"), 600),
            "level": _truncate((raw.get("level") or "").lower() or None, 16),
            "label": _truncate(raw.get("label"), MAX_LABEL_LEN),
            "value_num": value_num,
            "detail": detail,
            "html": html,
            "html_bytes": html_bytes,
        }

    # ── Background writer + pruner ─────────────────────────────────────────

    def _ensure_task(self) -> None:
        if self._task and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._run())

    def start(self) -> None:
        """Idempotently start the background writer (call at app startup)."""
        self._ensure_task()

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
                await self._flush_once()
                now = time.time()
                if now - self._last_prune >= PRUNE_INTERVAL_SECONDS:
                    self._last_prune = now
                    await self._prune_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("render recorder writer error: %s", e)

    async def _flush_once(self) -> None:
        if not self._pending:
            return
        batch: List[Dict[str, Any]] = []
        while self._pending and len(batch) < 200:
            try:
                batch.append(self._pending.popleft())
            except IndexError:
                break
        if not batch:
            return
        db = self._db()
        if db is None or not hasattr(db, "insert_render_recordings_batch"):
            return
        try:
            await db.insert_render_recordings_batch(batch)
        except Exception as e:
            logger.debug("insert_render_recordings_batch failed: %s", e)

    async def _prune_once(self) -> None:
        db = self._db()
        if db is None or not hasattr(db, "prune_render_recordings"):
            return
        try:
            await db.prune_render_recordings(
                max_rows=self.retention_rows,
                max_age_seconds=self.retention_hours * 3600.0,
            )
        except Exception as e:
            logger.debug("prune_render_recordings failed: %s", e)

    @staticmethod
    def _db() -> Any:
        try:
            from app.db import get_db
            return get_db()
        except Exception:
            return None

    # ── Read / clear / stats ───────────────────────────────────────────────

    async def query(
        self,
        *,
        rec_id: Optional[str] = None,
        kinds: Optional[Iterable[str]] = None,
        levels: Optional[Iterable[str]] = None,
        session_id: Optional[str] = None,
        client_id: Optional[str] = None,
        session_seq: Optional[int] = None,
        since_minutes: Optional[float] = None,
        search: Optional[str] = None,
        include_html: bool = False,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        db = self._db()
        if db is None or not hasattr(db, "query_render_recordings"):
            return []
        since_iso = None
        if since_minutes:
            from datetime import timedelta
            since_iso = (datetime.now(timezone.utc) - timedelta(minutes=since_minutes)).isoformat()
        rows = await db.query_render_recordings(
            rec_id=rec_id,
            kinds=[str(k).lower() for k in kinds] if kinds else None,
            levels=[str(x).lower() for x in levels] if levels else None,
            session_id=session_id,
            client_id=client_id,
            session_seq=session_seq,
            since=since_iso,
            search=search,
            include_html=include_html,
            limit=limit,
        )
        return [self._normalize_out(r) for r in (rows or [])]

    @staticmethod
    def _normalize_out(r: Dict[str, Any]) -> Dict[str, Any]:
        r = dict(r)
        d = r.get("detail")
        if isinstance(d, str) and d:
            try:
                r["detail"] = json.loads(d)
            except Exception:
                pass
        return r

    async def clear(
        self,
        *,
        older_than_minutes: Optional[float] = None,
        kinds: Optional[Iterable[str]] = None,
        session_id: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        db = self._db()
        deleted = 0
        if db is not None and hasattr(db, "clear_render_recordings"):
            try:
                deleted = await db.clear_render_recordings(
                    older_than_seconds=(older_than_minutes * 60.0) if older_than_minutes else None,
                    kinds=[str(k).lower() for k in kinds] if kinds else None,
                    session_id=session_id,
                    search=search,
                )
            except Exception as e:
                logger.debug("clear_render_recordings failed: %s", e)
        # Also drop anything still queued in RAM that matches a full clear.
        pending_dropped = 0
        if older_than_minutes is None and not kinds and not session_id and not search:
            pending_dropped = len(self._pending)
            self._pending.clear()
        return {"db_deleted": deleted, "pending_dropped": pending_dropped}

    async def stats(self) -> Dict[str, Any]:
        db = self._db()
        durable = {"rows": 0, "html_bytes": 0, "by_kind": {}}
        if db is not None and hasattr(db, "render_recordings_stats"):
            try:
                durable = await db.render_recordings_stats()
            except Exception as e:
                logger.debug("render_recordings_stats failed: %s", e)
        return {
            "enabled": self.enabled,
            "pending_writes": len(self._pending),
            "accepted_total": self._accepted,
            "dropped_total": self._dropped,
            "retention_rows": self.retention_rows,
            "retention_hours": self.retention_hours,
            "durable": durable,
        }


# ── Module singleton ────────────────────────────────────────────────────────--

_recorder: Optional[RenderRecorder] = None


def get_render_recorder() -> RenderRecorder:
    global _recorder
    if _recorder is None:
        _recorder = RenderRecorder()
    return _recorder


def start_render_recorder() -> None:
    """Start the background writer (idempotent). Call from app startup."""
    get_render_recorder().start()

"""
Central diagnostic flight-recorder for webAgent.

Captures the server's and agent loop's internal activity into a rolling
window so an operator — or a diagnostic AI agent — can inspect what has been
happening and what went wrong, *after the fact*, without tailing the console.

Four capture categories:

  • ``server`` — WARNING/ERROR log records (with tracebacks) from the stdlib
                 ``logging`` system, fed in by :class:`DiagnosticLogHandler`.
  • ``loop``   — agent-loop pipeline events (provider race, tool calls,
                 guardrail trips, stalls) tapped from ``_emit_to_visualizers``.
  • ``run``    — run lifecycle (start / complete / interrupted / error /
                 resume) tapped from the Run Manager / runner.
  • ``tool``   — tool execution errors surfaced during a turn.

Storage is two-tier, mirroring the chat stream's "RAM buffer in front of the
DB" design (see :mod:`app.agent.run_buffer`):

  • an in-memory **ring** (always on, fast, lost on restart) drives the live
    feed and the freshest "what is happening right now";
  • a durable, auto-pruned **``diagnostics``** table keeps warnings, errors and
    run outcomes so post-hoc diagnosis survives ring eviction and restarts.

:func:`DiagnosticRecorder.record` is deliberately cheap and NON-blocking — it
appends to the ring and (only when the record warrants durability) queues it
for a background batch-writer, so nothing on the hot streaming path ever awaits
the database. The writer + pruner run as one asyncio task started at app
startup (:func:`start_recorder`), exactly like the RunBuffer sweeper.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
import traceback
import uuid
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# ── Constants / defaults ──────────────────────────────────────────────────────

LEVELS: Dict[str, int] = {
    "debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50,
}

DEFAULT_RING_SIZE = 2000          # in-memory records kept for the live feed
DEFAULT_RETENTION_ROWS = 20000    # max durable rows kept in the DB table
DEFAULT_RETENTION_HOURS = 72      # max age of durable rows
DEFAULT_LOG_DIR = "logs"          # per-source text files live here
DEFAULT_LOG_MAX_BYTES = 5_000_000 # rotate each file at ~5 MB
DEFAULT_LOG_BACKUPS = 3           # keep this many rotated files per source
FLUSH_INTERVAL_SECONDS = 3        # batch-writer cadence
PRUNE_INTERVAL_SECONDS = 120      # how often the writer prunes the DB table
MAX_MESSAGE_LEN = 4000            # truncate over-long messages
MAX_DETAIL_LEN = 16000            # truncate serialized detail blobs
SETTINGS_FILE = "app-settings.json"

# Loop/visualizer event types worth recording. Raw stream deltas, db echoes and
# user-message broadcasts are intentionally skipped — they are noise for
# diagnosis and would flood the ring.
_TAPPED_EVENT_TYPES = frozenset({"pipeline", "tool_call", "tool_result"})

# Pipeline ``step`` values that signal a problem → recorded at WARNING and made
# durable, instead of the routine INFO/RAM-only treatment.
_PROBLEM_STEPS = frozenset({
    "guardrail_block", "guardrail", "stall_guard", "stall", "error",
    "turn_error", "interrupted", "interrupt", "max_turns", "wall_timeout",
    "loser_error", "permission_denied", "destructive_block",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_minus(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


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

class DiagnosticRecorder:
    """Two-tier (RAM ring + durable DB) recorder. One global instance."""

    def __init__(self) -> None:
        s = _read_settings()
        self.enabled: bool = bool(s.get("diagnostics_enabled", True))
        ring_size = int(s.get("diagnostics_ring_size", DEFAULT_RING_SIZE) or DEFAULT_RING_SIZE)
        self.retention_rows = int(s.get("diagnostics_retention_rows", DEFAULT_RETENTION_ROWS) or DEFAULT_RETENTION_ROWS)
        self.retention_hours = float(s.get("diagnostics_retention_hours", DEFAULT_RETENTION_HOURS) or DEFAULT_RETENTION_HOURS)

        # Per-source rotating text files (logs/<category>.log). A plain,
        # tail-able mirror of every captured record, in addition to the DB.
        self.file_logging: bool = bool(s.get("diagnostics_file_logging", True))
        self.log_dir: str = s.get("diagnostics_log_dir", DEFAULT_LOG_DIR) or DEFAULT_LOG_DIR
        self.log_max_bytes: int = int(s.get("diagnostics_log_max_bytes", DEFAULT_LOG_MAX_BYTES) or DEFAULT_LOG_MAX_BYTES)
        self.log_backups: int = int(s.get("diagnostics_log_backups", DEFAULT_LOG_BACKUPS) or DEFAULT_LOG_BACKUPS)
        self._file_loggers: Dict[str, Any] = {}  # category → Logger (or False if init failed)

        # Ring + pending-write queue are both plain deques. ``deque.append`` /
        # ``popleft`` are atomic under CPython's GIL, so they are safe to touch
        # from the logging handler (any thread) without a lock.
        self._ring: Deque[Dict[str, Any]] = collections.deque(maxlen=ring_size)
        self._pending: Deque[Dict[str, Any]] = collections.deque(maxlen=ring_size * 4)
        self._task: Optional[asyncio.Task] = None
        self._last_prune: float = 0.0
        # Lightweight running counters for the stats endpoint.
        self._counts: Dict[str, int] = {k: 0 for k in LEVELS}

    # ── Capture ────────────────────────────────────────────────────────────

    def record(
        self,
        level: str,
        category: str,
        message: str,
        *,
        source: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        user_id: Optional[str] = None,
        persist: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        """Append a diagnostic record. Cheap, non-blocking, never raises.

        ``persist=None`` (the default) auto-decides durability: WARNING+ and all
        ``run`` records are written to the DB; routine INFO/loop records stay in
        the RAM ring only. Pass ``persist=True/False`` to force it.
        """
        if not self.enabled:
            return None
        try:
            lvl = (level or "info").lower()
            if lvl not in LEVELS:
                lvl = "info"
            detail_str: Optional[str] = None
            if detail is not None:
                try:
                    detail_str = _truncate(json.dumps(detail, default=str), MAX_DETAIL_LEN)
                except Exception:
                    detail_str = _truncate(str(detail), MAX_DETAIL_LEN)
            rec: Dict[str, Any] = {
                "id": _uuid(),
                "ts": _now_iso(),
                "level": lvl,
                "category": category or "server",
                "source": _truncate(source, 300),
                "message": _truncate(message, MAX_MESSAGE_LEN) or "",
                "detail": detail_str,
                "session_id": session_id,
                "turn_id": turn_id,
                "agent_id": agent_id,
                "user_id": user_id,
            }
            self._ring.append(rec)
            self._counts[lvl] = self._counts.get(lvl, 0) + 1
            self._write_file(rec)
            if persist is None:
                persist = LEVELS[lvl] >= LEVELS["warning"] or (category == "run")
            if persist:
                self._pending.append(rec)
                self._ensure_task()
            return rec
        except Exception:
            # A recorder must never break its caller.
            return None

    # ── Per-source text files ──────────────────────────────────────────────

    def _write_file(self, rec: Dict[str, Any]) -> None:
        """Append one line for this record to logs/<category>.log. Never raises."""
        if not self.file_logging:
            return
        try:
            lg = self._get_file_logger(rec.get("category") or "misc")
            if lg is None:
                return
            lg.log(LEVELS.get(rec.get("level"), 20), self._format_line(rec))
        except Exception:
            pass

    def _get_file_logger(self, category: str):
        """Lazily build a dedicated rotating-file logger per category.

        Each category gets its own ``logs/<category>.log`` (size-rotated, N
        backups). ``propagate=False`` so these lines never bubble up to the root
        logger (which would echo them to the console and re-capture them via
        DiagnosticLogHandler — an infinite loop)."""
        cat = "".join(c if (c.isalnum() or c == "_") else "_" for c in (category or "misc").lower()) or "misc"
        cached = self._file_loggers.get(cat)
        if cached is not None:
            return cached or None  # False sentinel → init failed before, skip
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            lg = logging.getLogger(f"webagent.diagnostics.file.{cat}")
            lg.setLevel(logging.DEBUG)
            lg.propagate = False
            if not any(isinstance(h, RotatingFileHandler) for h in lg.handlers):
                h = RotatingFileHandler(
                    os.path.join(self.log_dir, f"{cat}.log"),
                    maxBytes=self.log_max_bytes, backupCount=self.log_backups,
                    encoding="utf-8", delay=True,
                )
                h.setFormatter(logging.Formatter("%(message)s"))
                lg.addHandler(h)
            self._file_loggers[cat] = lg
            return lg
        except Exception as e:
            logger.debug("diagnostics file logger init failed for %s: %s", cat, e)
            self._file_loggers[cat] = False
            return None

    @staticmethod
    def _format_line(rec: Dict[str, Any]) -> str:
        """One self-contained line per record. ``detail`` is already a single-line
        JSON string (newlines in tracebacks are escaped), so the line stays whole."""
        ids = []
        for k in ("session_id", "turn_id", "agent_id", "user_id"):
            v = rec.get(k)
            if v:
                ids.append(f"{k[:-3]}={v}")  # session_id → session=...
        id_part = ("  |  " + " ".join(ids)) if ids else ""
        detail = rec.get("detail")
        detail_part = ("  |  " + detail) if detail else ""
        return (f"{rec.get('ts','')}  {str(rec.get('level','info')).upper():<8} "
                f"[{rec.get('source') or '-'}] {rec.get('message','')}{id_part}{detail_part}")

    # ── Background writer + pruner ─────────────────────────────────────────

    def _ensure_task(self) -> None:
        if self._task and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop yet (e.g. logging during import) — flush later
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
            except Exception as e:  # never let the loop die
                logger.debug("diagnostics writer error: %s", e)

    async def _flush_once(self) -> None:
        if not self._pending:
            return
        batch: List[Dict[str, Any]] = []
        while self._pending and len(batch) < 500:
            try:
                batch.append(self._pending.popleft())
            except IndexError:
                break
        if not batch:
            return
        db = self._db()
        if db is None or not hasattr(db, "insert_diagnostics_batch"):
            return  # backend can't persist → records stay RAM-only
        try:
            await db.insert_diagnostics_batch(batch)
        except Exception as e:
            logger.debug("insert_diagnostics_batch failed: %s", e)

    async def _prune_once(self) -> None:
        db = self._db()
        if db is None or not hasattr(db, "prune_diagnostics"):
            return
        try:
            await db.prune_diagnostics(
                max_rows=self.retention_rows,
                max_age_seconds=self.retention_hours * 3600.0,
            )
        except Exception as e:
            logger.debug("prune_diagnostics failed: %s", e)

    @staticmethod
    def _db() -> Any:
        try:
            from app.db import get_db
            return get_db()
        except Exception:
            return None

    # ── Read ───────────────────────────────────────────────────────────────

    async def query(
        self,
        *,
        levels: Optional[Iterable[str]] = None,
        categories: Optional[Iterable[str]] = None,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        search: Optional[str] = None,
        since_minutes: Optional[float] = None,
        limit: int = 200,
        include_ring: bool = True,
    ) -> List[Dict[str, Any]]:
        """Return recent records (durable DB + live ring, merged + deduped).

        Newest first. ``detail`` is returned parsed back into a dict.
        """
        limit = max(1, min(int(limit or 200), 2000))
        lvlset = {str(x).lower() for x in levels} if levels else None
        catset = {str(x).lower() for x in categories} if categories else None
        since_iso = _iso_minus(since_minutes * 60.0) if since_minutes else None
        needle = search.lower() if search else None

        merged: Dict[str, Dict[str, Any]] = {}

        # Durable rows first (so the ring copy, if any, overwrites identically).
        db = self._db()
        if db is not None and hasattr(db, "query_diagnostics"):
            try:
                rows = await db.query_diagnostics(
                    levels=list(lvlset) if lvlset else None,
                    categories=list(catset) if catset else None,
                    session_id=session_id,
                    agent_id=agent_id,
                    since=since_iso,
                    search=search,
                    limit=limit,
                )
                for r in rows or []:
                    merged[r["id"]] = self._normalize(r)
            except Exception as e:
                logger.debug("query_diagnostics failed: %s", e)

        if include_ring:
            for rec in list(self._ring):
                merged[rec["id"]] = self._normalize(rec)

        out = [r for r in merged.values() if self._matches(
            r, lvlset, catset, session_id, agent_id, since_iso, needle)]
        out.sort(key=lambda r: r.get("ts") or "", reverse=True)
        return out[:limit]

    # ── Clear / purge ──────────────────────────────────────────────────────

    async def clear(
        self,
        *,
        older_than_minutes: Optional[float] = None,
        levels: Optional[Iterable[str]] = None,
        categories: Optional[Iterable[str]] = None,
        search: Optional[str] = None,
        clear_files: bool = False,
    ) -> Dict[str, Any]:
        """Purge diagnostics. With NO scope it clears everything; otherwise it
        clears only records matching the scope (older-than / levels / categories
        / search). Purges the RAM ring and the durable DB table; when
        ``clear_files`` is set on a *full* clear it also truncates the
        ``logs/*.log`` text files. Returns a small summary."""
        older_secs = older_than_minutes * 60.0 if older_than_minutes else None
        lvlset = {str(x).lower() for x in levels} if levels else None
        catset = {str(x).lower() for x in categories} if categories else None
        needle = search.lower() if search else None
        cutoff_iso = _iso_minus(older_secs) if older_secs else None
        is_full = older_secs is None and not lvlset and not catset and not needle

        # 1. RAM ring — keep records the clear does NOT target.
        kept: Deque[Dict[str, Any]] = collections.deque(maxlen=self._ring.maxlen)
        removed = 0
        for rec in list(self._ring):
            if self._targets_clear(rec, cutoff_iso, lvlset, catset, needle):
                removed += 1
            else:
                kept.append(rec)
        self._ring = kept

        # 2. Durable DB table.
        db_deleted = 0
        db = self._db()
        if db is not None and hasattr(db, "clear_diagnostics"):
            try:
                db_deleted = await db.clear_diagnostics(
                    older_than_seconds=older_secs,
                    levels=list(lvlset) if lvlset else None,
                    categories=list(catset) if catset else None,
                    search=search,
                )
            except Exception as e:
                logger.debug("clear_diagnostics failed: %s", e)

        # 3. Text files — only on a full clear (partial line-rewrite of an
        #    open, rotating file is fragile, especially on Windows).
        files_cleared: List[str] = []
        if clear_files and is_full:
            files_cleared = self._clear_files()

        return {"ring_removed": removed, "db_deleted": db_deleted,
                "files_cleared": files_cleared, "scope": "all" if is_full else "filtered"}

    @staticmethod
    def _targets_clear(rec, cutoff_iso, lvlset, catset, needle) -> bool:
        """True if this record matches every supplied clear criterion (no
        criteria → matches everything → full clear)."""
        if cutoff_iso is not None and (rec.get("ts") or "") >= cutoff_iso:
            return False  # newer than the cutoff → keep
        if lvlset and (rec.get("level") or "").lower() not in lvlset:
            return False
        if catset and (rec.get("category") or "").lower() not in catset:
            return False
        if needle:
            hay = " ".join(str(rec.get(k) or "") for k in ("message", "source", "category"))
            det = rec.get("detail")
            if det:
                hay += " " + (det if isinstance(det, str) else json.dumps(det, default=str))
            if needle not in hay.lower():
                return False
        return True

    def _clear_files(self) -> List[str]:
        """Truncate the currently-open per-source files (in place, keeping the
        handle) and delete any rotated backups. Returns cleared file names."""
        cleared: List[str] = []
        open_paths = set()
        for lg in self._file_loggers.values():
            if not lg:
                continue
            for h in getattr(lg, "handlers", []):
                if not isinstance(h, RotatingFileHandler):
                    continue
                try:
                    h.acquire()
                    try:
                        if h.stream:
                            h.stream.seek(0)
                            h.stream.truncate()
                        open_paths.add(os.path.abspath(h.baseFilename))
                        cleared.append(os.path.basename(h.baseFilename))
                    finally:
                        h.release()
                except Exception:
                    pass
        try:
            import glob
            for p in glob.glob(os.path.join(self.log_dir, "*.log*")):
                if os.path.abspath(p) in open_paths:
                    continue
                try:
                    os.remove(p)
                    cleared.append(os.path.basename(p))
                except Exception:
                    pass
        except Exception:
            pass
        return cleared

    @staticmethod
    def _normalize(r: Dict[str, Any]) -> Dict[str, Any]:
        r = dict(r)
        d = r.get("detail")
        if isinstance(d, str) and d:
            try:
                r["detail"] = json.loads(d)
            except Exception:
                pass  # leave as string if it isn't valid JSON
        return r

    @staticmethod
    def _matches(r, lvlset, catset, session_id, agent_id, since_iso, needle) -> bool:
        if lvlset and (r.get("level") or "").lower() not in lvlset:
            return False
        if catset and (r.get("category") or "").lower() not in catset:
            return False
        if session_id and r.get("session_id") != session_id:
            return False
        if agent_id and r.get("agent_id") != agent_id:
            return False
        if since_iso and (r.get("ts") or "") < since_iso:
            return False
        if needle:
            hay = " ".join(str(r.get(k) or "") for k in ("message", "source", "category"))
            det = r.get("detail")
            if det is not None:
                hay += " " + (det if isinstance(det, str) else json.dumps(det, default=str))
            if needle not in hay.lower():
                return False
        return True

    def stats(self) -> Dict[str, Any]:
        ring_levels: Dict[str, int] = {k: 0 for k in LEVELS}
        for rec in list(self._ring):
            lvl = rec.get("level") or "info"
            ring_levels[lvl] = ring_levels.get(lvl, 0) + 1
        return {
            "enabled": self.enabled,
            "ring_size": len(self._ring),
            "ring_capacity": self._ring.maxlen,
            "pending_writes": len(self._pending),
            "total_by_level": dict(self._counts),
            "ring_by_level": ring_levels,
            "file_logging": self.file_logging,
            "log_dir": self.log_dir if self.file_logging else None,
            "log_files": sorted(self._file_loggers.keys()) if self.file_logging else [],
        }


# ── Module singleton ──────────────────────────────────────────────────────────

_recorder: Optional[DiagnosticRecorder] = None


def get_recorder() -> DiagnosticRecorder:
    global _recorder
    if _recorder is None:
        _recorder = DiagnosticRecorder()
    return _recorder


def record(level: str, category: str, message: str, **kw) -> None:
    """Module-level convenience wrapper around the singleton recorder."""
    get_recorder().record(level, category, message, **kw)


def start_recorder() -> None:
    """Start the background writer (idempotent). Call from app startup."""
    get_recorder().start()


# ── stdlib logging → recorder bridge ──────────────────────────────────────────

class DiagnosticLogHandler(logging.Handler):
    """Feeds WARNING+ log records (with tracebacks) into the recorder.

    Attached to the root logger so it captures everything the app logs at the
    chosen level — including the unhandled-exception handler in ``app.main`` and
    every module's ``logger.error``/``logger.warning`` call.
    """

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            name = record.name or ""
            # Avoid feedback loops: never re-record our own writer/debug chatter.
            if name.startswith("app.agent.diagnostics"):
                return
            detail: Dict[str, Any] = {
                "logger": name,
                "where": f"{record.pathname}:{record.lineno}",
                "func": record.funcName,
            }
            if record.exc_info:
                detail["traceback"] = _truncate(
                    "".join(traceback.format_exception(*record.exc_info)), MAX_DETAIL_LEN)
            get_recorder().record(
                level=record.levelname.lower(),
                category="server",
                message=record.getMessage(),
                source=name,
                detail=detail,
            )
        except Exception:
            # Logging handlers must never raise.
            pass


def install_log_handler(level: int = logging.WARNING) -> None:
    """Attach a single DiagnosticLogHandler to the root logger (idempotent)."""
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, DiagnosticLogHandler):
            return
    h = DiagnosticLogHandler(level=level)
    h.setLevel(level)
    root.addHandler(h)


# ── High-level capture helpers (used by the loop / run lifecycle taps) ─────────

def tap_loop_event(session_id: str, event: Dict[str, Any]) -> None:
    """Record an interesting agent-loop / visualizer event. Never raises.

    Called from ``app.api.chat._emit_to_visualizers`` for every emitted event;
    this function decides what (if anything) is worth keeping.
    """
    try:
        rec = get_recorder()
        if not rec.enabled:
            return
        etype = event.get("type")
        if etype not in _TAPPED_EVENT_TYPES:
            return
        turn_id = event.get("turn_id")
        agent_id = event.get("agent_id") or event.get("agent")
        user_id = event.get("user_id")

        if etype == "pipeline":
            step = event.get("step") or "pipeline"
            problem = step in _PROBLEM_STEPS or bool(event.get("error"))
            level = "warning" if problem else "info"
            detail = {k: v for k, v in event.items()
                      if k not in ("type", "session_id", "session_seq", "turn_seq", "emit_time")}
            rec.record(level, "loop", f"pipeline: {step}", source=step,
                       detail=detail, session_id=session_id, turn_id=turn_id,
                       agent_id=agent_id, user_id=user_id,
                       persist=problem or None)
        elif etype == "tool_call":
            name = event.get("name") or event.get("tool_name") or "tool"
            rec.record("info", "tool", f"tool_call: {name}", source=name,
                       detail={"arguments": event.get("arguments") or event.get("args")},
                       session_id=session_id, turn_id=turn_id, agent_id=agent_id,
                       user_id=user_id, persist=False)
        elif etype == "tool_result":
            name = event.get("name") or event.get("tool_name") or "tool"
            is_err = bool(event.get("error") or event.get("is_error"))
            rec.record("error" if is_err else "info", "tool",
                       f"tool_result: {name}" + (" (error)" if is_err else ""),
                       source=name,
                       detail={"result": _truncate(
                           event.get("result") if isinstance(event.get("result"), str)
                           else json.dumps(event.get("result"), default=str), 4000),
                           "error": event.get("error")},
                       session_id=session_id, turn_id=turn_id, agent_id=agent_id,
                       user_id=user_id, persist=is_err or None)
    except Exception:
        pass


def record_run_lifecycle(
    phase: str,
    session_id: str,
    *,
    status: Optional[str] = None,
    stop_cause: Optional[str] = None,
    error: Optional[str] = None,
    agent_id: Optional[str] = None,
    user_id: Optional[str] = None,
    origin: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    """Record a run-manager lifecycle transition (start / finish / resume)."""
    try:
        # error / crash outcomes are warnings; clean stops are info.
        level = "info"
        if status == "error" or (stop_cause in ("crash", "zombie", "frozen")):
            level = "error"
        elif status == "interrupted" and stop_cause not in ("user_stop", "replaced"):
            level = "warning"
        msg = f"run {phase}: {status or ''}".strip()
        if stop_cause:
            msg += f" ({stop_cause})"
        d = dict(detail or {})
        d.update({"phase": phase, "status": status, "stop_cause": stop_cause,
                  "origin": origin, "error": _truncate(error, MAX_DETAIL_LEN)})
        get_recorder().record(level, "run", msg, source=phase, detail=d,
                              session_id=session_id, agent_id=agent_id,
                              user_id=user_id, persist=True)
    except Exception:
        pass

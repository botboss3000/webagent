"""Liveness watchdog — the self-healing core for runs that stop *while the
process is alive* (boot recovery in app/agent/runner.py handles process death).

A single asyncio task wakes every ``poll_seconds`` and sweeps the durable
``session_runs`` rows still marked 'running'. For each it decides, using
``RunManager.is_running`` (the in-RAM truth for this process) plus the
``heartbeat_at`` the loop advances:

  * **healthy**  — live task with a fresh heartbeat → leave it alone.
  * **frozen**   — live task but no heartbeat for ``frozen_threshold`` seconds
                   (stuck inside an await the cooperative interrupt can't reach):
                   tag it, hard-cancel it, then resume from durable history.
  * **zombie**   — row still 'running' but NO live task past ``zombie_grace``
                   (the task vanished without finalizing): flip it terminal and
                   resume.

Fully backend-driven — it needs no user WebSocket, so background, sub-agent and
delegated runs are healed too. All resume decisions (eligibility, retry budget,
backoff, opt-out, lease) are delegated to ``runner.resume_one``. Mirrors the
shape of app/scheduler/local.py.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        # Tolerate the bare SQLite 'YYYY-MM-DD HH:MM:SS' form (space, no tz).
        try:
            dt = datetime.fromisoformat(s.replace(" ", "T"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class RunWatchdog:
    name = "run_watchdog"

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running: bool = False
        self._last_poll: Optional[str] = None
        self._last_error: Optional[str] = None
        self._resumes: int = 0
        self._frozen_detected: int = 0
        self._zombie_detected: int = 0

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="run_watchdog_loop")
        logger.info("RunWatchdog started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("RunWatchdog stopped")

    async def get_status(self) -> dict:
        from app.admin.settings import get_self_heal_config
        try:
            cfg = get_self_heal_config()
        except Exception:
            cfg = {}
        return {
            "provider": self.name,
            "running": bool(self._task and not self._task.done() and self._running),
            "enabled": cfg.get("watchdog_enabled", True),
            "poll_seconds": cfg.get("poll_seconds"),
            "frozen_threshold_seconds": cfg.get("frozen_threshold_seconds"),
            "zombie_grace_seconds": cfg.get("zombie_grace_seconds"),
            "last_poll": self._last_poll,
            "last_error": self._last_error,
            "resumes": self._resumes,
            "frozen_detected": self._frozen_detected,
            "zombie_detected": self._zombie_detected,
        }

    async def _loop(self) -> None:
        # Let startup (including boot-resume) settle before the first sweep.
        await asyncio.sleep(8)
        while self._running:
            poll = 30
            try:
                poll = await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.exception("RunWatchdog tick failed: %s", e)
                self._last_error = str(e)[:200]
            try:
                await asyncio.sleep(max(5, poll))
            except asyncio.CancelledError:
                raise

    async def _tick(self) -> int:
        from app.db import get_db
        from app.db.local import _iso_now
        from app.agent.run_manager import get_run_manager
        from app.admin.settings import get_self_heal_config
        from app.agent import runner

        cfg = get_self_heal_config()
        poll = cfg["poll_seconds"]
        self._last_poll = _iso_now()
        if not cfg["watchdog_enabled"]:
            return poll

        db = get_db()
        rm = get_run_manager()
        frozen_th = cfg["frozen_threshold_seconds"]
        zombie_grace = cfg["zombie_grace_seconds"]

        try:
            rows = await db.run_state_list_active_all()
        except Exception as e:  # noqa: BLE001
            self._last_error = str(e)[:200]
            return poll

        now = datetime.now(timezone.utc)
        for row in rows:
            sid = row.get("session_id")
            if not sid:
                continue
            origin = row.get("origin") or "web"
            # Never touch throwaway / excluded runs (resume_one guards too).
            if origin in runner._EXCLUDED_ORIGINS or sid.startswith(runner._THROWAWAY_PREFIXES):
                continue

            is_live = rm.is_running(sid)
            hb = _parse_iso(row.get("heartbeat_at")) or _parse_iso(row.get("updated_at"))
            hb_age = (now - hb).total_seconds() if hb else None

            if is_live:
                # FROZEN: a live task that hasn't proven liveness in too long.
                if hb_age is not None and hb_age > frozen_th:
                    self._frozen_detected += 1
                    logger.warning(
                        "Watchdog: run %s FROZEN (no heartbeat for %.0fs) — cancel + resume",
                        sid[:12], hb_age)
                    self._record_recovery(
                        "warning", "frozen",
                        f"run frozen — no heartbeat for {hb_age:.0f}s; cancel + resume",
                        sid, row,
                        detail={"heartbeat_age_seconds": round(hb_age, 1),
                                "frozen_threshold_seconds": frozen_th})
                    try:
                        await db.run_state_set_cause(sid, "frozen")
                    except Exception:
                        pass
                    # Hard-cancel; its finally flips run-state to interrupted while
                    # preserving the 'frozen' cause, making it resume-eligible.
                    try:
                        await rm.cancel(sid)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("Watchdog cancel failed for %s: %s", sid[:12], e)
                    await self._try_resume(runner, sid, "frozen")
                continue

            # Not live in this process. A fresh lease means another worker just
            # claimed it for resume / launch — don't double-act.
            lease = _parse_iso(row.get("lease_expires_at"))
            if lease and lease > now:
                continue
            upd = _parse_iso(row.get("updated_at"))
            upd_age = (now - upd).total_seconds() if upd else None
            # ZOMBIE: 'running' row with no task and no recent activity.
            if upd_age is None or upd_age > zombie_grace:
                self._zombie_detected += 1
                logger.warning(
                    "Watchdog: run %s ZOMBIE (running row, no live task, idle %s) — resume",
                    sid[:12], f"{upd_age:.0f}s" if upd_age is not None else "unknown")
                self._record_recovery(
                    "warning", "zombie",
                    "run zombie — running row, no live task, idle "
                    f"{('%.0fs' % upd_age) if upd_age is not None else 'unknown'}; resume",
                    sid, row,
                    detail={"idle_seconds": round(upd_age, 1) if upd_age is not None else None,
                            "zombie_grace_seconds": zombie_grace})
                # Flip the 'running' row terminal with the zombie cause so the
                # atomic claim (which requires status != 'running') can take it.
                try:
                    await db.run_state_finish(sid, status="interrupted", stop_cause="zombie")
                except Exception as e:  # noqa: BLE001
                    logger.warning("Watchdog zombie-finish failed for %s: %s", sid[:12], e)
                    continue
                await self._try_resume(runner, sid, "zombie")

        return poll

    def _record_recovery(self, level: str, source: str, message: str,
                         session_id: str, row: dict,
                         detail: Optional[dict] = None) -> None:
        """Structured 'recovery'-category record for the diagnostics page, in
        addition to the logger.warning above (which also lands under 'server').
        Never raises — diagnostics must not break the watchdog sweep."""
        try:
            from app.agent.diagnostics import record as _diag
            d = dict(detail or {})
            d.setdefault("stop_cause", source)
            _diag(level, "recovery", message, source=source, detail=d,
                  session_id=session_id, agent_id=(row or {}).get("agent_id"),
                  user_id=(row or {}).get("user_id"))
        except Exception:
            pass

    async def _try_resume(self, runner, sid: str, reason: str) -> None:
        try:
            if await runner.resume_one(sid, reason=reason):
                self._resumes += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("Watchdog %s-resume failed for %s: %s", reason, sid[:12], e)


# ── Module-level singleton ──
_watchdog: Optional[RunWatchdog] = None


def get_watchdog() -> RunWatchdog:
    global _watchdog
    if _watchdog is None:
        _watchdog = RunWatchdog()
    return _watchdog


async def start_watchdog() -> None:
    await get_watchdog().start()


async def stop_watchdog() -> None:
    if _watchdog is not None:
        await _watchdog.stop()

"""The background watchdog — the autonomous half of the Server Manager.

A single asyncio loop, independent of the chat, that on a fixed interval:

1. probes the linked server's health (``/health``);
2. reads the app's NEW diagnostics (flight-recorder rows) since it last looked;
3. evaluates server state + new diagnostics against the user's **alarm rules**
   and the configured **thresholds**;
4. reacts within the configured **autonomy** level — notifies the user on the
   chosen channel(s) and, when allowed, recovers the server (auto-restart with
   backoff + a crash-loop guard).

It re-reads ``monitor.json`` and ``alarms.json`` every tick, so the agent (or the
admin panel) can retune it live with no restart. It owns no UI: the app injects a
``log`` callback (transcript) and a coroutine that actually restarts the server,
so this module stays testable and decoupled from the Textual app.

``self_heal`` autonomy does NOT auto-edit code from this loop (code fixes need the
LLM and the user's eyes) — it behaves like ``auto_restart`` for recovery and the
agent handles fixes in conversation. The distinction is kept for future use.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from . import sysmetrics
from .env_probe import server_health
from .monstate import load_alarms, load_monitor_config
from .notify import Notifier

PORT = 8080
_DIAG_COLS = "id, ts, level, category, source, message, detail"

# The app registers its live Watchdog here so the monitor_* tools can read its
# in-memory state (recent reactions, restart counts) — config/alarms come from disk.
_ACTIVE: "Optional[Watchdog]" = None


def set_active_watchdog(wd: "Optional[Watchdog]") -> None:
    global _ACTIVE
    _ACTIVE = wd


def active_watchdog() -> "Optional[Watchdog]":
    return _ACTIVE


def _diag_db(project_root: Path) -> Path:
    return project_root / "app" / "db" / "local.db"


def _read_new_diagnostics(db: Path, after_id: int) -> tuple[list[dict[str, Any]], int]:
    """Read diagnostics with id greater than ``after_id`` (read-only). Returns the
    new rows (oldest first) and the highest id seen (== ``after_id`` if none/error)."""
    if not db.exists():
        return [], after_id
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(
            f"SELECT {_DIAG_COLS} FROM diagnostics WHERE id > ? ORDER BY id ASC LIMIT 500",
            (after_id,),
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return [], after_id
    out: list[dict[str, Any]] = []
    max_id = after_id
    for rid, ts, level, category, source, message, detail in rows:
        max_id = max(max_id, int(rid))
        out.append({"id": int(rid), "ts": ts or "", "level": (level or "").lower(),
                    "category": (category or "").lower(), "source": source or "",
                    "message": message or "", "detail": detail or ""})
    return out, max_id


def _current_max_id(db: Path) -> int:
    if not db.exists():
        return 0
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute("SELECT COALESCE(MAX(id), 0) FROM diagnostics").fetchone()
        con.close()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def _signature(diag: dict[str, Any]) -> str:
    """A stable key for an error so 'once'/'digest' loudness can dedupe it."""
    return f"{diag['level']}|{diag['category']}|{diag['message'][:80]}"


def _matches(rule: dict[str, Any], diag: dict[str, Any]) -> bool:
    """Does an alarm rule match a diagnostic row? All set criteria must hold."""
    contains = (rule.get("contains") or "").lower()
    level = (rule.get("level") or "").lower()
    category = (rule.get("category") or "").lower()
    if contains and contains not in (diag["message"] + " " + str(diag["detail"])).lower():
        return False
    if level and level != diag["level"]:
        return False
    if category and category != diag["category"]:
        return False
    return True


class Watchdog:
    def __init__(
        self,
        *,
        get_project_root: Callable[[], Optional[Path]],
        restart_server: Callable[[], Awaitable[str]],
        notifier: Notifier,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._get_root = get_project_root
        self._restart = restart_server
        self._notifier = notifier
        self._log = log
        self._last_health: str = "unknown"
        self._last_diag_id: int = 0
        self._primed = False
        self._seen_running = False                   # has the server been up this session?
        self._restart_times: list[float] = []      # monotonic stamps of recent restarts
        self._seen_once: set[str] = set()           # signatures already alerted ('once')
        self._digest: dict[str, int] = {}           # signature → pending count ('digest')
        self._digest_last_flush = time.monotonic()
        self._rate_alert_at = 0.0                    # last error-rate alert (monotonic)
        self._res_alert_at: dict[str, float] = {}    # per-metric last resource alert (monotonic)
        self._warned_orphan = False                  # alerted about an untracked server?
        self._port_alert_at = 0.0                    # last zombie-port alert (monotonic)
        self._last_resources: dict[str, Any] = {}    # latest sample (for snapshot / report)
        self._reactions: deque[str] = deque(maxlen=25)
        self._running = False

    # ── public ───────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        """A read-only view for ``monitor_status`` / the admin panel."""
        cfg = load_monitor_config()
        now = time.monotonic()
        return {
            "running": self._running,
            "enabled": bool(cfg.get("enabled")),
            "interval_seconds": cfg.get("interval_seconds"),
            "autonomy": cfg.get("autonomy"),
            "channels": cfg.get("channels"),
            "auto_restart": bool(cfg.get("auto_restart")),
            "error_rate_threshold": cfg.get("error_rate_threshold"),
            "max_restarts_per_hour": cfg.get("max_restarts_per_hour"),
            "last_health": self._last_health,
            "restarts_last_hour": len([t for t in self._restart_times if now - t < 3600]),
            "alarms": len(load_alarms()),
            "resources": self._last_resources,
            "recent_reactions": list(self._reactions),
        }

    async def run(self) -> None:
        """The loop. Sleeps ``interval_seconds`` between ticks; never raises out."""
        self._running = True
        try:
            while True:
                cfg = load_monitor_config()
                interval = max(5, int(cfg.get("interval_seconds") or 20))
                try:
                    if cfg.get("enabled"):
                        await self._tick(cfg)
                except Exception as e:  # one bad tick must not kill the watchdog
                    self._record(f"watchdog tick error: {type(e).__name__}: {e}")
                await asyncio.sleep(interval)
        finally:
            self._running = False

    # ── one cycle ──────────────────────────────────────────────────────────
    async def _tick(self, cfg: dict[str, Any]) -> None:
        root = self._get_root()
        if root is None:
            return  # monitoring only runs in managed mode

        health = await server_health(PORT)
        db = _diag_db(root)
        if not self._primed:
            # First tick: just establish a baseline (current health + existing
            # diagnostics backlog). Don't react — that avoids racing the app's own
            # autostart while the server is still booting, and we only alert on
            # what happens AFTER we start watching.
            self._last_health = health
            self._seen_running = self._seen_running or health == "running"
            self._last_diag_id = _current_max_id(db)
            self._primed = True
            return

        await self._handle_health(cfg, health)
        self._last_health = health

        await self._check_port(cfg, root, health)
        await self._check_resources(cfg, root)

        new_rows, max_id = _read_new_diagnostics(db, self._last_diag_id)
        self._last_diag_id = max_id
        if new_rows:
            await self._handle_diagnostics(cfg, new_rows)
        await self._maybe_flush_digest(cfg)

    async def _handle_health(self, cfg: dict[str, Any], health: str) -> None:
        if health == "running":
            if self._last_health == "stopped" and cfg.get("notify_on_recovery"):
                await self._notify(cfg, "Server recovered",
                                   "The webAgent server is healthy again.")
            self._seen_running = True
            return
        if health == "stopped":
            if self._last_health == "running" and cfg.get("notify_on_server_down"):
                await self._notify(cfg, "Server down",
                                   "The webAgent server stopped responding.")
            # Only auto-recover a server that WAS up this session — never fight the
            # app's autostart or repeatedly restart one that has never come up.
            if (self._seen_running and cfg.get("auto_restart")
                    and cfg.get("autonomy") in ("auto_restart", "self_heal")):
                await self._attempt_restart(cfg)
        # health == "unknown" (probe indeterminate): take no action this tick.

    async def _attempt_restart(self, cfg: dict[str, Any]) -> None:
        now = time.monotonic()
        self._restart_times = [t for t in self._restart_times if now - t < 3600]
        cap = int(cfg.get("max_restarts_per_hour") or 5)
        if len(self._restart_times) >= cap:
            # Crash-loop: stop flapping, escalate once per hour.
            if now - self._rate_alert_at > 3600:
                self._rate_alert_at = now
                await self._notify(
                    cfg, "Server crash-loop",
                    f"Restarted {len(self._restart_times)}× in the last hour and it keeps "
                    "dying. Auto-restart paused — check the logs/diagnostics for the cause.")
            self._record("crash-loop guard: auto-restart paused")
            return
        await asyncio.sleep(max(0, int(cfg.get("restart_backoff_seconds") or 0)))
        self._restart_times.append(time.monotonic())
        self._record("auto-restarting the server")
        try:
            msg = await self._restart()
        except Exception as e:
            msg = f"restart failed: {type(e).__name__}: {e}"
        await self._notify(cfg, "Auto-restart", msg)

    async def _check_port(self, cfg: dict[str, Any], root: Path, health: str) -> None:
        """Port/zombie detection: tell apart a clean server, an UNTRACKED server we
        didn't start, and a ZOMBIE holding port 8080 without serving /health (which
        blocks a clean restart — the classic orphaned-LISTENER run.py fights)."""
        from .tools.server import _pid_alive, _read_pidinfo  # lazy: avoid import cycle

        info = _read_pidinfo()
        tracked_alive = bool(info and info.get("pid") and _pid_alive(int(info["pid"])))

        if health == "running":
            if not tracked_alive and not self._warned_orphan:
                self._warned_orphan = True
                await self._notify(
                    cfg, "Untracked server",
                    "Port 8080 is serving but the manager has no live PID for it — it "
                    "looks like an externally-started instance. Stop/restart may not be clean.")
            elif tracked_alive:
                self._warned_orphan = False  # back to a server we own
            return

        # Server is NOT answering /health. If the port is still held, a zombie is
        # squatting on it — surface it (deduped hourly) so a restart isn't blocked.
        if sysmetrics.port_in_use(PORT):
            now = time.monotonic()
            if now - self._port_alert_at > 3600:
                self._port_alert_at = now
                await self._notify(
                    cfg, "Port held by a zombie",
                    f"Port {PORT} is held by a process that isn't answering /health. "
                    "A clean restart may be blocked until it's cleared.")

    async def _check_resources(self, cfg: dict[str, Any], root: Path) -> None:
        """Sample host disk/memory/CPU + the server process's memory, store the
        latest for reporting, and alert (deduped hourly per metric) when disk or
        memory crosses its threshold. A threshold of 0 disables that check."""
        from .tools.server import _pid_alive, _read_pidinfo  # lazy: avoid import cycle

        info = _read_pidinfo()
        pid = int(info["pid"]) if (info and info.get("pid")
                                   and _pid_alive(int(info["pid"]))) else 0
        snap = sysmetrics.gather(str(root), pid)  # one CPU delta-sample per tick
        self._last_resources = snap

        disk = snap.get("disk") or {}
        mem = snap.get("memory") or {}
        await self._resource_alert(cfg, "disk", disk.get("percent"),
                                   int(cfg.get("disk_percent_threshold") or 0),
                                   "Disk almost full")
        await self._resource_alert(cfg, "memory", mem.get("percent"),
                                   int(cfg.get("mem_percent_threshold") or 0),
                                   "Memory pressure high")
        await self._resource_alert(cfg, "cpu", snap.get("cpu_percent"),
                                   int(cfg.get("cpu_percent_threshold") or 0),
                                   "CPU sustained high")

    async def _resource_alert(self, cfg: dict[str, Any], metric: str,
                              value: Optional[float], threshold: int, title: str) -> None:
        if not threshold or value is None or value < threshold:
            return
        now = time.monotonic()
        if now - self._res_alert_at.get(metric, 0.0) < 3600:
            return
        self._res_alert_at[metric] = now
        await self._notify(cfg, title, f"{metric} at {value}% (threshold {threshold}%).")

    async def _handle_diagnostics(self, cfg: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        alarms = load_alarms()
        errors = 0
        for diag in rows:
            if diag["level"] == "error":
                errors += 1
            for rule in alarms:
                if _matches(rule, diag):
                    await self._fire_alarm(cfg, rule, diag)
        # Error-rate threshold: too many new errors in one interval → alert (deduped hourly).
        thr = int(cfg.get("error_rate_threshold") or 0)
        if thr and errors >= thr:
            now = time.monotonic()
            if now - self._rate_alert_at > 600:
                self._rate_alert_at = now
                await self._notify(cfg, "Error spike",
                                   f"{errors} new errors in the last interval (threshold {thr}).")

    async def _fire_alarm(self, cfg: dict[str, Any], rule: dict[str, Any],
                          diag: dict[str, Any]) -> None:
        channels = rule.get("channels") or cfg.get("channels") or ["desktop"]
        label = rule.get("label") or rule.get("contains") or "alarm"
        loud = rule.get("loudness", "every")
        sig = f"{rule.get('id')}|{_signature(diag)}"
        action = rule.get("action", "notify")

        if action == "auto_restart" and cfg.get("autonomy") in ("auto_restart", "self_heal"):
            self._record(f"alarm '{label}' → auto-restart")
            await self._attempt_restart(cfg)
            return

        body = f"{diag['level']}/{diag['category']}: {diag['message'][:160]}"
        if loud == "once":
            if sig in self._seen_once:
                return
            self._seen_once.add(sig)
            await self._notify(cfg, f"Alarm: {label}", body, channels)
        elif loud == "digest":
            self._digest[label] = self._digest.get(label, 0) + 1
            self._record(f"alarm '{label}' matched (digest)")
        else:  # every
            await self._notify(cfg, f"Alarm: {label}", body, channels)

    async def _maybe_flush_digest(self, cfg: dict[str, Any]) -> None:
        if not self._digest:
            return
        window = max(1, int(cfg.get("digest_minutes") or 60)) * 60
        if time.monotonic() - self._digest_last_flush < window:
            return
        lines = ", ".join(f"{k}: {v}×" for k, v in self._digest.items())
        self._digest.clear()
        self._digest_last_flush = time.monotonic()
        await self._notify(cfg, "Alarm digest", lines)

    # ── helpers ─────────────────────────────────────────────────────────
    async def _notify(self, cfg: dict[str, Any], title: str, message: str,
                      channels: Optional[list[str]] = None) -> None:
        self._record(f"{title}: {message[:80]}")
        await self._notifier.notify(title, message, channels or cfg.get("channels") or ["desktop"])

    def _record(self, what: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._reactions.append(f"[{stamp}] {what}")

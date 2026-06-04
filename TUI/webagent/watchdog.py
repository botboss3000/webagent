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

from . import playbook, sysmetrics
from .env_probe import server_health
from .monstate import load_alarms, load_monitor_config
from .notify import Notifier

PORT = 8080
_DIAG_COLS = "rowid AS _rid, ts, level, category, source, message, detail"

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
            f"SELECT {_DIAG_COLS} FROM diagnostics WHERE rowid > ? ORDER BY rowid ASC LIMIT 500",
            (after_id,),
        ).fetchall()
        con.close()
    except sqlite3.Error:
        return [], after_id
    out: list[dict[str, Any]] = []
    max_id = after_id
    for _rid, ts, level, category, source, message, detail in rows:
        max_id = max(max_id, _rid)
        out.append({"id": _rid, "ts": ts or "", "level": (level or "").lower(),
                    "category": (category or "").lower(), "source": source or "",
                    "message": message or "", "detail": detail or ""})
    return out, max_id


def _current_max_id(db: Path) -> int:
    if not db.exists():
        return 0
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        row = con.execute("SELECT COALESCE(MAX(rowid), 0) FROM diagnostics").fetchone()
        con.close()
        return row[0] if row else 0
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
        inject_event: Optional[Callable[[str], None]] = None,
        coordinator: Optional[Any] = None,
        clear_port: Optional[Callable[[], Awaitable[str]]] = None,
        run_command: Optional[Callable[[str], Awaitable[str]]] = None,
    ) -> None:
        self._get_root = get_project_root
        self._restart = restart_server
        self._notifier = notifier
        self._log = log
        # mk2: under self_heal autonomy, hand serious conditions to the agent as an
        # event so it can diagnose + remediate (not just notify/restart). Set by app.
        self._inject_event = inject_event
        # mk3: the self-healing Playbook. ``coordinator`` (a PlaybookCoordinator, or
        # None on a bare/test watchdog) persists issues/remedies/incidents and learns
        # what helps; ``clear_port``/``run_command`` are remedy actuators the app
        # provides. When the coordinator is None the watchdog keeps its legacy reflexes.
        self._pb = coordinator
        self._clear_port = clear_port
        self._run_command = run_command
        self._open_incidents: list[dict[str, Any]] = []   # remedies awaiting verification
        self._doc_last: dict[str, float] = {}             # per-issue last documented (throttle)
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
        # Playbook: resolve/fail any remedy we applied that is now in its
        # verification window (did the condition actually clear?).
        await self._verify_open_incidents(cfg, health, new_rows)

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
            if self._seen_running:
                if self._pb is not None:
                    # Route recovery through the Playbook (record + ranked remedy +
                    # verify + learn). Gating reproduces the legacy rule for restart.
                    await self._remediate(cfg, key="server_down", label="Server down",
                                          kind="builtin", trigger={"health": health})
                elif (cfg.get("auto_restart")
                        and cfg.get("autonomy") in ("auto_restart", "self_heal")):
                    await self._attempt_restart(cfg)
        # health == "unknown" (probe indeterminate): take no action this tick.

    async def _attempt_restart(self, cfg: dict[str, Any]) -> bool:
        """Restart the server with backoff + a crash-loop guard. Returns True if a
        restart was actually issued, False if the crash-loop guard paused it."""
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
            if self._pb is not None:
                # Document the crash-loop as its own issue for visibility.
                try:
                    self._pb.record("crash_loop", "Server crash-loop", "builtin")
                except Exception:
                    pass
            self._maybe_inject(
                cfg, "Server crash-loop",
                "The server has been restarted repeatedly and keeps dying; auto-restart "
                "is paused. Read the diagnostics and server logs, find the root cause, fix "
                "it, then restart the server.")
            return False
        await asyncio.sleep(max(0, int(cfg.get("restart_backoff_seconds") or 0)))
        self._restart_times.append(time.monotonic())
        self._record("auto-restarting the server")
        try:
            msg = await self._restart()
        except Exception as e:
            msg = f"restart failed: {type(e).__name__}: {e}"
        await self._notify(cfg, "Auto-restart", msg)
        return True

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
                if self._pb is not None:
                    await self._remediate(cfg, key="port_zombie",
                                          label="Port held by a zombie", kind="builtin",
                                          trigger={"port": PORT, "health": health})

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
        specs = [
            ("disk", "resource_disk", disk.get("percent"),
             int(cfg.get("disk_percent_threshold") or 0), "Disk almost full"),
            ("memory", "resource_memory", mem.get("percent"),
             int(cfg.get("mem_percent_threshold") or 0), "Memory pressure high"),
            ("cpu", "resource_cpu", snap.get("cpu_percent"),
             int(cfg.get("cpu_percent_threshold") or 0), "CPU sustained high"),
        ]
        for metric, key, value, threshold, title in specs:
            if await self._resource_alert(cfg, metric, value, threshold, title):
                if self._pb is not None:
                    await self._remediate(cfg, key=key, label=title, kind="builtin",
                                          trigger={"metric": metric, "value": value,
                                                   "threshold": threshold})

    async def _resource_alert(self, cfg: dict[str, Any], metric: str,
                              value: Optional[float], threshold: int, title: str) -> bool:
        """Alert (deduped hourly) when a metric crosses its threshold. Returns True
        if it alerted this tick (so the caller can also kick off remediation)."""
        if not threshold or value is None or value < threshold:
            return False
        now = time.monotonic()
        if now - self._res_alert_at.get(metric, 0.0) < 3600:
            return False
        self._res_alert_at[metric] = now
        await self._notify(cfg, title, f"{metric} at {value}% (threshold {threshold}%).")
        return True

    async def _handle_diagnostics(self, cfg: dict[str, Any], rows: list[dict[str, Any]]) -> None:
        alarms = load_alarms()
        errors = 0
        for diag in rows:
            if diag["level"] == "error":
                errors += 1
            for rule in alarms:
                if _matches(rule, diag):
                    await self._fire_alarm(cfg, rule, diag)
            # Playbook: learn from each error independently of user alarm rules —
            # cluster it into an issue so a remedy can be recorded + (once it recurs)
            # an explicit trigger programmed.
            if self._pb is not None and diag["level"] == "error":
                await self._remediate_diagnostic(cfg, diag)
        # Error-rate threshold: too many new errors in one interval → alert (deduped hourly).
        thr = int(cfg.get("error_rate_threshold") or 0)
        if thr and errors >= thr:
            now = time.monotonic()
            if now - self._rate_alert_at > 600:
                self._rate_alert_at = now
                await self._notify(cfg, "Error spike",
                                   f"{errors} new errors in the last interval (threshold {thr}).")
                if self._pb is not None:
                    await self._remediate(cfg, key="error_spike", label="Error spike",
                                          kind="builtin",
                                          trigger={"errors": errors, "threshold": thr})
                else:
                    self._maybe_inject(
                        cfg, "Error spike",
                        f"{errors} new errors arrived in the last interval. Investigate the "
                        "diagnostics and address the underlying cause.")

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

    # ── playbook: the self-healing loop ─────────────────────────────────
    def _open_keys(self) -> set[str]:
        return {e["key"] for e in self._open_incidents}

    def _verify_window(self, cfg: dict[str, Any]) -> float:
        return float(max(45, 3 * max(5, int(cfg.get("interval_seconds") or 20))))

    async def _remediate(self, cfg: dict[str, Any], *, key: str, label: str, kind: str,
                         trigger: dict[str, Any], match: Optional[dict[str, Any]] = None,
                         sig: Optional[str] = None) -> None:
        """Record an issue occurrence, pick the best-known remedy, and — if the
        configured remediation_mode + autonomy allow — apply it and queue it for
        verification. Otherwise just document it (throttled). Safe no-op without a
        coordinator."""
        if self._pb is None:
            return
        try:
            issue = self._pb.record(key, label, kind, match)
        except Exception:
            return
        try:
            if playbook.should_program_trigger(issue, int(cfg.get("program_trigger_after") or 3)):
                self._pb.program(key)
        except Exception:
            pass
        if key in self._open_keys():
            return  # already trying a remedy for this issue — wait for verification
        remedies = self._pb.remedies(key)
        remedy = playbook.best_remedy(remedies)
        mode = cfg.get("remediation_mode") or "safe_auto"
        autonomy = cfg.get("autonomy")
        threshold = float(cfg.get("remediation_confidence_threshold") or 0.6)
        if remedy is None:
            self._document(key, trigger, "")
            return
        conf = playbook.confidence(remedy)
        may = playbook.gate(remedy, mode=mode, autonomy=autonomy,
                            auto_restart=bool(cfg.get("auto_restart")),
                            conf=conf, threshold=threshold)
        if not may:
            self._document(key, trigger, remedy["id"])
            if mode != "document" and remedy["kind"] in (playbook.COMMAND, playbook.NOTE):
                self._maybe_inject(
                    cfg, label,
                    f"A remedy is on file ({remedy['kind']}: {(remedy.get('payload') or '')[:120]}) "
                    "but isn't approved to auto-run. Review and approve it if appropriate.")
            return
        incident_id = self._pb.open_incident(key, trigger)
        self._record(f"playbook: '{label}' → {remedy['kind']} (trying)")
        applied = await self._apply_remedy(cfg, remedy)
        if not applied:
            self._pb.close_incident(incident_id, "failed", remedy["id"], "remedy could not run")
            self._pb.tally(remedy["id"], False)
            return
        self._open_incidents.append({
            "key": key, "label": label, "incident_id": incident_id,
            "remedy_id": remedy["id"], "remedy_kind": remedy["kind"],
            "deadline": time.monotonic() + self._verify_window(cfg), "sig": sig,
        })

    async def _remediate_diagnostic(self, cfg: dict[str, Any], diag: dict[str, Any]) -> None:
        key = playbook.diagnostic_key(diag["level"], diag["category"], diag["message"])
        label = (f"{diag['category'] or 'app'} error: {diag['message'][:60]}").strip()
        match = {"contains": playbook.keyword(diag["message"]),
                 "level": diag["level"], "category": diag["category"]}
        trigger = {"level": diag["level"], "category": diag["category"],
                   "message": diag["message"][:200], "source": diag.get("source", "")}
        await self._remediate(cfg, key=key, label=label, kind="diagnostic",
                              trigger=trigger, match=match, sig=_signature(diag))

    def _document(self, key: str, trigger: dict[str, Any], remedy_id: str) -> None:
        """Log an incident we are NOT acting on (document-only / blocked remedy),
        throttled to at most once per 5 min per issue so it never floods."""
        now = time.monotonic()
        if now - self._doc_last.get(key, 0.0) < 300:
            return
        self._doc_last[key] = now
        try:
            iid = self._pb.open_incident(key, trigger)
            self._pb.close_incident(iid, "documented", remedy_id)
        except Exception:
            pass

    async def _apply_remedy(self, cfg: dict[str, Any], remedy: dict[str, Any]) -> bool:
        """Execute one remedy via its actuator. Returns True if it actually ran."""
        kind = remedy.get("kind")
        if kind == playbook.RESTART:
            return await self._attempt_restart(cfg)
        if kind == playbook.CLEAR_PORT:
            if self._clear_port is None:
                return False
            try:
                await self._clear_port()
            except Exception:
                return False
            return True
        if kind == playbook.ESCALATE:
            self._maybe_inject(cfg, remedy.get("issue_label") or "Recurring issue",
                               "Diagnose and remediate this recurring issue autonomously.")
            return True
        if kind == playbook.NOTIFY:
            return True  # the detection site already notified; nothing else to do
        if kind == playbook.COMMAND:
            if self._run_command is None or not (remedy.get("payload") or "").strip():
                return False
            try:
                await self._run_command(remedy["payload"])
            except Exception:
                return False
            return True
        return False

    async def _verify_open_incidents(self, cfg: dict[str, Any], health: str,
                                     new_rows: list[dict[str, Any]]) -> None:
        """For every remedy we applied and are watching, decide if the condition
        cleared (remedy helped) or the window expired (it didn't), record the
        outcome, and learn. Diagnostic incidents key off whether the same error
        recurs during the window."""
        if self._pb is None or not self._open_incidents:
            return
        now = time.monotonic()
        sigs = {_signature(d) for d in new_rows}
        still: list[dict[str, Any]] = []
        for e in self._open_incidents:
            expired = now >= e["deadline"]
            outcome: Optional[bool] = None
            if e.get("sig") is not None:           # diagnostic-driven
                if e["sig"] in sigs:
                    outcome = False                # recurred → didn't hold
                elif expired:
                    outcome = True                 # quiet through the window → helped
            else:                                  # condition-driven
                if self._incident_cleared(cfg, e, health):
                    outcome = True
                elif expired:
                    outcome = False
            if outcome is None:
                still.append(e)
                continue
            if outcome:
                self._pb.close_incident(e["incident_id"], "resolved", e["remedy_id"])
                self._pb.tally(e["remedy_id"], True)
                self._record(f"playbook: '{e['label']}' resolved by {e['remedy_kind']}")
            else:
                self._pb.close_incident(e["incident_id"], "failed", e["remedy_id"])
                self._pb.tally(e["remedy_id"], False)
                self._record(f"playbook: '{e['label']}' NOT fixed by {e['remedy_kind']}")
                self._maybe_inject(
                    cfg, e["label"],
                    f"The remedy '{e['remedy_kind']}' did not clear this; investigate + fix it.")
        self._open_incidents = still

    def _incident_cleared(self, cfg: dict[str, Any], e: dict[str, Any], health: str) -> bool:
        key = e["key"]
        if key in ("server_down", "crash_loop", "error_spike"):
            return health == "running"
        if key == "port_zombie":
            return health == "running" or not sysmetrics.port_in_use(PORT)
        if key.startswith("resource_"):
            res = self._last_resources or {}
            if key == "resource_disk":
                val, thr = (res.get("disk") or {}).get("percent"), int(cfg.get("disk_percent_threshold") or 0)
            elif key == "resource_memory":
                val, thr = (res.get("memory") or {}).get("percent"), int(cfg.get("mem_percent_threshold") or 0)
            else:
                val, thr = res.get("cpu_percent"), int(cfg.get("cpu_percent_threshold") or 0)
            if val is None or not thr:
                return True
            return val < thr
        return health == "running"

    # ── helpers ─────────────────────────────────────────────────────────
    def _maybe_inject(self, cfg: dict[str, Any], title: str, body: str) -> None:
        """Hand a serious event to the agent — ONLY under ``self_heal`` autonomy,
        so notify/auto_restart users never get autonomous agent turns."""
        if self._inject_event is None or cfg.get("autonomy") != "self_heal":
            return
        try:
            self._inject_event(f"{title}: {body}")
        except Exception:
            pass

    async def _notify(self, cfg: dict[str, Any], title: str, message: str,
                      channels: Optional[list[str]] = None) -> None:
        self._record(f"{title}: {message[:80]}")
        await self._notifier.notify(title, message, channels or cfg.get("channels") or ["desktop"])

    def _record(self, what: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self._reactions.append(f"[{stamp}] {what}")

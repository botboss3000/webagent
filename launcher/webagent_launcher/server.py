"""webagent server subprocess controller.

Spawns `uv run python run.py` inside the configured project directory,
streams stdout/stderr, polls the HTTP port for readiness, and supports
clean shutdown + force-kill of stale port-8080 listeners.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import httpx
import psutil

WEBAGENT_PORT = 8080
WEBAGENT_HOST = "127.0.0.1"
HEALTH_URL = f"http://{WEBAGENT_HOST}:{WEBAGENT_PORT}/"
MAX_LOG_LINES = 500

# ── Auto-restart watchdog tuning ───────────────────────────────────────────
# When the server exits without us asking (crash / disconnect), relaunch it.
# A run that stays up at least this long is "stable" and forgives prior crashes
# so an occasional crash always gets restarted at full speed.
AUTO_RESTART_STABLE_SEC = 30.0
# Consecutive *rapid* crashes before we stop trying (avoid a tight crash loop
# on a genuinely broken build/config).
AUTO_RESTART_MAX_TRIES = 5
# Exponential backoff between rapid retries: 1s, 2s, 4s, 8s, 16s … capped.
AUTO_RESTART_BASE_DELAY = 1.0
AUTO_RESTART_MAX_DELAY = 30.0

# ── Health-check (hung-server) tuning ──────────────────────────────────────
# While the server is "running" we poll HEALTH_URL. A process that is alive but
# wedged (event loop blocked) never trips the process-exit watchdog, so this
# catches it. Require several *consecutive* misses so a momentary blip under
# load can't cause a needless restart.
HEALTH_POLL_INTERVAL = 10.0   # seconds between probes while running
HEALTH_PROBE_TIMEOUT = 5.0    # per-probe HTTP timeout (a hung server won't answer)
HEALTH_FAIL_THRESHOLD = 3     # consecutive misses ⇒ treat as hung (~30s window)

# Windows-specific subprocess flags
if sys.platform == "win32":
    _CREATE_NEW_PROCESS_GROUP = subprocess.CREATE_NEW_PROCESS_GROUP
    _CREATE_NO_WINDOW = 0x08000000
    _SPAWN_FLAGS = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
else:
    _SPAWN_FLAGS = 0


@dataclass
class ServerState:
    status: str = "stopped"  # stopped | starting | running | stopping | error
    pid: Optional[int] = None
    started_at: Optional[float] = None
    last_error: Optional[str] = None
    log: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))

    @property
    def uptime_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        return time.time() - self.started_at

    def uptime_str(self) -> str:
        s = int(self.uptime_seconds)
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m {s % 60}s"
        h = s // 3600
        m = (s % 3600) // 60
        return f"{h}h {m}m"


class ServerController:
    """Async controller around the uvicorn subprocess."""

    def __init__(
        self,
        project_dir: Path,
        on_state_change: Optional[Callable[[ServerState], None]] = None,
        on_log_line: Optional[Callable[[str], None]] = None,
        auto_restart: bool = True,
        health_check: bool = True,
    ) -> None:
        self.project_dir = project_dir
        self.state = ServerState()
        self.auto_restart = auto_restart
        self.health_check = health_check
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._on_state_change = on_state_change or (lambda _s: None)
        self._on_log_line = on_log_line or (lambda _l: None)
        # Watchdog bookkeeping.
        #   _intentional_stop → set while WE asked the process to exit (stop /
        #     restart / reset); the reader uses it instead of reading mutable
        #     status, which races with stop()'s own "stopped" write.
        #   _restart_failures → consecutive rapid crashes / hangs (drives backoff).
        #   _auto_restart_task → the pending relaunch timer, if any.
        #   _health_task → the long-lived health-poll loop (created on first start).
        self._intentional_stop = False
        self._restart_failures = 0
        self._auto_restart_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None

    # ── state notification helper ──────────────────────────────────────
    def _emit(self) -> None:
        try:
            self._on_state_change(self.state)
        except Exception:  # never let a UI callback kill the controller
            pass

    def _append_log(self, line: str) -> None:
        self.state.log.append(line)
        try:
            self._on_log_line(line)
        except Exception:
            pass

    # ── port helpers ──────────────────────────────────────────────────
    @staticmethod
    def port_in_use() -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                s.connect((WEBAGENT_HOST, WEBAGENT_PORT))
                return True
            except OSError:
                return False

    @staticmethod
    def find_port_pids() -> list[int]:
        pids: set[int] = set()
        try:
            for conn in psutil.net_connections(kind="inet"):
                if conn.laddr and conn.laddr.port == WEBAGENT_PORT and conn.pid:
                    pids.add(conn.pid)
        except (psutil.AccessDenied, OSError):
            pass
        return sorted(pids)

    @classmethod
    def kill_port_pids(cls) -> int:
        killed = 0
        for pid in cls.find_port_pids():
            try:
                p = psutil.Process(pid)
                p.kill()
                killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return killed

    # ── readiness polling ──────────────────────────────────────────────
    @staticmethod
    async def wait_until_ready(timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.monotonic() < deadline:
                try:
                    r = await client.get(HEALTH_URL)
                    if r.status_code < 500:
                        return True
                except (httpx.HTTPError, OSError):
                    pass
                await asyncio.sleep(0.4)
        return False

    # ── start / stop ──────────────────────────────────────────────────
    async def start(self, *, _auto: bool = False) -> None:
        if self.state.status in ("starting", "running"):
            self._append_log("[launcher] server already running")
            return

        # Any start supersedes a queued auto-restart. A *manual* start (user
        # pressed Launch / Restart, or a reset finished) is also a clean slate
        # for the crash counter so the watchdog is armed at full strength again.
        self._cancel_auto_restart()
        self._intentional_stop = False
        if not _auto:
            self._restart_failures = 0

        if not (self.project_dir / "run.py").exists():
            self.state.status = "error"
            self.state.last_error = f"run.py not found in {self.project_dir}"
            self._append_log(f"[launcher] ERROR: {self.state.last_error}")
            self._emit()
            return

        # Kill stale port listeners (mirrors webagent.bat behavior)
        stale = self.kill_port_pids()
        if stale:
            self._append_log(f"[launcher] killed {stale} stale process(es) on port {WEBAGENT_PORT}")
            await asyncio.sleep(0.5)

        self.state.status = "starting"
        self.state.started_at = None
        self.state.last_error = None
        self.state.pid = None
        self._emit()

        # Resolve interpreter: prefer uv (handles .venv automatically)
        cmd = self._resolve_command()
        self._append_log(f"[launcher] $ {' '.join(cmd)}")
        self._append_log(f"[launcher] cwd: {self.project_dir}")

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.project_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                creationflags=_SPAWN_FLAGS,
            )
        except FileNotFoundError as e:
            self.state.status = "error"
            self.state.last_error = f"Failed to spawn: {e}"
            self._append_log(f"[launcher] ERROR: {self.state.last_error}")
            self._emit()
            return

        self.state.pid = self._proc.pid
        self._reader_task = asyncio.create_task(self._read_output())
        asyncio.create_task(self._await_ready())
        self._ensure_health_monitor()
        self._emit()

    def _resolve_command(self) -> list[str]:
        """Pick the best command to run the server."""
        # 1. Project .venv python (Windows: .venv\Scripts\python.exe)
        if sys.platform == "win32":
            venv_py = self.project_dir / ".venv" / "Scripts" / "python.exe"
        else:
            venv_py = self.project_dir / ".venv" / "bin" / "python"
        if venv_py.exists():
            return [str(venv_py), "run.py"]

        # 2. uv on PATH
        from shutil import which
        uv = which("uv")
        if uv:
            return [uv, "run", "python", "run.py"]

        # 3. system python
        return [sys.executable, "run.py"]

    async def _read_output(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    text = line.decode("utf-8", errors="replace").rstrip()
                except Exception:
                    text = repr(line)
                self._append_log(text)
        except asyncio.CancelledError:
            pass
        finally:
            if self._proc:
                rc = await self._proc.wait()
                self._append_log(f"[launcher] server exited with code {rc}")
                # Decide intentional-vs-crash from our own flag, not from
                # state.status: stop() may have already flipped status to
                # "stopped" by the time we get here (both coroutines wake on
                # the same process exit), which would otherwise look like a
                # crash and trigger a bogus restart.
                intentional = self._intentional_stop
                ran_seconds = self.state.uptime_seconds
                if intentional:
                    self.state.status = "stopped"
                elif rc != 0:
                    self.state.status = "error"
                    self.state.last_error = f"exit code {rc}"
                else:
                    self.state.status = "stopped"
                self.state.pid = None
                self.state.started_at = None
                self._emit()
                # Crash / unexpected disconnect → relaunch (with backoff).
                if not intentional and self.auto_restart:
                    self._schedule_auto_restart(ran_seconds)

    async def _await_ready(self) -> None:
        if await self.wait_until_ready(timeout=45.0):
            if self.state.status == "starting":
                self.state.status = "running"
                self.state.started_at = time.time()
                self._append_log(f"[launcher] server READY on http://localhost:{WEBAGENT_PORT}")
                self._emit()
        else:
            if self.state.status == "starting":
                self.state.last_error = "timed out waiting for HTTP readiness"
                self._append_log("[launcher] WARN: readiness probe timed out (server may still be starting)")
                self._emit()

    async def stop(self, timeout: float = 8.0) -> None:
        # We're asking the server to exit — disarm the watchdog so the reader
        # doesn't read this as a crash, and drop any queued relaunch.
        self._intentional_stop = True
        self._cancel_auto_restart()

        if self._proc is None or self._proc.returncode is not None:
            self.state.status = "stopped"
            self._emit()
            return

        self.state.status = "stopping"
        self._append_log("[launcher] stopping server...")
        self._emit()

        try:
            if sys.platform == "win32":
                # CTRL_BREAK_EVENT lets uvicorn shut down cleanly
                self._proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self._proc.terminate()
        except (ProcessLookupError, OSError):
            pass

        try:
            await asyncio.wait_for(self._proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._append_log("[launcher] graceful stop timed out, killing...")
            try:
                self._proc.kill()
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass

        # Final cleanup of any zombie port listeners
        self.kill_port_pids()

        self.state.status = "stopped"
        self.state.pid = None
        self.state.started_at = None
        self._emit()

    async def restart(self) -> None:
        await self.stop()
        await asyncio.sleep(0.5)
        await self.start()

    # ── auto-restart watchdog ──────────────────────────────────────────
    def cancel_auto_restart(self) -> None:
        """Public hook: drop any queued relaunch (e.g. before a DB/venv reset
        so the watchdog can't race a destructive wipe)."""
        self._cancel_auto_restart()

    def _cancel_auto_restart(self) -> None:
        # Null the handle *before* cancelling so a relaunch coroutine that
        # cancels via start() can't end up cancelling itself.
        task = self._auto_restart_task
        self._auto_restart_task = None
        if task is not None and not task.done():
            task.cancel()

    def _register_failure(self, ran_seconds: float) -> Optional[float]:
        """Bump the crash/hang counter and return the backoff delay before the
        next relaunch — or None if we've hit the give-up cap.

        Shared by both failure paths (process exit + unresponsive) so they share
        one backoff schedule and one crash-loop ceiling."""
        # A run that lasted past the stability window means the server was
        # healthy and just died — forgive earlier rapid failures.
        if ran_seconds >= AUTO_RESTART_STABLE_SEC:
            self._restart_failures = 0
        self._restart_failures += 1
        if self._restart_failures > AUTO_RESTART_MAX_TRIES:
            return None
        return min(
            AUTO_RESTART_MAX_DELAY,
            AUTO_RESTART_BASE_DELAY * (2 ** (self._restart_failures - 1)),
        )

    def _giveup(self, reason: str) -> None:
        """Stop trying to relaunch and surface an error for the user to fix."""
        self._append_log(
            f"[launcher] auto-restart gave up after {AUTO_RESTART_MAX_TRIES} "
            f"{reason} — fix the error above, then press Launch"
        )
        self.state.status = "error"
        self.state.last_error = f"auto-restart aborted ({reason})"
        self._emit()

    def _schedule_auto_restart(self, ran_seconds: float) -> None:
        """Queue a relaunch after an unexpected *exit*, with backoff + a cap so a
        permanently-broken server doesn't spin in a tight crash loop."""
        delay = self._register_failure(ran_seconds)
        if delay is None:
            self._giveup("rapid crashes")
            return
        self._append_log(
            f"[launcher] server disconnected — auto-restart in {delay:.0f}s "
            f"(attempt {self._restart_failures}/{AUTO_RESTART_MAX_TRIES})"
        )
        self._cancel_auto_restart()  # never stack two timers
        self._auto_restart_task = asyncio.create_task(self._auto_restart_after(delay))

    async def _auto_restart_after(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        # Clear our own handle first so start()'s cancel-pending is a no-op for
        # this (now-running) task.
        self._auto_restart_task = None
        # Bail if the user / a reset already moved the server in the meantime.
        if self.state.status in ("starting", "running", "stopping"):
            return
        await self.start(_auto=True)

    # ── health check (hung-server) watchdog ────────────────────────────
    def _ensure_health_monitor(self) -> None:
        """Spawn the long-lived health-poll loop once (it persists across
        start/stop cycles and simply idles while the server isn't running)."""
        if self._health_task is None or self._health_task.done():
            self._health_task = asyncio.create_task(self._health_monitor())

    async def _probe_health(self) -> bool:
        """One HTTP probe of the server root. False on any error/timeout —
        which is exactly what a wedged (alive-but-unresponsive) server gives."""
        try:
            async with httpx.AsyncClient(timeout=HEALTH_PROBE_TIMEOUT) as client:
                r = await client.get(HEALTH_URL)
                return r.status_code < 500
        except (httpx.HTTPError, OSError):
            return False

    async def _health_monitor(self) -> None:
        """While the server is 'running', poll it. HEALTH_FAIL_THRESHOLD misses
        in a row ⇒ the process is alive but hung ⇒ force a restart. Idle (no
        probes) in every other state so it never fights start/stop/restart."""
        consecutive = 0
        while True:
            try:
                await asyncio.sleep(HEALTH_POLL_INTERVAL)
            except asyncio.CancelledError:
                return
            try:
                if not self.health_check or self.state.status != "running":
                    consecutive = 0
                    continue
                if await self._probe_health():
                    consecutive = 0
                    continue
                consecutive += 1
                self._append_log(
                    f"[launcher] health check missed "
                    f"({consecutive}/{HEALTH_FAIL_THRESHOLD})"
                )
                if consecutive >= HEALTH_FAIL_THRESHOLD:
                    consecutive = 0
                    await self._restart_unresponsive()
            except asyncio.CancelledError:
                return
            except Exception:
                # Never let a transient error kill the monitor; try again next tick.
                consecutive = 0

    async def _restart_unresponsive(self) -> None:
        """Bring a hung-but-alive server down and relaunch it, counting the
        cycle against the same crash-loop guard as a real crash."""
        # If the process actually exited, the process-exit watchdog owns the
        # restart — don't double-act. Also bail if we're no longer 'running'.
        if self._proc is None or self._proc.returncode is not None:
            return
        if self.state.status != "running":
            return

        window = HEALTH_POLL_INTERVAL * HEALTH_FAIL_THRESHOLD
        self._append_log(
            f"[launcher] server unresponsive (~{window:.0f}s without a health "
            "reply) — restarting"
        )
        delay = self._register_failure(self.state.uptime_seconds)

        # stop() sets _intentional_stop so the reader won't ALSO schedule an
        # exit-restart, and force-kills if the hung process ignores CTRL-BREAK.
        await self.stop()

        if delay is None:
            # Over the cap. Let the reader's finally finish (it sets "stopped"),
            # then deterministically mark "error" so the UI shows the failure.
            # CancelledError (app teardown) is a BaseException — let it bubble to
            # the monitor's handler; only swallow ordinary errors here.
            if self._reader_task is not None:
                try:
                    await self._reader_task
                except Exception:
                    pass
            self._giveup("unresponsive/crash cycles")
            return

        if delay:
            await asyncio.sleep(delay)
        # _auto=True keeps the failure counter (don't reset on a watchdog relaunch).
        await self.start(_auto=True)

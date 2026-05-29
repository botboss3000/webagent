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
    ) -> None:
        self.project_dir = project_dir
        self.state = ServerState()
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._on_state_change = on_state_change or (lambda _s: None)
        self._on_log_line = on_log_line or (lambda _l: None)

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
    async def start(self) -> None:
        if self.state.status in ("starting", "running"):
            self._append_log("[launcher] server already running")
            return

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
                if self.state.status == "stopping":
                    self.state.status = "stopped"
                elif rc != 0:
                    self.state.status = "error"
                    self.state.last_error = f"exit code {rc}"
                else:
                    self.state.status = "stopped"
                self.state.pid = None
                self.state.started_at = None
                self._emit()

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

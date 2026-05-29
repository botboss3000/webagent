"""webAgent server subprocess controller — Android/Linux flavor.

Spawns `python run.py` from the project's venv (or system python).
Tracks PID, streams stdout/stderr to a callback, polls the HTTP port
for readiness. No third-party deps (stdlib only) so it works before
the Android-Ubuntu environment has anything installed.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

WEBAGENT_PORT = 8080
WEBAGENT_HOST = "127.0.0.1"
HEALTH_URL = f"http://{WEBAGENT_HOST}:{WEBAGENT_PORT}/"
MAX_LOG_LINES = 800


@dataclass
class ServerState:
    status: str = "stopped"  # stopped | starting | running | stopping | error
    pid: Optional[int] = None
    started_at: Optional[float] = None
    last_error: Optional[str] = None
    log: deque = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))

    @property
    def uptime_seconds(self) -> float:
        return time.time() - self.started_at if self.started_at else 0.0

    def uptime_str(self) -> str:
        s = int(self.uptime_seconds)
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m {s % 60}s"
        return f"{s // 3600}h {(s % 3600) // 60}m"


class ServerController:
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

    def _emit(self) -> None:
        try:
            self._on_state_change(self.state)
        except Exception:
            pass

    def _append_log(self, line: str) -> None:
        self.state.log.append(line)
        try:
            self._on_log_line(line)
        except Exception:
            pass

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
        """Find PIDs holding port 8080 by scanning /proc/net/tcp.

        Works on Android-Linux without psutil.
        """
        port_hex = f"{WEBAGENT_PORT:04X}"
        inodes: set[str] = set()
        for path in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(path) as f:
                    next(f, None)  # header
                    for line in f:
                        cols = line.split()
                        if len(cols) < 10:
                            continue
                        local = cols[1]
                        state = cols[3]
                        if state != "0A":  # LISTEN
                            continue
                        if not local.endswith(":" + port_hex):
                            continue
                        inodes.add(cols[9])
            except OSError:
                continue

        if not inodes:
            return []

        pids: set[int] = set()
        try:
            for pid_dir in os.listdir("/proc"):
                if not pid_dir.isdigit():
                    continue
                fd_dir = f"/proc/{pid_dir}/fd"
                try:
                    for fd in os.listdir(fd_dir):
                        try:
                            target = os.readlink(f"{fd_dir}/{fd}")
                        except OSError:
                            continue
                        if target.startswith("socket:["):
                            inode = target[len("socket:["):-1]
                            if inode in inodes:
                                pids.add(int(pid_dir))
                                break
                except (OSError, PermissionError):
                    continue
        except OSError:
            pass
        return sorted(pids)

    @classmethod
    def kill_port_pids(cls) -> int:
        killed = 0
        for pid in cls.find_port_pids():
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError, OSError):
                continue
        return killed

    @staticmethod
    async def wait_until_ready(timeout: float = 45.0) -> bool:
        deadline = time.monotonic() + timeout
        loop = asyncio.get_event_loop()
        while time.monotonic() < deadline:
            try:
                def _probe() -> bool:
                    try:
                        with urllib.request.urlopen(HEALTH_URL, timeout=2.0) as r:
                            return r.status < 500
                    except urllib.error.HTTPError as e:
                        return e.code < 500
                    except (urllib.error.URLError, OSError):
                        return False

                ok = await loop.run_in_executor(None, _probe)
                if ok:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return False

    def resolve_command(self) -> list[str]:
        """Pick the best python for running run.py inside the proot."""
        venv_py = self.project_dir / "venv" / "bin" / "python"
        if venv_py.exists():
            return [str(venv_py), "run.py"]
        venv_py = self.project_dir / ".venv" / "bin" / "python"
        if venv_py.exists():
            return [str(venv_py), "run.py"]
        return ["python3", "run.py"]

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

        stale = self.kill_port_pids()
        if stale:
            self._append_log(f"[launcher] killed {stale} stale process(es) on port {WEBAGENT_PORT}")
            await asyncio.sleep(0.5)

        self.state.status = "starting"
        self.state.started_at = None
        self.state.last_error = None
        self.state.pid = None
        self._emit()

        cmd = self.resolve_command()
        self._append_log(f"[launcher] $ {' '.join(cmd)}")
        self._append_log(f"[launcher] cwd: {self.project_dir}")

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.project_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        except (FileNotFoundError, OSError) as e:
            self.state.status = "error"
            self.state.last_error = f"Failed to spawn: {e}"
            self._append_log(f"[launcher] ERROR: {self.state.last_error}")
            self._emit()
            return

        self.state.pid = self._proc.pid
        self._reader_task = asyncio.create_task(self._read_output())
        asyncio.create_task(self._await_ready())
        self._emit()

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
        if await self.wait_until_ready(timeout=60.0):
            if self.state.status == "starting":
                self.state.status = "running"
                self.state.started_at = time.time()
                self._append_log(f"[launcher] server READY on http://localhost:{WEBAGENT_PORT}")
                self._emit()
        else:
            if self.state.status == "starting":
                self.state.last_error = "timed out waiting for HTTP readiness"
                self._append_log("[launcher] WARN: readiness probe timed out")
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
            os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            try:
                self._proc.terminate()
            except (ProcessLookupError, OSError):
                pass

        try:
            await asyncio.wait_for(self._proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._append_log("[launcher] graceful stop timed out, killing...")
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                try:
                    self._proc.kill()
                except (ProcessLookupError, OSError):
                    pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=4.0)
            except asyncio.TimeoutError:
                pass

        self.kill_port_pids()

        self.state.status = "stopped"
        self.state.pid = None
        self.state.started_at = None
        self._emit()

    async def restart(self) -> None:
        await self.stop()
        await asyncio.sleep(0.5)
        await self.start()

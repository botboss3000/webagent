"""Local interactive terminal engine for the Server-Manager TUI.

Lets the manager agent open and **drive interactive terminal programs** on the
machine the TUI runs on — Claude Code, Gemini CLI, Aider, a REPL, any CLI. This
is the TUI-local counterpart to the web app's `app/api/terminal.py`: same design
(a per-session **background pump** owns the PTY's sole reader and drains output
into a scrollback buffer so the program never blocks and the agent can read the
screen at any time; `wait_idle` waits for the program to *react then settle* so
it doesn't false-settle on cold start), but standalone — no FastAPI/WebSocket,
just an in-process session registry the `terminal_*` tools call.

Cross-platform: stdlib `pty` on Unix, `pywinpty` on Windows (a TUI extra). The
engine runs inside the Textual app's asyncio loop.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
import time
import uuid
from typing import Dict, List, Optional, Tuple

IS_WINDOWS = sys.platform.startswith("win")
SCROLLBACK_BYTES = 64 * 1024

# Strip ANSI/VT control sequences so the agent reads clean text (the raw bytes
# keep their structure in scrollback; this only cleans the agent-facing view).
_ANSI_RE = re.compile(
    r"\x1B\[[0-?]*[ -/]*[@-~]|\x1B\].*?(?:\x07|\x1B\\)|\x1B[@-Z\\-_]|[\x00-\x08\x0B\x0C\x0E-\x1F]",
    re.DOTALL,
)

# Named keys → raw byte sequences (answering prompts, navigating menus, aborting).
_KEY_BYTES: Dict[str, bytes] = {
    "enter": b"\r", "return": b"\r", "tab": b"\t", "space": b" ",
    "esc": b"\x1b", "escape": b"\x1b", "backspace": b"\x7f", "delete": b"\x1b[3~",
    "up": b"\x1b[A", "down": b"\x1b[B", "right": b"\x1b[C", "left": b"\x1b[D",
    "home": b"\x1b[H", "end": b"\x1b[F", "pageup": b"\x1b[5~", "pagedown": b"\x1b[6~",
    "ctrl+c": b"\x03", "ctrl+d": b"\x04", "ctrl+z": b"\x1a", "ctrl+l": b"\x0c",
    "ctrl+a": b"\x01", "ctrl+e": b"\x05", "ctrl+u": b"\x15", "ctrl+r": b"\x12",
}


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class LocalTerminalSession:
    """One PTY-backed program running locally, driven by the agent."""

    def __init__(self) -> None:
        self._proc = None                      # (master_fd, pid) on Unix, PtyProcess on Windows
        self._scrollback = bytearray()
        self._pump_task: Optional[asyncio.Task] = None
        self._ended = False
        self.created_at = time.time()
        self.last_output_ts = time.monotonic()
        self.name = ""
        self.launch_command = ""

    # ── spawn ──
    def spawn(self, shell: Optional[str] = None) -> None:
        if shell is None:
            shell = self._default_shell()
        if IS_WINDOWS:
            try:
                from winpty import PtyProcess
            except ImportError as e:
                raise RuntimeError(
                    "interactive terminals need pywinpty on Windows — install it with "
                    "`pip install pywinpty` (it's a declared dependency)."
                ) from e
            self._proc = PtyProcess.spawn(shell)
            self._proc.setwinsize(40, 120)
        else:
            import fcntl
            import pty
            import struct
            import termios
            master_fd, slave_fd = pty.openpty()
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            pid = os.fork()
            if pid == 0:                        # child
                os.close(master_fd)
                os.setsid()
                os.dup2(slave_fd, 0); os.dup2(slave_fd, 1); os.dup2(slave_fd, 2)
                if slave_fd > 2:
                    os.close(slave_fd)
                os.execve(shell, [shell], os.environ)
            os.close(slave_fd)
            fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
            self._proc = (master_fd, pid)

    @staticmethod
    def _default_shell() -> str:
        if IS_WINDOWS:
            for c in ("pwsh.exe", "powershell.exe", "cmd.exe"):
                p = _which(c)
                if p:
                    return p
            return "powershell.exe"
        for c in ("/bin/bash", "/bin/sh"):
            if os.path.exists(c):
                return c
        return "/bin/sh"

    # ── pump + scrollback ──
    def start_pump(self) -> None:
        if self._pump_task is not None and not self._pump_task.done():
            return
        self._pump_task = asyncio.ensure_future(self._pump_loop())

    async def _pump_loop(self) -> None:
        try:
            while True:
                data = await self._read_output()
                if data is None:
                    self._ended = True
                    return
                if not data:
                    continue
                self.last_output_ts = time.monotonic()
                self._append(data)
        except asyncio.CancelledError:
            pass
        except Exception:
            self._ended = True

    def _append(self, data: bytes) -> None:
        self._scrollback.extend(data)
        overflow = len(self._scrollback) - SCROLLBACK_BYTES
        if overflow > 0:
            del self._scrollback[:overflow]

    async def _read_output(self) -> Optional[bytes]:
        if self._proc is None:
            return None
        if IS_WINDOWS:
            return await self._read_windows()
        return await self._read_unix()

    async def _read_unix(self) -> Optional[bytes]:
        master_fd, _ = self._proc
        loop = asyncio.get_event_loop()
        fut = loop.create_future()

        def _on_readable():
            try:
                data = os.read(master_fd, 65536)
                fut.set_result(data or None)
            except OSError as e:
                fut.set_exception(e)

        loop.add_reader(master_fd, _on_readable)
        try:
            return await fut
        finally:
            try:
                loop.remove_reader(master_fd)
            except (ValueError, NotImplementedError):
                pass

    async def _read_windows(self) -> Optional[bytes]:
        loop = asyncio.get_event_loop()
        proc = self._proc

        def _read():
            try:
                data = proc.read(65536)
                if not data:
                    return None
                return data.encode("utf-8", "replace") if isinstance(data, str) else data
            except (EOFError, OSError):
                return None

        return await loop.run_in_executor(_get_executor(), _read)

    # ── I/O the tools use ──
    def write_input(self, data: bytes) -> None:
        if self._proc is None:
            return
        try:
            if IS_WINDOWS:
                self._proc.write(data.decode("utf-8", "replace") if isinstance(data, bytes) else data)
            else:
                os.write(self._proc[0], data)
        except (OSError, Exception):
            pass

    def send_text(self, text: str, enter: bool = False) -> None:
        if text:
            self.write_input(text.encode("utf-8"))
        if enter:
            self.write_input(b"\r")

    def send_keys(self, keys: List[str]) -> List[str]:
        unknown: List[str] = []
        for k in keys:
            seq = _KEY_BYTES.get(str(k).strip().lower())
            if seq is None:
                unknown.append(k)
            else:
                self.write_input(seq)
        return unknown

    def read_text(self, tail_chars: int = 4000, strip_ansi: bool = True) -> str:
        raw = bytes(self._scrollback).decode("utf-8", "replace")
        if strip_ansi:
            raw = _strip_ansi(raw)
        if tail_chars and len(raw) > tail_chars:
            raw = raw[-tail_chars:]
        return raw

    async def wait_idle(self, quiet_secs: float = 0.6, timeout: float = 30.0,
                        require_activity: bool = True) -> bool:
        """Wait until the program reacts and then goes quiet for `quiet_secs`
        (settled), or `timeout` elapses. `require_activity` avoids false-settling
        during cold start / the beat after a send."""
        start = time.monotonic()
        deadline = start + max(0.0, timeout)
        baseline = self.last_output_ts
        saw = False
        poll = min(0.1, quiet_secs / 2) if quiet_secs > 0 else 0.1
        while True:
            if self._ended or not self.is_alive:
                return True
            if self.last_output_ts > baseline:
                saw = True
            ready = saw or not require_activity
            if ready and (time.monotonic() - self.last_output_ts) >= quiet_secs:
                return True
            if time.monotonic() >= deadline:
                return saw or not require_activity
            await asyncio.sleep(poll)

    @property
    def is_alive(self) -> bool:
        if self._proc is None:
            return False
        if IS_WINDOWS:
            try:
                return self._proc.isalive()
            except Exception:
                return False
        _, pid = self._proc
        try:
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            return wpid == 0
        except (ChildProcessError, ProcessLookupError):
            return False

    @property
    def ended(self) -> bool:
        return self._ended

    def close(self) -> None:
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
        self._pump_task = None
        self._ended = True
        if self._proc is None:
            return
        try:
            if IS_WINDOWS:
                self._proc.close()
            else:
                master_fd, pid = self._proc
                try:
                    os.close(master_fd)
                except OSError:
                    pass
                try:
                    os.kill(pid, signal.SIGTERM)
                    os.waitpid(pid, os.WNOHANG)
                except (ProcessLookupError, ChildProcessError):
                    pass
        except Exception:
            pass
        self._proc = None


# ── module helpers ──

def _which(name: str) -> Optional[str]:
    import subprocess
    try:
        r = subprocess.run(["where", name], capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            return r.stdout.strip().splitlines()[0].strip()
    except Exception:
        pass
    return None


_EXECUTOR = None


def _get_executor():
    global _EXECUTOR
    if _EXECUTOR is None:
        import concurrent.futures
        # One blocking reader thread per live session, so size to the cap below.
        _EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=16, thread_name_prefix="tui-pty-read"
        )
    return _EXECUTOR


# ── in-process session registry ──

_SESSIONS: Dict[str, LocalTerminalSession] = {}
MAX_SESSIONS = 12


def _reap_dead() -> None:
    for sid, s in list(_SESSIONS.items()):
        if not s.is_alive:
            try:
                s.close()
            except Exception:
                pass
            del _SESSIONS[sid]


async def open_session(command: str = "", name: str = "") -> Tuple[str, LocalTerminalSession]:
    _reap_dead()
    if len(_SESSIONS) >= MAX_SESSIONS:
        raise RuntimeError(f"too many terminal sessions open (cap {MAX_SESSIONS}); close one first")
    sid = uuid.uuid4().hex
    sess = LocalTerminalSession()
    sess.spawn()
    sess.start_pump()
    sess.write_input(b"\r")
    sess.launch_command = (command or "").strip()
    sess.name = (name or "").strip()[:80] or (sess.launch_command[:80] if sess.launch_command else "terminal")
    _SESSIONS[sid] = sess
    if sess.launch_command:
        await sess.wait_idle(quiet_secs=0.5, timeout=12.0, require_activity=True)
        sess.send_text(sess.launch_command, enter=True)
    return sid, sess


def get_session(session_id: str) -> Optional[LocalTerminalSession]:
    _reap_dead()
    s = _SESSIONS.get(session_id)
    return s if (s and s.is_alive) else None


def list_sessions() -> List[dict]:
    _reap_dead()
    now = time.time()
    return [{
        "session_id": sid,
        "name": s.name,
        "launch_command": s.launch_command,
        "ended": s.ended,
        "age_secs": int(now - s.created_at),
    } for sid, s in _SESSIONS.items()]


def close_session(session_id: str) -> bool:
    s = _SESSIONS.pop(session_id, None)
    if s is None:
        return False
    try:
        s.close()
    except Exception:
        pass
    return True


def close_all() -> None:
    for sid in list(_SESSIONS):
        close_session(sid)

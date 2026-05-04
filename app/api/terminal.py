"""
Web-based terminal endpoint — real PTY shell accessible via WebSocket.

Connects a browser (xterm.js) to a real shell session on the server.
Cross-platform: uses os/pty on Unix, pywinpty on Windows.
"""

import asyncio
import concurrent.futures
import json
import logging
import os
import sys
import signal
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Persistent terminal session (survives page reloads) ──
_persistent_session: Optional["TerminalSession"] = None
_persistent_session_lock = asyncio.Lock()


async def get_or_create_session() -> "TerminalSession":
    """Return the one persistent shell session. Creates on first call.
    Re-spawns if the underlying process has exited (e.g. user typed 'exit').
    Lives until server shutdown. WebSocket attach/detach doesn't kill it."""
    global _persistent_session
    async with _persistent_session_lock:
        if _persistent_session is None or not _persistent_session.is_alive:
            if _persistent_session is not None:
                _persistent_session.close()
                logger.info("Re-spawning dead terminal session")
            _persistent_session = TerminalSession()
            _persistent_session.spawn()
            _persistent_session.write_input(b"\r")
            logger.info("Persistent terminal session created")
        return _persistent_session


async def close_persistent_session():
    """Close the session on server shutdown. Called from main.py shutdown."""
    global _persistent_session
    if _persistent_session is not None:
        _persistent_session.close()
        _persistent_session = None
        logger.info("Persistent terminal session closed")
    if IS_WINDOWS:
        _close_winpty_executor()


def _ws_client_is_loopback(host: Optional[str]) -> bool:
    """Allow browser WebSockets from loopback (incl. IPv4-mapped IPv6 on Windows)."""
    if host is None:
        return True
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    if host.startswith("::ffff:"):
        return host.removeprefix("::ffff:") == "127.0.0.1"
    return False

# ── Platform detection ──
IS_WINDOWS = sys.platform.startswith("win")

if IS_WINDOWS:
    try:
        import winpty  # noqa: F401
        _HAS_WINPTY = True
    except ImportError:
        _HAS_WINPTY = False
        logger.warning("winpty not installed — terminal WebSocket will return an error. Install with: pip install pywinpty")

    # Shared thread pool for Windows PTY reads — avoids per-call executor
    # that blocks event loop on task cancellation (pool.shutdown deadlock).
    _WINPTY_READER: Optional[concurrent.futures.ThreadPoolExecutor] = None

    def _get_winpty_executor() -> concurrent.futures.ThreadPoolExecutor:
        global _WINPTY_READER
        if _WINPTY_READER is None:
            _WINPTY_READER = concurrent.futures.ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="pty-read"
            )
        return _WINPTY_READER

    def _close_winpty_executor():
        global _WINPTY_READER
        if _WINPTY_READER is not None:
            _WINPTY_READER.shutdown(wait=False)
            _WINPTY_READER = None
else:
    _HAS_WINPTY = True  # Unix always works


class TerminalSession:
    """A single PTY-based shell session, one per WebSocket connection."""

    def __init__(self):
        self._process = None  # WinPty on Windows, (master_fd, pid) tuple on Unix
        self._reader_added = False

    # ── Platform-specific helpers ──

    def _spawn_unix(self, shell: str):
        """Unix: fork + pty."""
        import fcntl
        import pty
        import struct
        import termios

        master_fd, slave_fd = pty.openpty()

        # Set window size
        buf = struct.pack("HHHH", 40, 120, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, buf)

        pid = os.fork()

        if pid == 0:
            # Child
            os.close(master_fd)
            os.setsid()
            os.dup2(slave_fd, 0)
            os.dup2(slave_fd, 1)
            os.dup2(slave_fd, 2)
            if slave_fd > 2:
                os.close(slave_fd)
            os.execve(shell, [shell], os.environ)

        # Parent
        os.close(slave_fd)

        # Non-blocking
        fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        self._process = (master_fd, pid)

    def _spawn_windows(self, shell: str):
        """Windows: pywinpty WinPty."""
        if not _HAS_WINPTY:
            raise RuntimeError(
                "winpty is not installed. Install it with: pip install pywinpty"
            )
        from winpty import PtyProcess

        self._process = PtyProcess.spawn(shell)
        # Set initial size
        self._process.setwinsize(40, 120)

    def spawn(self, shell: Optional[str] = None):
        """Spawn a PTY shell session."""
        if shell is None:
            if IS_WINDOWS:
                # Find the best available shell on Windows
                for candidate in ["pwsh.exe", "powershell.exe", "cmd.exe"]:
                    try:
                        shell_path = self._which_windows(candidate)
                        if shell_path:
                            shell = shell_path
                            break
                    except Exception:
                        continue
                if not shell:
                    shell = "powershell.exe"
            else:
                for candidate in ["/bin/bash", "/bin/sh"]:
                    if os.path.exists(candidate):
                        shell = candidate
                        break
                if not shell:
                    shell = "/bin/sh"

        if IS_WINDOWS:
            self._spawn_windows(shell)
        else:
            self._spawn_unix(shell)

    @staticmethod
    def _which_windows(name: str) -> Optional[str]:
        """Find an executable in PATH on Windows."""
        import subprocess
        try:
            result = subprocess.run(
                ["where", name], capture_output=True, text=True, timeout=3
            )
            if result.returncode == 0:
                return result.stdout.strip().split("\n")[0].strip()
        except Exception:
            pass
        return None

    # ── I/O (platform-agnostic interface) ──

    async def read_output(self) -> Optional[bytes]:
        """
        Read available output from the PTY.
        Returns None when the child process has exited.
        """
        if self._process is None:
            return None

        if IS_WINDOWS:
            return await self._read_output_windows()
        else:
            return await self._read_output_unix()

    async def _read_output_unix(self) -> Optional[bytes]:
        master_fd, pid = self._process
        loop = asyncio.get_event_loop()
        future = loop.create_future()

        def _on_readable():
            try:
                data = os.read(master_fd, 65536)
                if not data:
                    future.set_result(None)
                else:
                    future.set_result(data)
            except OSError as e:
                future.set_exception(e)

        loop.add_reader(master_fd, _on_readable)
        self._reader_added = True
        try:
            return await future
        finally:
            try:
                loop.remove_reader(master_fd)
            except (ValueError, NotImplementedError):
                pass
            self._reader_added = False

    async def _read_output_windows(self) -> Optional[bytes]:
        """Read from pywinpty WinPty via shared thread pool.

        Uses a module-level reusable executor so task cancellation does NOT
        trigger pool.shutdown() (which would block the event loop waiting for
        a stuck proc.read() thread).
        """
        loop = asyncio.get_event_loop()
        proc = self._process

        def _read():
            try:
                data = proc.read(65536)
                if not data:
                    return None
                return data
            except (EOFError, OSError):
                return None

        executor = _get_winpty_executor()
        return await loop.run_in_executor(executor, _read)

    def write_input(self, data: bytes):
        """Write raw bytes into the PTY (keystrokes from the browser)."""
        if self._process is None:
            return

        if IS_WINDOWS:
            try:
                self._process.write(data)
            except Exception:
                pass
        else:
            master_fd, _ = self._process
            try:
                os.write(master_fd, data)
            except OSError:
                pass

    def resize(self, rows: int, cols: int):
        """Resize the terminal window."""
        if self._process is None:
            return

        if IS_WINDOWS:
            try:
                self._process.setwinsize(rows, cols)
            except Exception:
                pass
        else:
            import fcntl
            import struct
            import termios

            master_fd, _ = self._process
            try:
                buf = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(master_fd, termios.TIOCSWINSZ, buf)
            except OSError:
                pass

    @property
    def is_alive(self) -> bool:
        """Check if the child process is still running."""
        if self._process is None:
            return False
        if IS_WINDOWS:
            try:
                # pywinpty: check if process handle is valid
                return self._process.isalive()
            except Exception:
                return False
        else:
            master_fd, pid = self._process
            try:
                wpid, _ = os.waitpid(pid, os.WNOHANG)
                # wpid == 0 means child still alive (WNOHANG, no status available)
                # wpid > 0 means child has exited and was reaped
                return wpid == 0
            except ChildProcessError:
                # Already reaped by another handler
                return False
            except ProcessLookupError:
                # No such process
                return False

    def close(self):
        """Kill the child and clean up."""
        if self._process is None:
            return

        if IS_WINDOWS:
            try:
                self._process.close()
            except Exception:
                pass
        else:
            master_fd, pid = self._process

            # Remove FD reader
            if self._reader_added:
                loop = asyncio.get_event_loop()
                if not loop.is_closed():
                    try:
                        loop.remove_reader(master_fd)
                    except (ValueError, OSError):
                        pass

            # Close FD
            try:
                os.close(master_fd)
            except OSError:
                pass

            # Kill process
            try:
                os.kill(pid, signal.SIGTERM)
                try:
                    os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    pass
            except ProcessLookupError:
                pass

        self._process = None


@router.websocket("/api/v1/terminal/ws")
async def terminal_websocket(websocket: WebSocket):
    """WebSocket endpoint — one PTY session per connection."""

    client_host = websocket.client.host if websocket.client else None
    if not _ws_client_is_loopback(client_host):
        await websocket.close(code=4001, reason="Localhost only")
        return

    await websocket.accept()

    # ── Heartbeat ping to keep connection alive ──
    HEARTBEAT_INTERVAL = 25  # seconds

    async def _heartbeat():
        """Send periodic ping frames to keep WS alive through proxies."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                try:
                    await websocket.send_json({"type": "ping"})
                except (WebSocketDisconnect, Exception):
                    break
        except asyncio.CancelledError:
            pass

    heartbeat_task = asyncio.ensure_future(_heartbeat())

    session = await get_or_create_session()
    try:
        async def reader_task():
            """Background: pump PTY output → WebSocket."""
            try:
                while True:
                    data = await session.read_output()
                    if data is None:
                        await websocket.send_bytes(b"")
                        break
                    await websocket.send_bytes(data=data)
            except (WebSocketDisconnect, Exception):
                pass

        reader = asyncio.ensure_future(reader_task())

        try:
            while True:
                msg = await websocket.receive()

                if msg.get("type") == "websocket.disconnect":
                    break

                if "bytes" in msg:
                    session.write_input(msg["bytes"])

                elif "text" in msg:
                    text = msg["text"]
                    try:
                        ctrl = json.loads(text)
                        if ctrl.get("type") == "resize":
                            session.resize(ctrl["rows"], ctrl["cols"])
                        elif ctrl.get("type") == "input":
                            session.write_input(ctrl["data"].encode("utf-8"))
                    except (json.JSONDecodeError, TypeError):
                        session.write_input(text.encode("utf-8"))

        finally:
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"Terminal error: {e}", exc_info=True)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        # Do NOT close the session — it's persistent across page reloads
        logger.info("Terminal WebSocket detached (persistent session keeps running)")

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
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect

from app.auth.identity import _is_admin, assert_caller_is
from app.auth.jwt import decode_token

logger = logging.getLogger(__name__)
router = APIRouter()

# ── Keyed persistent terminal sessions ──
#
# Each browser-side terminal tab passes a client-generated session_id when it
# opens the WebSocket. A PTY is spawned on first connect for that id and kept
# alive across WS reconnects (refresh / network blip), so a long-running
# command keeps going even if the user closes their tab and comes back later.
# Sessions are reaped when:
#   • the shell exits naturally (e.g. user types `exit`) — caught lazily on
#     the next lookup that touches that id, or proactively on each new
#     session creation,
#   • the browser explicitly DELETEs the session (close-tab path), or
#   • the server shuts down.
_sessions: Dict[str, "TerminalSession"] = {}
_sessions_lock = asyncio.Lock()


def _reap_dead_locked() -> None:
    """Drop any sessions whose shell process has exited. Caller MUST hold the lock."""
    for sid, s in list(_sessions.items()):
        if not s.is_alive:
            try:
                s.close()
            except Exception:
                pass
            del _sessions[sid]
            logger.info("Reaped dead terminal session %s", sid)


async def get_or_create_session(session_id: str) -> "TerminalSession":
    """Return the PTY session for session_id, spawning it if it doesn't exist
    yet or if its previous shell has died."""
    async with _sessions_lock:
        _reap_dead_locked()
        sess = _sessions.get(session_id)
        if sess is not None and sess.is_alive:
            return sess
        sess = TerminalSession()
        sess.spawn()
        sess.write_input(b"\r")
        _sessions[session_id] = sess
        logger.info("Created terminal session %s", session_id)
        return sess


async def close_session(session_id: str) -> bool:
    """Kill a session by id and drop it from the map. Returns True if a
    session existed for that id."""
    async with _sessions_lock:
        sess = _sessions.pop(session_id, None)
        if sess is None:
            return False
        try:
            sess.close()
        except Exception:
            pass
        logger.info("Closed terminal session %s on client request", session_id)
        return True


async def close_all_sessions():
    """Close every live session. Called on server shutdown."""
    async with _sessions_lock:
        for sid, s in list(_sessions.items()):
            try:
                s.close()
            except Exception:
                pass
        _sessions.clear()
        logger.info("Closed all terminal sessions")
    if IS_WINDOWS:
        _close_winpty_executor()


# Backwards-compatible name retained because main.py imports it on shutdown.
close_persistent_session = close_all_sessions


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


# Per-session scrollback ring buffer size. 64 KB ≈ ~800 lines of typical
# shell output — enough to make a refreshed tab feel continuous, small
# enough that 100 idle sessions cost <10 MB of RSS.
SCROLLBACK_BYTES = 64 * 1024


class TerminalSession:
    """A single PTY-based shell session, one per WebSocket connection."""

    def __init__(self):
        self._process = None  # WinPty on Windows, (master_fd, pid) tuple on Unix
        self._reader_added = False
        # Ring buffer of recent PTY output. Replayed to any newly-attached
        # WebSocket so a refreshed/reattached tab doesn't see a blank screen.
        self._scrollback = bytearray()

    def _append_scrollback(self, data: bytes) -> None:
        """Append `data` to the ring buffer, evicting bytes from the front
        once the cap is exceeded."""
        if not data:
            return
        self._scrollback.extend(data)
        overflow = len(self._scrollback) - SCROLLBACK_BYTES
        if overflow > 0:
            del self._scrollback[:overflow]

    def get_scrollback(self) -> bytes:
        """Return a snapshot of the current scrollback buffer."""
        return bytes(self._scrollback)

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
            data = await self._read_output_windows()
        else:
            data = await self._read_output_unix()
        # Mirror everything into the scrollback so a later reattach can replay.
        if data:
            self._append_scrollback(data)
        return data

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
                # pywinpty returns str, encode to bytes for the WebSocket
                if isinstance(data, str):
                    return data.encode('utf-8', errors='replace')
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
                # pywinpty expects str, not bytes
                if isinstance(data, bytes):
                    data = data.decode('utf-8', errors='replace')
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


_FALLBACK_SESSION_ID = "__default__"


async def _verify_admin_token(token: str) -> Optional[str]:
    """Decode a JWT and confirm the subject is an admin. Returns the verified
    user_id or None if the token is invalid / the user is not an admin."""
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    user_id = payload.get("user_id") or payload.get("sub")
    if not user_id:
        return None
    if not await _is_admin(user_id):
        return None
    return user_id


@router.delete("/api/v1/terminal/sessions/{session_id}")
async def delete_terminal_session(session_id: str, request: Request):
    """Kill a PTY by session_id. Called by the browser when the user closes
    a terminal tab so the shell doesn't outlive its UI."""
    # Admin-only — the HTTP auth middleware already verifies the JWT and
    # populates request.state.user_id; we just need to confirm admin.
    uid = await assert_caller_is(request, None)
    if not await _is_admin(uid):
        raise HTTPException(status_code=403, detail="Admin required")
    closed = await close_session(session_id)
    return {"closed": closed}


@router.websocket("/api/v1/terminal/ws")
async def terminal_websocket(websocket: WebSocket):
    """WebSocket endpoint — one PTY session per `session_id` query param.
    Multiple connects with the same id reattach to the same shell.

    Authentication: the browser passes a JWT as `?token=<jwt>`. The token's
    subject must be an admin or the socket is closed before any shell is
    spawned. The HTTP auth middleware whitelists this WS path so this
    handler is the sole gatekeeper."""

    # Decode the token BEFORE accepting the WS so a hostile client doesn't
    # get a half-open connection.
    token = websocket.query_params.get("token", "")
    verified_uid = await _verify_admin_token(token)
    if not verified_uid:
        # 4401 = WebSocket "policy violation" close code reserved for auth
        # failures; client-side reconnect logic treats it as a hard stop.
        await websocket.close(code=4401)
        return

    await websocket.accept()

    # Each terminal tab in the browser generates its own session_id (a UUID)
    # and includes it as a query param. If a legacy client connects without
    # one, fall back to a shared id so the page still works.
    session_id = websocket.query_params.get("session_id") or _FALLBACK_SESSION_ID

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

    session = await get_or_create_session(session_id)

    # ── Replay scrollback ──
    # A reattached tab sees a fresh xterm that knows nothing about what was
    # on screen before. Send the last SCROLLBACK_BYTES of output before the
    # live stream so the user picks up roughly where they left off.
    scrollback = session.get_scrollback()
    if scrollback:
        try:
            await websocket.send_bytes(scrollback)
        except Exception:
            pass

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
        # Do NOT close the session — it's persistent across page reloads.
        # Sessions are only closed by an explicit DELETE from the client,
        # by the shell exiting on its own, or on server shutdown.
        logger.info("Terminal WebSocket detached for session %s (session keeps running)", session_id)

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
import time
from typing import Any, Dict, List, Optional

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
#   • the browser explicitly DELETEs the session (close-tab path),
#   • the idle GC pass kills shells whose WS has been detached longer than
#     `IDLE_TIMEOUT_SECS` (prevents slow fd creep on long-running VMs), or
#   • the server shuts down.
_sessions: Dict[str, "TerminalSession"] = {}
_sessions_lock = asyncio.Lock()

# Per-user limit on simultaneously-running PTYs. A soft guard against a UI
# bug or DOS attempt opening hundreds of shells. Overridable via env.
MAX_SESSIONS_PER_USER = int(os.environ.get("TERMINAL_MAX_SESSIONS_PER_USER", "20"))

# How long a session may sit unattended (no WebSocket connected) before the
# GC pass kills it. Default 0 (disabled) so long-running processes like
# `claude remote-control` survive browser closures indefinitely; set to a
# positive number of hours to enable the GC.
IDLE_TIMEOUT_SECS = int(os.environ.get("TERMINAL_IDLE_TIMEOUT_HOURS", "0")) * 3600

# How often the GC pass runs. Cheaper than the timeout — 5 minutes catches
# stale sessions quickly without burning cycles.
IDLE_GC_INTERVAL_SECS = 300


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


def _count_sessions_for_user_locked(user_id: str) -> int:
    """Count live sessions owned by `user_id`. Caller MUST hold the lock."""
    return sum(1 for s in _sessions.values() if s.user_id == user_id and s.is_alive)


class SessionCapExceeded(Exception):
    """Raised when a user has already hit MAX_SESSIONS_PER_USER live shells."""


async def get_or_create_session(session_id: str, user_id: str) -> "TerminalSession":
    """Return the PTY session for session_id, spawning it if it doesn't exist
    yet or if its previous shell has died. The first creation for an id
    binds it to `user_id`; reattachments verify the owner matches."""
    async with _sessions_lock:
        _reap_dead_locked()
        sess = _sessions.get(session_id)
        if sess is not None and sess.is_alive:
            # Don't let one admin attach to another admin's shell — the UI
            # generates UUIDs per-tab so collisions only happen by guess.
            if sess.user_id and sess.user_id != user_id:
                raise PermissionError("session_id belongs to a different user")
            return sess

        # Per-user cap — count only this user's live sessions.
        if MAX_SESSIONS_PER_USER > 0:
            existing = _count_sessions_for_user_locked(user_id)
            if existing >= MAX_SESSIONS_PER_USER:
                raise SessionCapExceeded(
                    f"You already have {existing} terminal session(s) open "
                    f"(cap: {MAX_SESSIONS_PER_USER}). Close one before opening another."
                )

        sess = TerminalSession(user_id=user_id)
        sess.spawn()
        sess.write_input(b"\r")
        _sessions[session_id] = sess
        logger.info("Created terminal session %s for user %s", session_id, user_id)
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


# ── Idle GC ──

_gc_task: Optional[asyncio.Task] = None


async def _idle_gc_loop():
    """Background task: every IDLE_GC_INTERVAL_SECS, kill sessions whose WS
    has been detached longer than IDLE_TIMEOUT_SECS. Set
    `TERMINAL_IDLE_TIMEOUT_HOURS=0` to disable."""
    if IDLE_TIMEOUT_SECS <= 0:
        logger.info("Terminal idle GC disabled (TERMINAL_IDLE_TIMEOUT_HOURS=0)")
        return
    logger.info(
        "Terminal idle GC armed: %ds timeout, %ds interval",
        IDLE_TIMEOUT_SECS, IDLE_GC_INTERVAL_SECS,
    )
    while True:
        try:
            await asyncio.sleep(IDLE_GC_INTERVAL_SECS)
            now = time.time()
            to_kill: List[str] = []
            async with _sessions_lock:
                for sid, s in _sessions.items():
                    if s.attached_count > 0:
                        continue
                    if s.last_detach_time is None:
                        continue
                    if (now - s.last_detach_time) >= IDLE_TIMEOUT_SECS:
                        to_kill.append(sid)
                for sid in to_kill:
                    sess = _sessions.pop(sid, None)
                    if sess is None:
                        continue
                    try:
                        sess.close()
                    except Exception:
                        pass
                    logger.info("Idle GC reaped terminal session %s", sid)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Idle GC pass failed")


def start_idle_gc():
    """Launch the background GC task. Safe to call multiple times."""
    global _gc_task
    if _gc_task is not None and not _gc_task.done():
        return
    _gc_task = asyncio.ensure_future(_idle_gc_loop())


def stop_idle_gc():
    """Cancel the background GC task. Called on shutdown."""
    global _gc_task
    if _gc_task is not None and not _gc_task.done():
        _gc_task.cancel()
    _gc_task = None


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

    def __init__(self, user_id: str = ""):
        self._process = None  # WinPty on Windows, (master_fd, pid) tuple on Unix
        self._reader_added = False
        # Ring buffer of recent PTY output. Replayed to any newly-attached
        # WebSocket so a refreshed/reattached tab doesn't see a blank screen.
        self._scrollback = bytearray()
        # Bookkeeping for the idle GC and the admin list endpoint.
        self.user_id: str = user_id
        self.created_at: float = time.time()
        self.attached_count: int = 0
        # When attached_count drops to 0 this is set to time.time(); reset to
        # None while at least one WS is attached. Initialised to creation
        # time so a session that's never been attached still has an idle
        # clock running.
        self.last_detach_time: Optional[float] = time.time()

    def mark_attached(self) -> None:
        """Increment the connected-clients counter (WS handshake)."""
        self.attached_count += 1
        self.last_detach_time = None

    def mark_detached(self) -> None:
        """Decrement the connected-clients counter (WS finally / disconnect).
        When it hits 0, start the idle clock."""
        self.attached_count = max(0, self.attached_count - 1)
        if self.attached_count == 0:
            self.last_detach_time = time.time()

    def scrollback_size(self) -> int:
        return len(self._scrollback)

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


@router.get("/api/v1/terminal/sessions")
async def list_terminal_sessions(request: Request) -> List[Dict[str, Any]]:
    """Snapshot of every live PTY session. Admin-only. Useful for debugging
    leaks and seeing what shells are attached to which browsers."""
    uid = await assert_caller_is(request, None)
    if not await _is_admin(uid):
        raise HTTPException(status_code=403, detail="Admin required")
    now = time.time()
    out: List[Dict[str, Any]] = []
    async with _sessions_lock:
        for sid, s in _sessions.items():
            idle_secs = None
            if s.attached_count == 0 and s.last_detach_time is not None:
                idle_secs = int(now - s.last_detach_time)
            out.append({
                "session_id": sid,
                "user_id": s.user_id,
                "alive": s.is_alive,
                "attached_clients": s.attached_count,
                "idle_secs": idle_secs,
                "age_secs": int(now - s.created_at),
                "scrollback_bytes": s.scrollback_size(),
            })
    return out


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

    # Resolve or spawn the PTY for this session_id, scoped to the verified
    # user. Two failure modes:
    #   • SessionCapExceeded — user has hit MAX_SESSIONS_PER_USER live shells.
    #   • PermissionError — session_id is taken by another user (UUID
    #     collision or guess).
    try:
        session = await get_or_create_session(session_id, verified_uid)
    except SessionCapExceeded as e:
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
        # 4002 = custom "policy" close, treated by the client as a hard stop.
        await websocket.close(code=4002, reason=str(e)[:120])
        heartbeat_task.cancel()
        return
    except PermissionError as e:
        await websocket.close(code=4401, reason=str(e)[:120])
        heartbeat_task.cancel()
        return

    session.mark_attached()

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
        # Decrement the attached-clients counter so the idle GC can start
        # ticking once this was the last open WebSocket on the session.
        try:
            session.mark_detached()
        except Exception:
            pass
        # Do NOT close the session — it's persistent across page reloads.
        # Sessions are only closed by an explicit DELETE from the client,
        # by the shell exiting on its own, or on server shutdown.
        logger.info("Terminal WebSocket detached for session %s (session keeps running)", session_id)

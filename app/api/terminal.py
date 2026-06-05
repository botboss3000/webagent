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
import re
import sys
import signal
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

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

# Strips ANSI/VT control sequences (CSI colour codes, cursor moves, OSC titles)
# so an agent reading the screen via read_text() gets human-/LLM-readable text
# instead of raw escape soup. The xterm.js browser panel still gets the raw
# bytes — this only cleans the agent-facing snapshot.
_ANSI_RE = re.compile(
    r"""
    \x1B \[ [0-?]* [ -/]* [@-~]     # CSI ... cmd
    | \x1B \] .*? (?: \x07 | \x1B \\ )  # OSC ... BEL or ST
    | \x1B [@-Z\\-_]                # two-byte escapes
    | [\x00-\x08\x0B\x0C\x0E-\x1F]  # stray control chars (keep \t \n \r)
    """,
    re.VERBOSE | re.DOTALL,
)

# Named keys an agent can send via terminal_send(keys=[...]) → raw byte sequences.
# Covers Enter, control combos, and the common navigation keys an interactive
# CLI/TUI expects (answering prompts, moving a selection, aborting).
_KEY_BYTES: Dict[str, bytes] = {
    "enter": b"\r", "return": b"\r", "tab": b"\t", "space": b" ",
    "esc": b"\x1b", "escape": b"\x1b", "backspace": b"\x7f", "delete": b"\x1b[3~",
    "up": b"\x1b[A", "down": b"\x1b[B", "right": b"\x1b[C", "left": b"\x1b[D",
    "home": b"\x1b[H", "end": b"\x1b[F", "pageup": b"\x1b[5~", "pagedown": b"\x1b[6~",
    "ctrl+c": b"\x03", "ctrl+d": b"\x04", "ctrl+z": b"\x1a", "ctrl+l": b"\x0c",
    "ctrl+a": b"\x01", "ctrl+e": b"\x05", "ctrl+u": b"\x15", "ctrl+k": b"\x0b",
    "ctrl+w": b"\x17", "ctrl+r": b"\x12",
}


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)

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

# Sidebar quick-launch shortcuts. Each entry opens a new terminal tab and
# types `command` followed by Enter into the freshly-spawned shell. Override
# the whole list at boot via env var QUICK_LAUNCHES_JSON='[{...}, {...}]'.
DEFAULT_QUICK_LAUNCHES: List[Dict[str, str]] = [
    {"name": "Claude Remote Control",
     "command": "tmux new -As cc 'claude remote-control --spawn=worktree'",
     "icon": "smartphone"},
    {"name": "Attach to 'cc'",
     "command": "tmux attach -t cc",
     "icon": "link"},
    {"name": "List tmux sessions",
     "command": "tmux ls",
     "icon": "list"},
    {"name": "Plain shell",
     "command": "",
     "icon": "terminal"},
]


def _load_quick_launches() -> List[Dict[str, str]]:
    raw = os.environ.get("QUICK_LAUNCHES_JSON")
    if not raw:
        return DEFAULT_QUICK_LAUNCHES
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict) and "name" in d]
    except Exception as e:
        logger.warning("Bad QUICK_LAUNCHES_JSON, using defaults: %s", e)
    return DEFAULT_QUICK_LAUNCHES


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
        sess.start_pump()          # begin draining output immediately (even with no WS)
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


# ── Agent-facing session API (used by the terminal_* tools) ──
#
# These wrap the same session registry the browser terminal uses, so an
# agent-opened session is a first-class session: it shows up in the sidebar,
# survives reconnects, and a human can attach to watch / take over.

async def open_agent_session(
    user_id: str, command: str = "", name: str = "",
) -> Tuple[str, "TerminalSession"]:
    """Spawn a new PTY for an agent and (optionally) run `command` in it.
    Returns (session_id, session). The session is tagged agent_driven so the
    UI surfaces it for the human to watch."""
    session_id = uuid.uuid4().hex
    sess = await get_or_create_session(session_id, user_id)
    sess.agent_driven = True
    sess.launch_command = (command or "").strip()
    sess.name = (name or "").strip()[:80] or (sess.launch_command[:80] if sess.launch_command else "agent terminal")
    if sess.launch_command:
        # Wait for the shell to actually paint its first prompt and settle
        # (cold shells like PowerShell take a few seconds), THEN run the command
        # — otherwise the keystrokes are typed into a not-yet-ready shell and
        # lost. require_activity guards against the empty-prompt false-settle.
        await sess.wait_idle(quiet_secs=0.5, timeout=12.0, require_activity=True)
        sess.send_text(sess.launch_command, enter=True)
    return session_id, sess


async def get_owned_session(session_id: str, user_id: str) -> Optional["TerminalSession"]:
    """Look up a live session by id, enforcing ownership. Returns None if it
    doesn't exist / has died / belongs to someone else."""
    async with _sessions_lock:
        _reap_dead_locked()
        sess = _sessions.get(session_id)
        if sess is None or not sess.is_alive:
            return None
        if sess.user_id and sess.user_id != user_id:
            return None
        return sess


async def snapshot_sessions(user_id: str) -> List[Dict[str, Any]]:
    """List the caller's live sessions with agent-driving metadata, for the
    terminal_list tool."""
    now = time.time()
    out: List[Dict[str, Any]] = []
    async with _sessions_lock:
        for sid, s in _sessions.items():
            if s.user_id and s.user_id != user_id:
                continue
            if not s.is_alive:
                continue
            idle_secs = None
            if s.attached_count == 0 and s.last_detach_time is not None:
                idle_secs = int(now - s.last_detach_time)
            out.append({
                "session_id": sid,
                "name": s.name,
                "agent_driven": s.agent_driven,
                "launch_command": s.launch_command,
                "paused": s.paused,
                "watchers": s.attached_count,
                "idle_secs": idle_secs,
                "ended": s.ended,
            })
    return out


async def set_session_paused(session_id: str, user_id: str, paused: bool) -> bool:
    """Take-over lock: pause/resume the agent's control of a session. Returns
    True if the session was found + toggled."""
    sess = await get_owned_session(session_id, user_id)
    if sess is None:
        return False
    sess.paused = bool(paused)
    return True


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
    #
    # Each session's background pump pins ONE worker in a blocking proc.read()
    # for the session's whole life, so the pool must be at least as large as the
    # number of concurrent live sessions or later sessions' pumps starve (their
    # output never drains). Sized to the per-user session cap + headroom;
    # override with TERMINAL_PTY_READ_THREADS.
    _PTY_READ_THREADS = int(os.environ.get(
        "TERMINAL_PTY_READ_THREADS", str(max(32, MAX_SESSIONS_PER_USER + 8))
    ))
    _WINPTY_READER: Optional[concurrent.futures.ThreadPoolExecutor] = None

    def _get_winpty_executor() -> concurrent.futures.ThreadPoolExecutor:
        global _WINPTY_READER
        if _WINPTY_READER is None:
            _WINPTY_READER = concurrent.futures.ThreadPoolExecutor(
                max_workers=_PTY_READ_THREADS, thread_name_prefix="pty-read"
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
        # Friendly label supplied by the client (?name=... on WS open, or a
        # JSON {"type":"set_name"} control). Used by the "Your sessions"
        # sidebar so other devices see something better than a raw UUID.
        self.name: str = ""
        # When attached_count drops to 0 this is set to time.time(); reset to
        # None while at least one WS is attached. Initialised to creation
        # time so a session that's never been attached still has an idle
        # clock running.
        self.last_detach_time: Optional[float] = time.time()

        # ── Background pump + broadcast (decouples reading from WS attachment) ──
        # A single pump task owns the PTY's sole reader; it drains output into
        # scrollback AND fans it out to every subscriber queue. This lets the
        # agent drive a session with NO browser attached (the program never
        # blocks on a full output buffer) and lets multiple watchers share one
        # PTY. Reads must not happen anywhere else (single-reader constraint).
        self._subscribers: set[asyncio.Queue] = set()
        self._io_lock = asyncio.Lock()        # guards (append scrollback + fan-out) vs (snapshot + subscribe)
        self._pump_task: Optional[asyncio.Task] = None
        self._ended = False                   # the child process has exited
        self.last_output_ts: float = time.monotonic()  # for wait_idle() quiet detection

        # ── Agent-driving metadata ──
        # agent_driven → this session was opened by an agent (terminal_open),
        # surfaced in the sessions list + sidebar so a human knows to watch it.
        # launch_command → the command the agent ran on open (for display).
        # paused → take-over lock: while True, agent terminal_send is refused so
        # a human who grabbed the wheel isn't fought for the keyboard.
        self.agent_driven: bool = False
        self.launch_command: str = ""
        self.paused: bool = False

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

    # ── Background pump + broadcast ──

    def start_pump(self) -> None:
        """Launch the single background reader for this session (idempotent).
        It drains the PTY forever — into scrollback and out to all subscribers —
        so output is captured even with no WebSocket attached."""
        if self._pump_task is not None and not self._pump_task.done():
            return
        self._pump_task = asyncio.ensure_future(self._pump_loop())

    async def _pump_loop(self) -> None:
        """Own the PTY's sole reader: read → append scrollback → fan out. On the
        child exiting, mark ended and signal every subscriber with a None
        sentinel so their WebSocket loops can close cleanly."""
        try:
            while True:
                data = await self.read_output()   # the ONLY caller of read_output
                if data is None:
                    self._ended = True
                    await self._broadcast(None)
                    return
                if not data:
                    continue
                self.last_output_ts = time.monotonic()
                await self._broadcast(data)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("pump loop ended for a session: %s", e)
            self._ended = True
            try:
                await self._broadcast(None)
            except Exception:
                pass

    async def _broadcast(self, item: Optional[bytes]) -> None:
        """Append `item` to scrollback AND push it to every subscriber, atomically
        under _io_lock vs. a new subscriber snapshotting scrollback — so no byte
        is both replayed AND queued (duplicate) or lost between the two (gap).
        `item` is None for the end-of-stream sentinel."""
        async with self._io_lock:
            if item:
                self._append_scrollback(item)
            for q in list(self._subscribers):
                try:
                    q.put_nowait(item)
                except asyncio.QueueFull:
                    # Slow watcher — drop to stay live; scrollback still has it.
                    pass

    async def subscribe(self) -> Tuple["asyncio.Queue", bytes]:
        """Register a live-output queue AND atomically grab the current
        scrollback, so a newly-attached watcher gets every byte exactly once:
        everything up to now via the returned snapshot, everything after via the
        queue. Returns (queue, scrollback_snapshot)."""
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        async with self._io_lock:
            snapshot = bytes(self._scrollback)
            self._subscribers.add(q)
            if self._ended:
                q.put_nowait(None)
        return q, snapshot

    def unsubscribe(self, q: "asyncio.Queue") -> None:
        self._subscribers.discard(q)

    # ── Agent-facing I/O (used by the terminal_* tools) ──

    def send_text(self, text: str, enter: bool = False) -> None:
        """Type `text` into the program (optionally followed by Enter)."""
        if text:
            self.write_input(text.encode("utf-8"))
        if enter:
            self.write_input(b"\r")

    def send_keys(self, keys: List[str]) -> List[str]:
        """Send a sequence of named keys (see _KEY_BYTES). Returns the list of
        any unknown key names (ignored) so the tool can report them."""
        unknown: List[str] = []
        for k in keys:
            seq = _KEY_BYTES.get(str(k).strip().lower())
            if seq is None:
                unknown.append(k)
            else:
                self.write_input(seq)
        return unknown

    def read_text(self, tail_chars: int = 4000, strip_ansi: bool = True) -> str:
        """Return the recent output as readable text (ANSI stripped by default),
        trimmed to the last `tail_chars` characters — what's effectively on
        screen now, for the agent to reason about."""
        raw = bytes(self._scrollback).decode("utf-8", errors="replace")
        if strip_ansi:
            raw = _strip_ansi(raw)
        if tail_chars and len(raw) > tail_chars:
            raw = raw[-tail_chars:]
        return raw

    async def wait_idle(
        self, quiet_secs: float = 0.6, timeout: float = 30.0,
        require_activity: bool = True,
    ) -> bool:
        """Block until the program has REACTED and then gone quiet — i.e. it
        produced some new output after this call started and has since been
        silent for `quiet_secs` (it's settled, presumably waiting for input).

        `require_activity` defaults True so this doesn't "false-settle" during a
        cold start or the beat between sending input and the program responding
        (both look momentarily quiet though nothing has happened yet). Pass
        False for a plain "is it quiet right now" check. Returns True if it
        settled (or the child exited), False on timeout with no reaction."""
        start = time.monotonic()
        deadline = start + max(0.0, timeout)
        baseline_ts = self.last_output_ts
        saw_activity = False
        poll = min(0.1, quiet_secs / 2) if quiet_secs > 0 else 0.1
        while True:
            if self._ended or not self.is_alive:
                return True
            if self.last_output_ts > baseline_ts:
                saw_activity = True
            ready = saw_activity or not require_activity
            quiet_for = time.monotonic() - self.last_output_ts
            if ready and quiet_for >= quiet_secs:
                return True
            if time.monotonic() >= deadline:
                return saw_activity or not require_activity
            await asyncio.sleep(poll)

    @property
    def ended(self) -> bool:
        return self._ended

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
        # NB: scrollback append happens in _broadcast(), under _io_lock, so that
        # "append + fan-out" is atomic vs. a new subscriber's "snapshot +
        # subscribe" — otherwise a byte could land in both the replay snapshot
        # AND the live queue (duplicate) or neither (gap).
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
        # Stop the background pump first so it doesn't read a closing fd.
        if self._pump_task is not None and not self._pump_task.done():
            self._pump_task.cancel()
        self._pump_task = None
        self._ended = True
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


@router.post("/api/v1/terminal/sessions/{session_id}/pause")
async def pause_terminal_session(session_id: str, request: Request):
    """Take-over lock: pause (or resume) an agent's control of a session so a
    human can drive it without the agent fighting for the keyboard. Body:
    {"paused": true|false}. Admin-only."""
    uid = await assert_caller_is(request, None)
    if not await _is_admin(uid):
        raise HTTPException(status_code=403, detail="Admin required")
    try:
        body = await request.json()
    except Exception:
        body = {}
    paused = bool(body.get("paused", True))
    ok = await set_session_paused(session_id, uid, paused)
    if not ok:
        raise HTTPException(status_code=404, detail="No such session")
    return {"session_id": session_id, "paused": paused}


# ── Terminal tunnel: the user drives a program directly through chat ──
#
# A tunnel binds a CHAT session to a terminal session so the user's chat
# messages become keystrokes for the program and its output streams into chat.
# The agent steps aside. Mechanism lives in app/agent/terminal_tunnel.py; these
# endpoints are what the chat UI calls (status on load, key palette, hand-back)
# and what the one-click "Terminal Tunnel" launcher calls to open+bind.


@router.get("/api/v1/terminal/tunnel/{chat_session_id}")
async def get_tunnel(chat_session_id: str, request: Request) -> Dict[str, Any]:
    """Return the tunnel binding for a chat session ({"enabled": false} if none).
    The chat UI calls this on load to know whether to mount the tunnel banner +
    embedded terminal."""
    uid = await assert_caller_is(request, None)
    if not await _is_admin(uid):
        raise HTTPException(status_code=403, detail="Admin required")
    from app.db import get_db
    from app.agent.terminal_tunnel import get_tunnel_cfg
    cfg = await get_tunnel_cfg(get_db(), chat_session_id)
    return cfg or {"enabled": False}


@router.post("/api/v1/terminal/tunnel/{chat_session_id}/enable")
async def enable_tunnel_endpoint(chat_session_id: str, request: Request) -> Dict[str, Any]:
    """Open (if needed) and bind a terminal to this chat as a tunnel — the
    one-click "Terminal Tunnel" path. Body: {terminal_session_id?, command?,
    name?}. With terminal_session_id it binds that existing session; otherwise it
    opens a fresh session running `command` and binds that."""
    uid = await assert_caller_is(request, None)
    if not await _is_admin(uid):
        raise HTTPException(status_code=403, detail="Admin required")
    try:
        body = await request.json()
    except Exception:
        body = {}
    terminal_session_id = (body.get("terminal_session_id") or "").strip()
    command = (body.get("command") or "").strip()
    name = (body.get("name") or "").strip()

    if not terminal_session_id:
        # Open a fresh terminal for this tunnel.
        try:
            terminal_session_id, _sess = await open_agent_session(
                uid, command=command, name=name or "terminal tunnel",
            )
        except SessionCapExceeded as e:
            raise HTTPException(status_code=409, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"could not open terminal: {e}")

    from app.db import get_db
    from app.agent.terminal_tunnel import enable_tunnel
    try:
        cfg = await enable_tunnel(
            get_db(), uid, chat_session_id, terminal_session_id,
            mediated=False, command=command,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return cfg


@router.post("/api/v1/terminal/tunnel/{chat_session_id}/disable")
async def disable_tunnel_endpoint(chat_session_id: str, request: Request) -> Dict[str, Any]:
    """Hand control back to the agent — the chat UI's "Hand back" button. The
    terminal program keeps running; only the chat↔terminal binding is removed."""
    uid = await assert_caller_is(request, None)
    if not await _is_admin(uid):
        raise HTTPException(status_code=403, detail="Admin required")
    from app.db import get_db
    from app.agent.terminal_tunnel import disable_tunnel
    await disable_tunnel(get_db(), uid, chat_session_id, reason="handed back")
    return {"chat_session_id": chat_session_id, "enabled": False}


@router.post("/api/v1/terminal/tunnel/{chat_session_id}/keys")
async def tunnel_keys_endpoint(chat_session_id: str, request: Request) -> Dict[str, Any]:
    """Send named special keys (Enter, Esc, Ctrl+C, arrows, Tab, …) from the
    chat key palette into the bound program. Body: {"keys": ["ctrl+c"]}."""
    uid = await assert_caller_is(request, None)
    if not await _is_admin(uid):
        raise HTTPException(status_code=403, detail="Admin required")
    try:
        body = await request.json()
    except Exception:
        body = {}
    keys = body.get("keys") or []
    if not isinstance(keys, list):
        raise HTTPException(status_code=400, detail="keys must be a list")
    from app.db import get_db
    from app.agent.terminal_tunnel import send_keys as _tunnel_send_keys
    ok, info = await _tunnel_send_keys(get_db(), uid, chat_session_id, keys)
    if not ok:
        raise HTTPException(status_code=409, detail=str(info))
    return {"chat_session_id": chat_session_id, "sent": keys, "unknown_keys": info}


@router.get("/api/v1/terminal/sessions")
async def list_terminal_sessions(request: Request) -> List[Dict[str, Any]]:
    """Snapshot of live PTY sessions. Each admin caller sees only their own
    sessions — the sidebar uses this to surface sessions started on other
    devices so the user can re-attach to them. Returns name, attach state,
    and idle time per session."""
    uid = await assert_caller_is(request, None)
    if not await _is_admin(uid):
        raise HTTPException(status_code=403, detail="Admin required")
    now = time.time()
    out: List[Dict[str, Any]] = []
    async with _sessions_lock:
        for sid, s in _sessions.items():
            # Only surface sessions owned by the caller. The earlier all-users
            # behaviour was useful for debugging but leaked other admins' tabs.
            if s.user_id and s.user_id != uid:
                continue
            idle_secs = None
            if s.attached_count == 0 and s.last_detach_time is not None:
                idle_secs = int(now - s.last_detach_time)
            out.append({
                "session_id": sid,
                "user_id": s.user_id,
                "name": s.name or "",
                "alive": s.is_alive,
                "attached_clients": s.attached_count,
                "idle_secs": idle_secs,
                "age_secs": int(now - s.created_at),
                "scrollback_bytes": s.scrollback_size(),
                # Agent-driving metadata — the sidebar badges agent-opened
                # sessions and offers a pause/resume (take-over) control.
                "agent_driven": s.agent_driven,
                "launch_command": s.launch_command,
                "paused": s.paused,
            })
    return out


@router.get("/api/v1/terminal/quick-launches")
async def list_quick_launches(request: Request) -> List[Dict[str, str]]:
    """Return the configured quick-launch shortcuts shown in the sidebar.
    Each entry has `name`, `command` (typed into a new shell), and `icon`."""
    uid = await assert_caller_is(request, None)
    if not await _is_admin(uid):
        raise HTTPException(status_code=403, detail="Admin required")
    return _load_quick_launches()


@router.get("/api/v1/terminal/tmux-sessions")
async def list_tmux_sessions(request: Request) -> List[Dict[str, Any]]:
    """List live tmux sessions on the host (whatever user the webagent runs
    as). Used by the sidebar to show running sessions you can attach to.
    Returns [] if tmux isn't installed or no server is running."""
    uid = await assert_caller_is(request, None)
    if not await _is_admin(uid):
        raise HTTPException(status_code=403, detail="Admin required")
    fmt = "#{session_name}|#{session_windows}|#{session_attached}|#{session_created}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "ls", "-F", fmt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return []
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=3.0)
    except asyncio.TimeoutError:
        try: proc.kill()
        except Exception: pass
        return []
    if proc.returncode != 0:
        # rc=1 with "no server running" is the normal empty case.
        return []
    out: List[Dict[str, Any]] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        parts = line.split("|")
        if len(parts) < 4:
            continue
        try:
            out.append({
                "name": parts[0],
                "windows": int(parts[1]),
                "attached": parts[2] == "1",
                "created": int(parts[3]),
            })
        except ValueError:
            continue
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

    # ── Bidirectional liveness ──
    # Server pings every HEARTBEAT_INTERVAL. The client replies with
    # {"type":"pong"} (or sends any other frame, e.g. a keystroke). If we see
    # no inbound frame for CLIENT_SILENCE_TIMEOUT, the peer is dead (mobile
    # backgrounded, NAT rebinding, hung proxy) — we force-close so the client
    # reconnect logic actually fires. Without this check, a half-open TCP
    # connection lets us cheerfully send into the void for ~10 minutes before
    # the kernel notices, during which keystrokes vanish silently.
    HEARTBEAT_INTERVAL = 25         # seconds — send-side keepalive
    CLIENT_SILENCE_TIMEOUT = 45     # seconds — inbound-silence death threshold
    SEND_TIMEOUT = 10               # seconds — bound any single send

    last_client_frame_ts = time.monotonic()
    closing = asyncio.Event()

    async def _force_close(reason: str) -> None:
        if closing.is_set():
            return
        closing.set()
        try:
            await websocket.close(code=1011, reason=reason[:120])
        except Exception:
            pass

    async def _heartbeat():
        """Periodic ping + inbound-silence watchdog."""
        try:
            while not closing.is_set():
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if closing.is_set():
                    break
                if time.monotonic() - last_client_frame_ts > CLIENT_SILENCE_TIMEOUT:
                    logger.info(
                        "Terminal %s: no client frames for >%ds, closing",
                        session_id, CLIENT_SILENCE_TIMEOUT,
                    )
                    await _force_close("client silent")
                    break
                try:
                    await asyncio.wait_for(
                        websocket.send_json({"type": "ping"}),
                        timeout=SEND_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.info("Terminal %s: ping send timed out, closing", session_id)
                    await _force_close("ping timeout")
                    break
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

    # Friendly label optionally supplied by the client on every connect. Stored
    # on the session so the list endpoint can return it to other devices.
    name_param = (websocket.query_params.get("name") or "").strip()
    if name_param:
        session.name = name_param[:80]

    session.mark_attached()

    # ── Subscribe to the session's output broadcast ──
    # The background pump owns the PTY reader; we get a live queue plus an atomic
    # scrollback snapshot (everything-so-far via the snapshot, everything-after
    # via the queue — no byte duplicated or dropped). Replaying the snapshot
    # first gives a reattached tab roughly the screen it left.
    out_queue, scrollback = await session.subscribe()
    if scrollback:
        try:
            await asyncio.wait_for(
                websocket.send_bytes(scrollback), timeout=SEND_TIMEOUT,
            )
        except Exception:
            pass

    try:
        async def reader_task():
            """Forward this subscriber's broadcast queue → WebSocket.
            Every send is bounded — a hung client must not pin this coroutine."""
            try:
                while not closing.is_set():
                    data = await out_queue.get()
                    if data is None:        # child exited (end sentinel from pump)
                        try:
                            await asyncio.wait_for(
                                websocket.send_bytes(b""), timeout=SEND_TIMEOUT,
                            )
                        except Exception:
                            pass
                        break
                    try:
                        await asyncio.wait_for(
                            websocket.send_bytes(data=data), timeout=SEND_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Terminal %s: send_bytes timed out, closing", session_id,
                        )
                        await _force_close("send timeout")
                        break
                    except (WebSocketDisconnect, Exception):
                        break
            except Exception:
                pass

        reader = asyncio.ensure_future(reader_task())

        try:
            while True:
                msg = await websocket.receive()
                # Any inbound frame (keystroke, resize, pong) counts as proof
                # of life for the silence watchdog.
                last_client_frame_ts = time.monotonic()

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
                        elif ctrl.get("type") == "set_name":
                            new_name = str(ctrl.get("name") or "").strip()[:80]
                            session.name = new_name
                        elif ctrl.get("type") == "set_paused":
                            # Take-over lock: while paused, the agent's
                            # terminal_send is refused so the human at this panel
                            # owns the keyboard. The human's own keystrokes
                            # (bytes / input) are never gated.
                            session.paused = bool(ctrl.get("paused"))
                        # Other JSON control types (e.g. "pong") only matter
                        # for liveness, which we already updated above.
                    except (json.JSONDecodeError, TypeError):
                        session.write_input(text.encode("utf-8"))

        finally:
            session.unsubscribe(out_queue)
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

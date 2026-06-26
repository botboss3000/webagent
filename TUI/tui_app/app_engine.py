"""App-brain bridge — drive the linked checkout's REAL agent loop from the TUI.

Once a checkout is linked, the terminal stops running its own clone-brain for
ordinary work and instead becomes a *front door* to the app's real agent: it
launches :mod:`tui_app.engine_driver` under the **checkout's own venv**, with
the checkout as the working directory, and talks to it over a line-delimited
JSON pipe. The app's loop runs in the app's environment, against the app's
database, as the **admin** user and the **server-manager** agent template —
but with **no web server**, so the manager keeps working when the server is down.

This module is the parent side of that pipe. It owns the subprocess lifecycle
and translates the driver's raw loop events into the TUI's existing
:class:`tui_app.agent.AgentEvent` contract, so the chat UI renders an app-brain
turn exactly like a built-in-brain turn.

If the checkout can't load (missing venv, broken code, import error), starting
the engine raises :class:`EngineUnavailable`; the caller falls back to the
built-in manager brain so the user can still diagnose and repair the install.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .agent import AgentEvent
from .config import data_dir

EventCB = Callable[[AgentEvent], Awaitable[None]]

_IS_WIN = sys.platform == "win32"
_DRIVER = Path(__file__).with_name("engine_driver.py")


class EngineUnavailable(Exception):
    """The app brain could not be started — caller should fall back."""


def _venv_python(project_root: Path) -> Optional[Path]:
    """The checkout's venv interpreter, or None if it isn't built yet."""
    p = project_root / ".venv" / ("Scripts" if _IS_WIN else "bin") / (
        "python.exe" if _IS_WIN else "python"
    )
    return p if p.exists() else None


class AppEngine:
    """Owns the headless app-brain subprocess and routes turns to it."""

    def __init__(self, project_root: Path, template_id: str = "server-manager") -> None:
        self.project_root = Path(project_root)
        self.template_id = template_id
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stderr_log = None
        self._lock = asyncio.Lock()  # serialize turns over the single pipe

    # ── lifecycle ──────────────────────────────────────────────────────────
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        """Launch the driver and wait for its ``ready`` handshake.

        Raises :class:`EngineUnavailable` if the venv is missing or the app
        fails to boot (the driver reports the reason on a ``fatal`` line).
        """
        if self.is_running():
            return
        vpy = _venv_python(self.project_root)
        if not vpy:
            raise EngineUnavailable(
                f"no virtual environment at {self.project_root / '.venv'} — "
                "run setup_environment first"
            )
        if not _DRIVER.exists():
            raise EngineUnavailable(f"engine driver missing at {_DRIVER}")

        # The app's own logs/prints land on the driver's stderr; keep them in a
        # file next to the TUI data so a broken boot is diagnosable.
        try:
            self._stderr_log = open(data_dir() / "engine.log", "ab", buffering=0)
        except OSError:
            self._stderr_log = None

        try:
            self._proc = await asyncio.create_subprocess_exec(
                str(vpy), str(_DRIVER),
                cwd=str(self.project_root),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=self._stderr_log or asyncio.subprocess.DEVNULL,
                # One loop event (e.g. a big read_source result) is a single JSON
                # line, which can dwarf asyncio's default 64 KB readline buffer —
                # raise it so large tool outputs don't break the pipe.
                limit=16 * 1024 * 1024,
            )
        except OSError as e:
            raise EngineUnavailable(f"could not launch the app engine: {e}") from e

        # Wait for the first protocol line: ready (good) or fatal (boot failed).
        line = await self._read_line(timeout=120.0)
        if line is None:
            await self.stop()
            raise EngineUnavailable("the app engine exited before it was ready "
                                    "(see engine.log)")
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            await self.stop()
            raise EngineUnavailable("the app engine sent an unreadable handshake")
        if msg.get("event") == "ready":
            return
        if msg.get("event") == "fatal":
            await self.stop()
            raise EngineUnavailable(
                "the linked checkout failed to load: " + (msg.get("message") or "?")
            )
        await self.stop()
        raise EngineUnavailable(f"unexpected engine handshake: {msg.get('event')!r}")

    async def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            try:
                if proc.stdin and not proc.stdin.is_closing():
                    proc.stdin.write((json.dumps({"op": "shutdown"}) + "\n").encode())
                    await proc.stdin.drain()
            except (OSError, RuntimeError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
        if self._stderr_log is not None:
            try:
                self._stderr_log.close()
            except OSError:
                pass
            self._stderr_log = None

    # ── a turn ─────────────────────────────────────────────────────────────
    async def run_turn(
        self, session_id: str, user_text: str, on_event: EventCB,
        execution_mode: str = "ask",
    ) -> None:
        """Send one turn to the app brain and stream its events to ``on_event``.

        Mirrors how the app's own buffered runner treats the loop: streaming
        text chunks are discarded (we surface the consolidated ``response``
        bubbles), and a single ``final`` event closes the turn.
        """
        if not self.is_running():
            await self.start()

        async with self._lock:
            rid = uuid.uuid4().hex
            req = {
                "op": "turn", "request_id": rid, "session_id": session_id,
                "message": user_text, "execution_mode": execution_mode,
                "template_id": self.template_id,
            }
            assert self._proc is not None and self._proc.stdin is not None
            try:
                self._proc.stdin.write((json.dumps(req) + "\n").encode())
                await self._proc.stdin.drain()
            except (OSError, RuntimeError) as e:
                raise EngineUnavailable(f"the app engine pipe broke: {e}") from e

            last_text = ""
            while True:
                line = await self._read_line(timeout=None)
                if line is None:
                    # Driver died mid-turn — surface it and let the caller fall back.
                    await self.stop()
                    raise EngineUnavailable("the app engine stopped mid-turn "
                                            "(see engine.log)")
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ev = msg.get("event")
                if ev == "turn_done" and msg.get("request_id") == rid:
                    await on_event(AgentEvent("final", text=last_text))
                    return
                if ev == "turn_error" and msg.get("request_id") == rid:
                    await on_event(AgentEvent("error", text=msg.get("message") or "turn error"))
                    # turn_done follows; loop will close out.
                    continue
                if ev == "loop" and msg.get("request_id") == rid:
                    text = await self._dispatch_loop_event(msg.get("payload") or {}, on_event)
                    if text:
                        last_text = text

    async def _dispatch_loop_event(self, payload: dict, on_event: EventCB) -> str:
        """Translate one raw loop event → AgentEvent. Returns assistant text, if any."""
        etype = payload.get("type")
        if etype in ("response", "agent_step_end"):
            content = payload.get("content") or ""
            if content:
                await on_event(AgentEvent("assistant", text=content))
            return content
        if etype == "tool_call":
            await on_event(AgentEvent(
                "tool_call", tool=payload.get("tool") or "", args=payload.get("args") or {}
            ))
        elif etype == "tool_result":
            await on_event(AgentEvent(
                "tool_result", tool=payload.get("tool") or "",
                text=str(payload.get("result") or ""),
            ))
        elif etype in ("error", "interrupted"):
            await on_event(AgentEvent("error", text=payload.get("message") or etype))
        # All other pipeline/db/billing events are diagnostic — ignore for now
        # (the loop already records them in the app's flight recorder).
        return ""

    # ── pipe reading ───────────────────────────────────────────────────────
    async def _read_line(self, timeout: Optional[float]) -> Optional[str]:
        """Read one decoded protocol line from the driver, or None on EOF."""
        if self._proc is None or self._proc.stdout is None:
            return None
        try:
            if timeout is None:
                raw = await self._proc.stdout.readline()
            else:
                raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout)
        except asyncio.TimeoutError:
            return None
        except (asyncio.LimitOverrunError, ValueError):
            # A line exceeded even the raised buffer — skip it rather than crash
            # the turn (the loop already recorded it in the app's flight recorder).
            return ""
        if not raw:
            return None
        return raw.decode("utf-8", errors="replace")

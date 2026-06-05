"""Terminal-control tools — let an agent drive interactive terminal programs.

These wrap the same persistent PTY session registry the browser terminal panel
uses (``app/api/terminal.py``), so a session an agent opens is a first-class
session: it shows up in the sidebar, survives reconnects, and a human can attach
to **watch or take over** at any time. That shared session is what makes the
"hybrid" experience work — the agent drives headlessly, you can grab the wheel.

The tools are deliberately low-level and program-agnostic so they drive ANY
terminal app — Claude Code, Gemini CLI, Aider, a REPL, an installer prompt:

  terminal_open   — start a program in a new session
  terminal_read   — see what's currently on screen
  terminal_send   — type text / keys into it
  terminal_wait   — block until it reacts and settles (waiting for input)
  terminal_list   — list the caller's live sessions
  terminal_close  — kill a session

Driving an interactive agent like Claude Code is then a recipe the model runs
with these primitives: open it, wait, read the prompt, answer, wait, read…

Gated by the ``terminal_control`` ability (off by default — this is effectively
shell access). Injected by app/tools/loader.py only when that ability is on.
"""

from __future__ import annotations

import json
from typing import List, Optional

from app.api import terminal as _term


# Cap how much screen text we hand back per call, so a chatty program can't blow
# the context window. The agent can ask for more via tail_chars if it must.
_DEFAULT_TAIL = 4000
_MAX_TAIL = 20000


def _err(msg: str, **extra) -> str:
    out = {"status": "error", "error": msg}
    out.update(extra)
    return json.dumps(out)


def build_terminal_tools(user_id: str):
    """Return {tool_name: handler} bound to this user. Mirrors the
    build_delegation_tools(user_id) pattern used elsewhere in the loader."""

    async def terminal_open(command: str = "", name: str = "", wait: bool = True) -> str:
        """Open a NEW terminal session and optionally run a command in it.

        Use this to launch any interactive program you want to drive — e.g.
        command="claude" to start Claude Code, "python" for a REPL, "aider", or
        leave it blank for a plain shell you'll type into. The session is shown
        to the user in the Terminal panel so they can watch and take over.

        Returns the session_id (needed for every other terminal tool) plus the
        first screen once the program has settled.
        """
        try:
            session_id, sess = await _term.open_agent_session(
                user_id, command=command or "", name=name or "",
            )
        except _term.SessionCapExceeded as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"could not open terminal: {e}")
        screen = ""
        settled = False
        if wait:
            settled = await sess.wait_idle(quiet_secs=0.6, timeout=30.0)
            screen = sess.read_text(tail_chars=_DEFAULT_TAIL)
        return json.dumps({
            "status": "ok",
            "session_id": session_id,
            "name": sess.name,
            "command": sess.launch_command,
            "settled": settled,
            "ended": sess.ended,
            "screen": screen,
            "note": "The user can watch and take over this session in the Terminal panel. "
                    "Use terminal_send/terminal_read with this session_id to drive it.",
        })

    async def terminal_read(session_id: str, tail_chars: int = _DEFAULT_TAIL,
                            raw: bool = False) -> str:
        """Read what's currently on the terminal screen (recent output).

        ANSI colour/cursor codes are stripped by default for readability; pass
        raw=true for the unmodified bytes. Note that for full-screen interactive
        UIs the cleaned text may still contain redraw artifacts."""
        sess = await _term.get_owned_session(session_id, user_id)
        if sess is None:
            return _err("no such session (it may have closed)", session_id=session_id)
        tail = max(1, min(int(tail_chars or _DEFAULT_TAIL), _MAX_TAIL))
        screen = sess.read_text(tail_chars=tail, strip_ansi=not raw)
        return json.dumps({
            "status": "ok", "session_id": session_id,
            "ended": sess.ended, "paused": sess.paused, "screen": screen,
        })

    async def terminal_send(session_id: str, text: str = "", enter: bool = True,
                            keys: Optional[List[str]] = None, wait: bool = True,
                            quiet_secs: float = 0.6, timeout: float = 30.0) -> str:
        """Type input into a terminal session.

        `text` is typed as-is; `enter=true` appends Enter (so a typed command
        runs). `keys` sends named special keys IN ORDER for answering prompts /
        navigating menus: e.g. ["enter"], ["ctrl+c"], ["down","down","enter"],
        ["y","enter"]. Recognised: enter, tab, space, esc, backspace, up, down,
        left, right, home, end, pageup, pagedown, ctrl+c/d/z/l/a/e/u/k/w/r.

        With wait=true (default) it then blocks until the program reacts and
        settles, and returns the resulting screen — so you can act on the
        program's response in the same step."""
        sess = await _term.get_owned_session(session_id, user_id)
        if sess is None:
            return _err("no such session (it may have closed)", session_id=session_id)
        if sess.paused:
            return _err(
                "session is paused — a human has taken over control. Ask them to "
                "resume (or do not send input) until the pause is lifted.",
                session_id=session_id, paused=True,
            )
        if sess.ended:
            return _err("the program in this session has exited", session_id=session_id, ended=True)

        unknown: List[str] = []
        if text:
            sess.send_text(text, enter=False)
        if keys:
            unknown = sess.send_keys(list(keys))
        if enter and text:
            sess.send_keys(["enter"])

        settled = False
        screen = ""
        if wait:
            settled = await sess.wait_idle(
                quiet_secs=max(0.1, float(quiet_secs)),
                timeout=max(0.5, float(timeout)),
            )
            screen = sess.read_text(tail_chars=_DEFAULT_TAIL)
        out = {
            "status": "ok", "session_id": session_id,
            "settled": settled, "ended": sess.ended, "screen": screen,
        }
        if unknown:
            out["unknown_keys"] = unknown
        return json.dumps(out)

    async def terminal_wait(session_id: str, quiet_secs: float = 0.6,
                            timeout: float = 30.0) -> str:
        """Block until the program reacts and then goes quiet for `quiet_secs`
        (i.e. it's done and waiting for input), or `timeout` seconds pass. Use
        this when a command may take a while; then terminal_read the result.
        Returns whether it settled plus the current screen."""
        sess = await _term.get_owned_session(session_id, user_id)
        if sess is None:
            return _err("no such session (it may have closed)", session_id=session_id)
        settled = await sess.wait_idle(
            quiet_secs=max(0.1, float(quiet_secs)),
            timeout=max(0.5, float(timeout)),
        )
        return json.dumps({
            "status": "ok", "session_id": session_id, "settled": settled,
            "ended": sess.ended, "screen": sess.read_text(tail_chars=_DEFAULT_TAIL),
        })

    async def terminal_list() -> str:
        """List your live terminal sessions (agent-opened and human-opened),
        with their names, the command each is running, and whether a human has
        paused/taken one over."""
        sessions = await _term.snapshot_sessions(user_id)
        return json.dumps({"status": "ok", "count": len(sessions), "sessions": sessions})

    async def terminal_close(session_id: str) -> str:
        """Kill a terminal session and the program running in it."""
        closed = await _term.close_session(session_id)
        return json.dumps({"status": "ok" if closed else "error",
                           "closed": closed, "session_id": session_id})

    return {
        "terminal_open": terminal_open,
        "terminal_read": terminal_read,
        "terminal_send": terminal_send,
        "terminal_wait": terminal_wait,
        "terminal_list": terminal_list,
        "terminal_close": terminal_close,
    }


# JSON-Schema parameter definitions, keyed by tool name. The loader pairs these
# with the handlers above when building ToolInfo entries.
TERMINAL_TOOL_SCHEMAS = {
    "terminal_open": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Program/command to run on open, e.g. 'claude', 'python', 'aider'. Blank = plain shell."},
            "name": {"type": "string", "description": "Short label for the session shown to the user (optional)."},
            "wait": {"type": "boolean", "description": "Wait for the program to settle and return the first screen (default true)."},
        },
        "required": [],
    },
    "terminal_read": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "The session to read (from terminal_open/terminal_list)."},
            "tail_chars": {"type": "integer", "description": f"How many trailing characters of screen text to return (default {_DEFAULT_TAIL}, max {_MAX_TAIL})."},
            "raw": {"type": "boolean", "description": "Return raw bytes incl. ANSI codes instead of cleaned text (default false)."},
        },
        "required": ["session_id"],
    },
    "terminal_send": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "The session to type into."},
            "text": {"type": "string", "description": "Text to type (e.g. a command, or an answer to a prompt)."},
            "enter": {"type": "boolean", "description": "Append Enter after `text` so a command runs (default true)."},
            "keys": {"type": "array", "items": {"type": "string"}, "description": "Named special keys to send in order, e.g. ['ctrl+c'] or ['down','enter']."},
            "wait": {"type": "boolean", "description": "After sending, wait for the program to react and settle, then return the screen (default true)."},
            "quiet_secs": {"type": "number", "description": "Seconds of silence that count as 'settled' (default 0.6)."},
            "timeout": {"type": "number", "description": "Max seconds to wait for settle (default 30)."},
        },
        "required": ["session_id"],
    },
    "terminal_wait": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "The session to wait on."},
            "quiet_secs": {"type": "number", "description": "Seconds of silence that count as 'settled' (default 0.6)."},
            "timeout": {"type": "number", "description": "Max seconds to wait (default 30)."},
        },
        "required": ["session_id"],
    },
    "terminal_list": {"type": "object", "properties": {}, "required": []},
    "terminal_close": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "The session to kill."},
        },
        "required": ["session_id"],
    },
}

# Tools that write/act (vs. read-only) — the loader marks these destructive so
# the write-mode guardrail can require confirmation before the agent launches a
# program or types into one.
TERMINAL_DESTRUCTIVE = {"terminal_open", "terminal_send", "terminal_close"}

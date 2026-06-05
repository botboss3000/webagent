"""Terminal-control tools for the manager agent — drive interactive terminal
programs locally (Claude Code, Gemini CLI, Aider, REPLs, any CLI).

Thin wrappers over the local PTY engine in ``webagent/terminal.py``. The open /
send / close tools mutate (launch + type into real programs) so they honour the
``writes_enabled`` gate like run_command. Handlers return human-readable text
(the recent screen) so the manager can reason about what the program shows.
"""

from __future__ import annotations

from typing import List, Optional

from .. import terminal as _term
from .base import ToolContext, WRITES_DISABLED_MSG

_DEFAULT_TAIL = 4000
_MAX_TAIL = 20000


async def terminal_open(ctx: ToolContext, command: str = "", name: str = "",
                        wait: bool = True) -> str:
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    try:
        sid, sess = await _term.open_session(command=command or "", name=name or "")
    except Exception as e:
        return f"(could not open terminal: {e})"
    ctx.audit("terminal_open", {"command": command}, True, sid)
    screen = ""
    if wait:
        await sess.wait_idle(quiet_secs=0.6, timeout=30.0)
        screen = sess.read_text(tail_chars=_DEFAULT_TAIL)
    head = (f"Opened terminal `{sid}`"
            + (f" running: {sess.launch_command}" if sess.launch_command else " (plain shell)")
            + f"{' [exited]' if sess.ended else ''}.\n"
            "Drive it with terminal_send / terminal_read using this session id.")
    return head + ("\n\n--- screen ---\n" + screen if screen else "")


async def terminal_read(ctx: ToolContext, session_id: str,
                        tail_chars: int = _DEFAULT_TAIL, raw: bool = False) -> str:
    sess = _term.get_session(session_id)
    if sess is None:
        return f"(no such session `{session_id}` — it may have closed)"
    tail = max(1, min(int(tail_chars or _DEFAULT_TAIL), _MAX_TAIL))
    screen = sess.read_text(tail_chars=tail, strip_ansi=not raw)
    flag = " [exited]" if sess.ended else ""
    return f"--- screen of `{session_id}`{flag} ---\n{screen}"


async def terminal_send(ctx: ToolContext, session_id: str, text: str = "",
                        enter: bool = True, keys: Optional[List[str]] = None,
                        wait: bool = True, quiet_secs: float = 0.6,
                        timeout: float = 30.0) -> str:
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    sess = _term.get_session(session_id)
    if sess is None:
        return f"(no such session `{session_id}` — it may have closed)"
    if sess.ended:
        return f"(the program in session `{session_id}` has exited)"
    unknown: List[str] = []
    if text:
        sess.send_text(text, enter=False)
    if keys:
        unknown = sess.send_keys(list(keys))
    if enter and text:
        sess.send_keys(["enter"])
    ctx.audit("terminal_send", {"session": session_id, "text": text, "keys": keys}, True, "")
    screen = ""
    if wait:
        await sess.wait_idle(quiet_secs=max(0.1, float(quiet_secs)),
                             timeout=max(0.5, float(timeout)))
        screen = sess.read_text(tail_chars=_DEFAULT_TAIL)
    note = f" (unknown keys ignored: {unknown})" if unknown else ""
    flag = " [exited]" if sess.ended else ""
    return f"Sent to `{session_id}`{note}{flag}." + (f"\n\n--- screen ---\n{screen}" if screen else "")


async def terminal_wait(ctx: ToolContext, session_id: str, quiet_secs: float = 0.6,
                        timeout: float = 30.0) -> str:
    sess = _term.get_session(session_id)
    if sess is None:
        return f"(no such session `{session_id}` — it may have closed)"
    settled = await sess.wait_idle(quiet_secs=max(0.1, float(quiet_secs)),
                                   timeout=max(0.5, float(timeout)))
    flag = " [exited]" if sess.ended else (" [settled]" if settled else " [still busy]")
    return f"--- screen of `{session_id}`{flag} ---\n{sess.read_text(tail_chars=_DEFAULT_TAIL)}"


async def terminal_list(ctx: ToolContext) -> str:
    sessions = _term.list_sessions()
    if not sessions:
        return "No terminal sessions open."
    lines = ["Open terminal sessions:"]
    for s in sessions:
        cmd = s["launch_command"] or "(shell)"
        lines.append(f"- `{s['session_id']}` — {s['name']} — {cmd}"
                     + (" [exited]" if s["ended"] else ""))
    return "\n".join(lines)


async def terminal_close(ctx: ToolContext, session_id: str) -> str:
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    closed = _term.close_session(session_id)
    ctx.audit("terminal_close", {"session": session_id}, closed, "")
    return f"Closed `{session_id}`." if closed else f"(no such session `{session_id}`)"

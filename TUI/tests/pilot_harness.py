"""Pilot test harness for the WebAgent Server-Manager TUI.

Textual's ``App.run_test()`` boots the app into an *off-screen* buffer — no real
terminal window opens — and hands back a ``Pilot`` you can drive: press keys,
click widgets, wait for changes, and inspect the widget tree. This module wraps
the WebAgent-specific boot quirks so individual tests stay short.

Boot quirks this harness handles for you:
  * ``_do_autostart = False``  — never launch the managed uvicorn server in tests.
  * ``cfg.bridge_enabled = False`` — silence the background bridge thread (it
    imports uvicorn, which the test venv need not have).
  * UTF-8 stdout — the chrome uses box-drawing glyphs that crash cp1252 consoles.

Typical use::

    import asyncio
    from pilot_harness import boot, snapshot, hdr_button

    async def main():
        async with boot(size=(110, 34)) as (app, pilot):
            app.query_one("#prompt").focus()
            await pilot.press("h", "i")
            await pilot.pause()
            assert app.query_one("#prompt").text == "hi"
            assert "WEBAGENT" in snapshot(app)

    asyncio.run(main())

These tests deliberately use a plain ``asyncio.run`` driver (like the other
files in this folder) so the suite needs no pytest-asyncio dependency.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

# Make ``webagent`` importable when run as a loose script (tests/ is not a pkg).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Box-drawing chrome must not hit a cp1252 console.
os.environ.setdefault("PYTHONUTF8", "1")
with contextlib.suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from tui_app.app import ServerManagerApp  # noqa: E402
from tui_app.llm import Completion, ToolCall  # noqa: E402


@contextlib.asynccontextmanager
async def boot(size: tuple[int, int] = (110, 34), *, autostart: bool = False):
    """Boot the TUI off-screen and yield ``(app, pilot)``.

    The managed server and the bridge thread are disabled by default so the
    test stays offline and deterministic. Pass ``autostart=True`` only if you
    specifically want to exercise the auto-launch path.
    """
    app = ServerManagerApp()
    app._do_autostart = autostart
    app._supervise = False        # never spawn the external guardian / touch its state files
    app.cfg.bridge_enabled = False
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        yield app, pilot


def snapshot(app) -> str:
    """Return the current off-screen buffer as plain text (one row per line).

    This is the "text screenshot" — what the user would see, minus colour.
    Print it *after* the ``run_test`` block (printing inside the context routes
    through the app's own print-capture and can deadlock/encode-crash).
    """
    strips = app.screen._compositor.render_strips()
    return "\n".join(s.text.rstrip() for s in strips)


def hdr_button(app, action: str):
    """Find a clickable header/category button by its ``_btn_action`` tag.

    Header buttons are ``Static`` widgets with class ``.hdr-btn`` and no id, so
    select by the action they carry, then hand the *widget* to ``pilot.click``::

        await pilot.click(hdr_button(app, "panel_admin"))
    """
    for w in app.query(".hdr-btn"):
        if getattr(w, "_btn_action", None) == action:
            return w
    raise LookupError(f"no .hdr-btn with _btn_action={action!r}")


# ───────────────────────── driving a real turn (the LLM path) ────────────────
#
# A chat turn runs as a Textual worker (`@work(group="agent")`) that calls
# `app.agent.llm.complete(...)`. There are two ways to exercise that path through
# the Pilot:
#
#   A. SCRIPTED — swap in `FakeLLM` so the turn flows through the entire UI
#      pipeline (assistant bubbles, tool-call collapsibles, token HUD, Stop pill)
#      deterministically, offline, and free. This is what you want ~always.
#
#   B. REAL — leave `app.agent.llm` as-is and let it call the live provider. Needs
#      a resolved API key + network, costs tokens, and is nondeterministic. Only
#      for the occasional "does the real provider wiring work" smoke test.


class FakeLLM:
    """A scripted stand-in for ``LLMClient``.

    Feed it a list of ``Completion`` objects (use ``assistant()`` / ``call()``
    below to build them). Each ``complete()`` returns the next one. When the
    script runs dry it returns a benign default forever — so background callers
    that also use the LLM (e.g. the post-turn session-title generator) never
    crash the test by exhausting the script.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.messages_seen = []

    async def complete(self, messages, tools=None, temperature=0.0, max_tokens=4096):
        self.messages_seen.append(messages)
        i, self.calls = self.calls, self.calls + 1
        if i < len(self.script):
            return self.script[i]
        return Completion(content="(ok)")        # default once the script is spent

    async def aclose(self):
        pass


def assistant(text: str) -> Completion:
    """Scripted final/assistant message (no tool calls)."""
    return Completion(content=text)


def call(name: str, arguments: dict, *, content: str = "", _id: str = "t1") -> Completion:
    """Scripted assistant turn that invokes one tool. The agent executes the tool
    for real and loops back for the next scripted Completion (usually an
    ``assistant(...)`` final answer). Mirrors the pattern in test_event_loop.py."""
    return Completion(
        content=content,
        tool_calls=[ToolCall(id=_id, name=name, arguments=arguments)],
        raw={"tool_calls": [{"id": _id, "type": "function",
                             "function": {"name": name, "arguments": "{}"}}]},
    )


def use_fake_llm(app, script) -> "FakeLLM":
    """Install a scripted LLM on the running app and return it (inspect ``.calls``
    / ``.messages_seen`` afterwards). Also pins session naming to ``off`` so the
    post-turn title worker doesn't fire an extra LLM call."""
    fake = FakeLLM(script)
    app.llm = fake
    app.agent.llm = fake
    app.cfg.session_name_mode = "off"
    return fake


async def drive_turn(app, pilot, text: str, *, timeout: float = 5.0) -> None:
    """Submit ``text`` through the prompt exactly as a user would (set the pill
    text, fire its submit action) and wait for the agent worker to settle.

    Waits by polling ``app._busy`` rather than ``workers.wait_for_complete()`` —
    the watchdog runs as a never-ending worker, so waiting for *all* workers would
    hang. Install a ``FakeLLM`` first (see ``use_fake_llm``) unless you really want
    a live provider call.
    """
    import asyncio

    prompt = app.query_one("#prompt")
    prompt.focus()
    await pilot.pause()
    prompt.text = text
    # Press Enter as a real key event so the Submitted message is posted from the
    # widget's own pump context (its `control` resolves to #prompt). Calling
    # action_submit() directly leaves the message sender unset -> OnNoWidget.
    await pilot.press("enter")
    await pilot.pause()                 # let the worker spin up + flip _busy
    waited, step = 0.0, 0.05
    while app._busy and waited < timeout:
        await asyncio.sleep(step)
        await pilot.pause()
        waited += step
    await pilot.pause()                 # final settle so the transcript paints


def visible_text(app, selector: str) -> str:
    """Concatenated plain text of every widget matching ``selector``."""
    out = []
    for w in app.query(selector):
        with contextlib.suppress(Exception):
            out.append(w.render_line(0).text if hasattr(w, "render_line") else "")
    return "\n".join(out)

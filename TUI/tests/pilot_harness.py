"""Pilot test harness for the webAgent Server-Manager TUI.

Textual's ``App.run_test()`` boots the app into an *off-screen* buffer — no real
terminal window opens — and hands back a ``Pilot`` you can drive: press keys,
click widgets, wait for changes, and inspect the widget tree. This module wraps
the webAgent-specific boot quirks so individual tests stay short.

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

from webagent.app import ServerManagerApp  # noqa: E402


@contextlib.asynccontextmanager
async def boot(size: tuple[int, int] = (110, 34), *, autostart: bool = False):
    """Boot the TUI off-screen and yield ``(app, pilot)``.

    The managed server and the bridge thread are disabled by default so the
    test stays offline and deterministic. Pass ``autostart=True`` only if you
    specifically want to exercise the auto-launch path.
    """
    app = ServerManagerApp()
    app._do_autostart = autostart
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


def visible_text(app, selector: str) -> str:
    """Concatenated plain text of every widget matching ``selector``."""
    out = []
    for w in app.query(selector):
        with contextlib.suppress(Exception):
            out.append(w.render_line(0).text if hasattr(w, "render_line") else "")
    return "\n".join(out)

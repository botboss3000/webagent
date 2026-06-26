"""Pilot test for the keep-alive guardian wiring in the TUI.

Boots the app off-screen with supervision ENABLED but the real guardian spawn
**stubbed** (so no detached process is launched), pointed at a throwaway data
dir. Verifies the on-mount lifecycle (PID heartbeat written, stale clean-exit
marker cleared, guardian ensured) and the ADMIN ``[Keep-alive: ON/OFF]`` toggle
(label flips, ``guardian.json`` flips, panel stays open). Run with the TUI venv::

    PYTHONUTF8=1 ../.venv/Scripts/python.exe test_tui_pilot_guardian.py
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

# Isolate the data dir + force UTF-8 BEFORE importing the app/package.
os.environ["WEBAGENT_DATA_DIR"] = tempfile.mkdtemp(prefix="wa-guardian-pilot-")
os.environ.setdefault("PYTHONUTF8", "1")
import sys  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
with contextlib.suppress(Exception):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import asyncio  # noqa: E402

from webagent import guardian  # noqa: E402
from tui_app.app import ServerManagerApp  # noqa: E402

# ── stub the real process side-effects (no subprocess spawned in tests) ────────
_calls = {"ensure": 0, "stop": 0}
guardian.ensure_running = lambda: _calls.__setitem__("ensure", _calls["ensure"] + 1) or True
guardian.stop_guardian = lambda: _calls.__setitem__("stop", _calls["stop"] + 1) or True


@contextlib.asynccontextmanager
async def boot_supervised(size=(110, 34)):
    app = ServerManagerApp()
    app._do_autostart = False
    app._supervise = True            # the behaviour under test
    app.cfg.bridge_enabled = False
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        yield app, pilot


def ka_button(app):
    for w in app.query(".panel-btn"):
        if getattr(w, "_btn_action", None) == "guardian_toggle":
            return w
    raise LookupError("no [Keep-alive] button in the panel")


def ka_label(app) -> str:
    btn = ka_button(app)
    with contextlib.suppress(Exception):
        return btn.render_line(0).text
    return ""


async def main() -> None:
    # Pre-seed a STALE clean-exit marker; on_mount must clear it.
    guardian.write_clean_exit()
    assert guardian._clean_exit_file().exists()
    guardian.write_enabled(True)     # start ON (the default)

    async with boot_supervised() as (app, pilot):
        # --- on-mount lifecycle ---
        await pilot.pause()
        assert guardian._read_pid(guardian._tpid_file()) == os.getpid(), \
            "on_mount must record the TUI's PID heartbeat"
        assert not guardian._clean_exit_file().exists(), \
            "on_mount must clear a stale clean-exit marker"
        assert _calls["ensure"] >= 1, "on_mount must ensure the guardian is running"
        print("  ok  on_mount: pid written, stale marker cleared, guardian ensured")

        # --- ADMIN panel shows the toggle, label reflects ON, wiring is correct ---
        app._open_panel("admin")
        await pilot.pause()
        assert ka_button(app) is not None, "ADMIN must render a [Keep-alive] button"
        assert "guardian_toggle" in app._KEEP_OPEN, \
            "the toggle must keep the panel open (in _KEEP_OPEN)"
        assert "ON" in ka_label(app), f"label should read ON, got {ka_label(app)!r}"
        print("  ok  ADMIN shows [Keep-alive: ON]; toggle wired + KEEP_OPEN")

        # --- activate the toggle (the exact handler a click dispatches to) → OFF ---
        ensure_before = _calls["ensure"]
        app.action_guardian_toggle()
        await pilot.pause()
        await pilot.pause()          # let _rebuild_panel + the toggle worker settle
        assert guardian.read_enabled() is False, "toggle should disable the guardian"
        assert app._panel_kind == "admin", "panel must stay open"
        assert "OFF" in ka_label(app), "label must flip to OFF"
        assert _calls["stop"] >= 1, "turning OFF must call stop_guardian"
        print("  ok  toggle → OFF: state flips, label flips, panel stays open, stop called")

        # --- toggle again → back ON and re-ensures ---
        app.action_guardian_toggle()
        await pilot.pause()
        await pilot.pause()
        assert guardian.read_enabled() is True, "second toggle should re-enable"
        assert "ON" in ka_label(app), "label must flip back to ON"
        assert _calls["ensure"] > ensure_before, "turning ON must re-ensure the guardian"
        print("  ok  toggle → ON: state flips back, guardian re-ensured")

    print("\nTUI pilot guardian: ALL PASSED")


asyncio.run(main())

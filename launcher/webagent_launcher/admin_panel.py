"""The expandable server-control panel for the single-screen chat.

In the old two-screen launcher these controls lived on the home page. Now they
live here: an overlay panel that slides in over the chat (on its own CSS layer,
docked to an edge, so the transcript doesn't reflow) and holds three sections —

  1. server status   (the migrated status dot / text / pid / uptime / target)
  2. control buttons  (Launch / Restart / Stop / Browser / App Win / Update /
                       Clear DB / Reset Py / Full Reset / Location / Quit)
  3. server log       (the migrated ``#log`` RichLog — install/update/server
                       stdout streams here, exactly as before)

The buttons call straight back into the app's existing ``action_*`` methods, so
the server lifecycle logic is unchanged. The chat screen owns showing/hiding the
panel and pushes server-state updates into it (see ChatScreen.notify_server_state).
"""

from __future__ import annotations

import asyncio

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, RichLog, Static

from .server import ServerState

# ASCII-only status indicator (safe in any terminal font) — mirrors app._status_glyph.
_STATUS_GLYPH = {
    "running": "[RUN]",
    "starting": "[...]",
    "stopping": "[...]",
    "error": "[ERR]",
    "stopped": "[OFF]",
}


class AdminPanel(Vertical):
    """Server status + control buttons + server log. Toggled over the chat."""

    # button id -> the app action method it invokes (same mapping the old home
    # screen used in app._btn).
    _ACTIONS = {
        "btn-launch": "action_launch",
        "btn-restart": "action_restart",
        "btn-stop": "action_stop_server",
        "btn-browser": "action_open_browser",
        "btn-appwindow": "action_open_app_window",
        "btn-location": "action_change_target",
        "btn-update": "action_update",
        "btn-cleardb": "action_clear_db",
        "btn-resetpy": "action_reset_python",
        "btn-fullreset": "action_full_reset",
        "btn-quit": "action_request_quit",
    }

    def __init__(self) -> None:
        super().__init__(id="admin-panel")
        self._ready = False

    def compose(self) -> ComposeResult:
        yield Static("SERVER CONTROL", id="admin-title")
        with Horizontal(id="admin-status"):
            yield Static("[OFF]", id="status-dot")
            yield Static("status: stopped", id="status-text")
            yield Static("pid: -", id="pid-text")
        with Horizontal(id="admin-status2"):
            yield Static("uptime: -", id="uptime-text")
            yield Static("target: -", id="target-text")
        with Vertical(id="admin-buttons"):
            with Horizontal(classes="admin-btn-row"):
                yield Button("Launch", id="btn-launch", classes="primary")
                yield Button("Restart", id="btn-restart")
                yield Button("Stop", id="btn-stop")
            with Horizontal(classes="admin-btn-row"):
                yield Button("Browser", id="btn-browser")
                yield Button("App Win", id="btn-appwindow")
                yield Button("Location", id="btn-location", classes="muted")
            with Horizontal(classes="admin-btn-row"):
                yield Button("Update", id="btn-update", classes="muted")
                yield Button("Clear DB", id="btn-cleardb", classes="danger")
                yield Button("Reset Py", id="btn-resetpy", classes="danger")
            with Horizontal(classes="admin-btn-row"):
                yield Button("Full Reset", id="btn-fullreset", classes="danger")
                yield Button("Quit", id="btn-quit", classes="muted")
        yield Static("SERVER LOG", id="admin-log-title")
        yield RichLog(highlight=False, markup=False, wrap=False, id="log")

    def on_mount(self) -> None:
        # Replay any log lines the app buffered before this panel existed (e.g.
        # the boot/auto-start lines emitted while the chat was still mounting).
        snapshot = list(getattr(self.app, "_log_lines", []))
        self._ready = True
        for line in snapshot:
            self.write_log(line)

    @on(Button.Pressed)
    async def _btn(self, event: Button.Pressed) -> None:
        name = self._ACTIONS.get(event.button.id or "")
        if not name:
            return
        event.stop()
        fn = getattr(self.app, name, None)
        if fn is None:
            return
        result = fn()
        if asyncio.iscoroutine(result):
            await result

    # ── state plumbing (driven by the chat screen) ──────────────────────
    def set_state(self, state: ServerState) -> None:
        try:
            dot = self.query_one("#status-dot", Static)
            dot.update(_STATUS_GLYPH.get(state.status, "[OFF]"))
            dot.set_classes(state.status)
            self.query_one("#status-text", Static).update(f"status: {state.status}")
            self.query_one("#pid-text", Static).update(
                f"pid: {state.pid if state.pid else '-'}"
            )
            self.query_one("#uptime-text", Static).update(
                f"uptime: {state.uptime_str() if state.started_at else '-'}"
            )
            btn = self.query_one("#btn-launch", Button)
            btn.label = "Launch\n& Browser" if state.status == "running" else "Launch"
        except Exception:
            pass

    def set_uptime(self, state: ServerState) -> None:
        try:
            self.query_one("#uptime-text", Static).update(
                f"uptime: {state.uptime_str() if state.started_at else '-'}"
            )
        except Exception:
            pass

    def set_target(self, pdir) -> None:
        try:
            self.query_one("#target-text", Static).update(
                f"target: {pdir if pdir else '(none - set one in Location)'}"
            )
        except Exception:
            pass

    def write_log(self, line: str) -> None:
        try:
            self.query_one("#log", RichLog).write(line)
        except Exception:
            pass

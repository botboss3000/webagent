"""Main Textual TUI — webAgent Android launcher.

Tabs:
  Server  — Launch / Restart / Kill / Browser buttons + status badge
  Doctor  — dep checks with per-row Fix buttons and a Fix-All button
  Logs    — scrolling RichLog from the server subprocess + fix output
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Footer, Header, Label, RichLog, Static, TabbedContent, TabPane

from . import fixes
from .doctor import Check, DoctorReport, run_doctor
from .server import HEALTH_URL, WEBAGENT_PORT, ServerController, ServerState


BROWSER_BRIDGE_PATH = Path(os.environ.get("WEBAGENT_BROWSER_BRIDGE", "/tmp/webagent_open.url"))


_SEVERITY_GLYPH = {
    "ok": "OK",
    "warn": "!",
    "fail": "X",
    "unknown": "?",
}


def _status_text(state: ServerState) -> str:
    if state.status == "running":
        return f"● running · pid {state.pid} · {state.uptime_str()}"
    if state.status == "starting":
        return "● starting…"
    if state.status == "stopping":
        return "● stopping…"
    if state.status == "error":
        return f"● error: {state.last_error or 'unknown'}"
    return "○ stopped"


class CheckRow(Horizontal):
    """One row in the doctor list."""

    DEFAULT_CSS = ""  # styled from styles.tcss

    def __init__(self, check: Check) -> None:
        super().__init__(classes="check-row")
        self.check = check

    def compose(self) -> ComposeResult:
        sev = self.check.severity
        yield Label(_SEVERITY_GLYPH.get(sev, "?"), classes=f"-sev -{sev}")
        body = f"{self.check.label} — {self.check.detail}" if self.check.detail else self.check.label
        yield Label(body, classes="-body")
        if self.check.fix_action:
            yield Button("Fix", id=f"fix:{self.check.fix_action}", classes="-fix")


class LauncherApp(App):
    TITLE = "webAgent"
    SUB_TITLE = "Android launcher"
    CSS_PATH = "styles.tcss"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("l", "launch", "Launch"),
        Binding("r", "restart", "Restart"),
        Binding("k", "kill", "Kill"),
        Binding("b", "browser", "Browser"),
        Binding("d", "doctor", "Doctor"),
        Binding("f", "fix_all", "Fix-all"),
    ]

    def __init__(self, project_dir: Path) -> None:
        super().__init__()
        self.project_dir = project_dir
        self.controller: ServerController | None = None
        self._doctor_report: DoctorReport | None = None
        self._fix_busy = False

    # ── compose ────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)

        with Horizontal(id="header"):
            yield Static(f"webAgent · {self.project_dir.name}", id="title")
            yield Static("○ stopped", id="status-badge", classes="-stopped")

        with Container(id="button-row"):
            yield Button("Launch (L)", id="btn-launch", classes="-primary")
            yield Button("Restart (R)", id="btn-restart")
            yield Button("Kill (K)", id="btn-kill", classes="-danger")
            yield Button("Browser (B)", id="btn-browser")

        with TabbedContent(initial="tab-logs"):
            with TabPane("Logs", id="tab-logs"):
                yield RichLog(
                    highlight=False, markup=False, wrap=True, id="log",
                )
            with TabPane("Doctor", id="tab-doctor"):
                yield VerticalScroll(id="doctor-list")
                with Horizontal(id="doctor-toolbar"):
                    yield Button("Re-scan (D)", id="btn-doctor-rescan")
                    yield Button("Fix all (F)", id="btn-fix-all", classes="-primary")

        yield Static(
            f"L=Launch  R=Restart  K=Kill  B=Browser  D=Doctor  F=Fix-all  Q=Quit  ·  {HEALTH_URL}",
            id="footer-hint",
        )
        yield Footer()

    # ── lifecycle ──────────────────────────────────────────────────────
    async def on_mount(self) -> None:
        self.controller = ServerController(
            project_dir=self.project_dir,
            on_state_change=self._on_state,
            on_log_line=self._on_log_line,
        )
        self._log(f"[launcher] project: {self.project_dir}")
        self._log(f"[launcher] ready. press L to launch the server, D for doctor.")
        # initial doctor run in background
        self.run_worker(self._refresh_doctor(), exclusive=True)
        # periodic uptime refresh
        self.set_interval(1.0, self._tick_uptime)

    # ── log helpers ────────────────────────────────────────────────────
    def _log(self, line: str) -> None:
        try:
            self.query_one("#log", RichLog).write(line)
        except Exception:
            pass

    def _on_log_line(self, line: str) -> None:
        self._log(line)

    _STATUS_CLASSES = ("-running", "-starting", "-stopping", "-stopped", "-error")

    def _on_state(self, state: ServerState) -> None:
        try:
            badge = self.query_one("#status-badge", Static)
            badge.update(_status_text(state))
            for c in self._STATUS_CLASSES:
                badge.remove_class(c)
            badge.add_class(f"-{state.status}")
        except Exception:
            pass

    def _tick_uptime(self) -> None:
        if self.controller and self.controller.state.status == "running":
            self._on_state(self.controller.state)

    # ── actions ────────────────────────────────────────────────────────
    async def action_launch(self) -> None:
        if self.controller:
            await self.controller.start()

    async def action_restart(self) -> None:
        if self.controller:
            await self.controller.restart()

    async def action_kill(self) -> None:
        if self.controller:
            await self.controller.stop()

    async def action_browser(self) -> None:
        """Write the URL to a bridge file the Termux side can poll, and log it.

        From inside a proot we can't reach termux-open-url directly. The
        recommended pattern is: have the Termux shim watch the bridge file
        and call termux-open-url whenever it changes. We always log the URL
        so the user can also tap it from Termux clipboard.
        """
        url = f"http://localhost:{WEBAGENT_PORT}/index.html"
        try:
            BROWSER_BRIDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
            BROWSER_BRIDGE_PATH.write_text(url + "\n")
            self._log(f"[launcher] browser → {url}  (bridge: {BROWSER_BRIDGE_PATH})")
        except OSError as e:
            self._log(f"[launcher] browser: could not write bridge ({e}); URL is {url}")

    async def action_doctor(self) -> None:
        await self._refresh_doctor()

    async def action_fix_all(self) -> None:
        if self._fix_busy:
            self._log("[launcher] fix already running")
            return
        self.run_worker(self._run_fix("__all__"), exclusive=True, name="fix")

    # ── button routing ─────────────────────────────────────────────────
    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "btn-launch":
            await self.action_launch()
        elif bid == "btn-restart":
            await self.action_restart()
        elif bid == "btn-kill":
            await self.action_kill()
        elif bid == "btn-browser":
            await self.action_browser()
        elif bid == "btn-doctor-rescan":
            await self.action_doctor()
        elif bid == "btn-fix-all":
            await self.action_fix_all()
        elif bid.startswith("fix:"):
            action = bid[len("fix:"):]
            self.run_worker(self._run_fix(action), exclusive=True, name="fix")

    # ── doctor ─────────────────────────────────────────────────────────
    async def _refresh_doctor(self) -> None:
        self._log("[doctor] scanning environment...")
        loop = asyncio.get_event_loop()
        report = await loop.run_in_executor(None, run_doctor, self.project_dir)
        self._doctor_report = report
        self._log(
            f"[doctor] {report.ok_count} ok · {report.warn_count} warn · "
            f"{report.fail_count} fail  →  overall: {report.overall.upper()}"
        )
        try:
            container = self.query_one("#doctor-list", VerticalScroll)
            await container.remove_children()
            for check in report.checks:
                await container.mount(CheckRow(check))
        except Exception as e:
            self._log(f"[doctor] UI error: {e}")

    # ── fixes ──────────────────────────────────────────────────────────
    async def _run_fix(self, action: str) -> None:
        if self._fix_busy:
            self._log("[launcher] fix already running, ignoring")
            return
        self._fix_busy = True
        try:
            if action == "__all__":
                gen = fixes.fix_all(self.project_dir)
            else:
                gen = fixes.dispatch(action, self.project_dir)
            async for line in gen:
                self._log(line)
        except Exception as e:
            self._log(f"[fix] CRASH: {e}")
        finally:
            self._fix_busy = False
        # auto-rescan after a fix
        await self._refresh_doctor()

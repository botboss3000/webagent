"""Main Textual application — the launcher home screen."""

from __future__ import annotations

import asyncio
import webbrowser
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical  # noqa: F401
from textual.widgets import Button, Footer, Label, RichLog, Static

from .config import LauncherConfig
from .palette import build_palette_from_config
from .reset import clear_database, full_reset, reset_venv
from .screens import ConfirmModal, SettingsScreen, SetupScreen
from .server import HEALTH_URL, ServerController, ServerState
from .stage import AnimatedStage


def _status_glyph(status: str) -> str:
    """ASCII-only status indicator (safe in any terminal font)."""
    return {
        "running":  "[RUN]",
        "starting": "[...]",
        "stopping": "[...]",
        "error":    "[ERR]",
        "stopped":  "[OFF]",
    }.get(status, "[OFF]")


class LauncherApp(App):
    TITLE = "webagent launcher"
    SUB_TITLE = "agent harness control surface"
    CSS_PATH = "styles.tcss"

    BINDINGS = [
        Binding("ctrl+c", "request_quit", "Quit"),
        Binding("q", "request_quit", "Quit"),
        Binding("l", "launch", "Launch"),
        Binding("r", "restart", "Restart"),
        Binding("s", "stop_server", "Stop"),
        Binding("b", "open_browser", "Browser"),
        Binding("d", "clear_db", "Clear DB"),
        Binding("p", "reset_python", "Reset Py"),
        Binding("f", "full_reset", "Full reset"),
        Binding("t", "theme", "Theme"),
        Binding("c", "cycle_theme", "Cycle theme"),
        Binding("space", "cycle_anim", "Cycle anim"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg = LauncherConfig.load()
        self.controller: ServerController | None = None
        self._stage: AnimatedStage | None = None

    # ── compose ────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        palette = build_palette_from_config(self.cfg)
        self._stage = AnimatedStage(
            palette=palette,
            char_ramp=self.cfg.char_ramp,
            fps=self.cfg.fps,
            style=self.cfg.animation_style,
            speed=self.cfg.theme_speed,
            intensity=self.cfg.animation_intensity,
        )
        self._stage.id = "stage-widget"

        # Plain vertical stack — Textual lays each child in order top→bottom.
        # No dock needed; #body has height: 1fr so it absorbs all slack.

        # row 0
        with Horizontal(id="status-bar"):
            yield Static("[OFF]", id="status-dot")
            yield Static("status: stopped", id="status-text")
            yield Static("pid: -", id="pid-text")
            yield Static("uptime: -", id="uptime-text")
            yield Static("url: " + HEALTH_URL, id="url-text")

        # rows 1-3
        with Horizontal(id="header-buttons"):
            yield Button("Launch",  id="btn-launch",  classes="primary")
            yield Button("Restart", id="btn-restart")
            yield Button("Stop",    id="btn-stop")
            yield Button("Browser", id="btn-browser")
            yield Button("Theme",   id="btn-theme",   classes="muted")

        # middle (1fr) — stage + log share remaining height
        with Vertical(id="body"):
            yield self._stage
            with Container(id="log-pane"):
                yield RichLog(
                    highlight=False, markup=False, wrap=False, id="log"
                )

        # rows N-3 .. N-1
        with Horizontal(id="footer-buttons"):
            yield Button("Clear DB",   id="btn-cleardb",   classes="danger")
            yield Button("Reset Py",   id="btn-resetpy",   classes="danger")
            yield Button("Full Reset", id="btn-fullreset", classes="danger")
            yield Button("Quit",       id="btn-quit",      classes="muted")

        # row N
        yield Static(
            "  L:launch  R:restart  S:stop  B:browser  D:clear-db  "
            "P:reset-py  F:full-reset  T:theme  C:cycle  Space:anim  Q:quit",
            id="footer-bar",
        )

    # ── lifecycle ─────────────────────────────────────────────────────
    def on_mount(self) -> None:
        initial = ServerState()
        self._update_status_widgets(initial)
        self._update_launch_button(initial)
        # Heartbeat for uptime display
        self.set_interval(1.0, self._refresh_uptime)
        if not self.cfg.is_valid_project():
            # Push setup non-blocking; finalize via callback
            self.push_screen(SetupScreen(self.cfg), self._after_setup)
        else:
            self._after_setup(True)

    def _after_setup(self, ok: bool | None) -> None:
        if self.cfg.is_valid_project():
            self._init_controller()
            self._log(f"[launcher] project: {self.cfg.project_path}")
            self._log("[launcher] press [L] to launch, [T] for theme, [Q] to quit")
        else:
            self._log("[launcher] no project configured — press [T] to configure")

    def _init_controller(self) -> None:
        pdir = self.cfg.project_dir()
        if not pdir:
            return
        self.controller = ServerController(
            project_dir=pdir,
            on_state_change=self._on_state_change,
            on_log_line=self._on_log_line,
        )

    # ── status / log plumbing ─────────────────────────────────────────
    def _log(self, line: str) -> None:
        try:
            self.query_one("#log", RichLog).write(line)
        except Exception:
            pass

    def _on_log_line(self, line: str) -> None:
        self.call_from_thread(self._log, line) if False else self._log(line)

    def _on_state_change(self, state: ServerState) -> None:
        self._update_status_widgets(state)
        self._update_launch_button(state)

    def _update_launch_button(self, state: ServerState | None = None) -> None:
        """Reflect server state in the Launch button label.

        Stopped / error / starting / stopping → 'Launch' (one line).
        Running → 'Launch\n& Browser' (clicking again opens the browser).
        """
        try:
            if state is None and self.controller is not None:
                state = self.controller.state
            running = state is not None and state.status == "running"
            btn = self.query_one("#btn-launch", Button)
            btn.label = "Launch\n& Browser" if running else "Launch"
        except Exception:
            pass

    def _update_status_widgets(self, state: ServerState) -> None:
        try:
            dot = self.query_one("#status-dot", Static)
            dot.update(_status_glyph(state.status))
            dot.set_classes(state.status)
            self.query_one("#status-text", Static).update(f"status: {state.status}")
            self.query_one("#pid-text", Static).update(
                f"pid: {state.pid if state.pid else '-'}"
            )
            self.query_one("#uptime-text", Static).update(
                f"uptime: {state.uptime_str() if state.started_at else '-'}"
            )
        except Exception:
            pass

    def _refresh_uptime(self) -> None:
        if self.controller and self.controller.state.started_at:
            try:
                self.query_one("#uptime-text", Static).update(
                    f"uptime: {self.controller.state.uptime_str()}"
                )
            except Exception:
                pass

    # ── actions ───────────────────────────────────────────────────────
    async def action_launch(self) -> None:
        """Two-step Launch button:

        1st click  (server stopped)  → start the server
        2nd click  (server running)  → open the browser
        """
        if not self._require_project():
            return
        assert self.controller
        status = self.controller.state.status
        if status == "running":
            # Server already up — open the browser.
            self.action_open_browser()
            return
        if status in ("starting", "stopping"):
            # Mid-transition; don't double-fire. Just leave a note.
            self._log(f"[launcher] server is {status}; please wait")
            return
        await self.controller.start()
        # No auto-open browser — the user clicks again to do that.

    async def action_restart(self) -> None:
        if not self._require_project():
            return
        assert self.controller
        await self.controller.restart()

    async def action_stop_server(self) -> None:
        if self.controller:
            await self.controller.stop()

    def action_open_browser(self) -> None:
        url = self.cfg.last_browser_url or HEALTH_URL
        webbrowser.open(url)
        self._log(f"[launcher] opened browser → {url}")

    async def action_clear_db(self) -> None:
        if not self._require_project():
            return

        def _go(confirmed: bool | None) -> None:
            if not confirmed:
                return
            asyncio.create_task(self._do_clear_db())

        self.push_screen(
            ConfirmModal(
                "Clear database?",
                "Wipes app/db/local.db and visuals/users/ (backed up to temp/).\n"
                "Server will restart automatically. Proceed?",
            ),
            _go,
        )

    async def _do_clear_db(self) -> None:
        assert self.controller and self.cfg.project_dir()
        await clear_database(self.cfg.project_dir(), self.controller, self._log, backup=True)

    async def action_reset_python(self) -> None:
        if not self._require_project():
            return

        def _go(confirmed: bool | None) -> None:
            if not confirmed:
                return
            asyncio.create_task(self._do_reset_python())

        self.push_screen(
            ConfirmModal(
                "Reset Python environment?",
                "Deletes .venv and re-runs `uv sync` (downloads packages again).\n"
                "Server will restart automatically. Proceed?",
            ),
            _go,
        )

    async def _do_reset_python(self) -> None:
        assert self.controller and self.cfg.project_dir()
        await reset_venv(self.cfg.project_dir(), self.controller, self._log)

    async def action_full_reset(self) -> None:
        if not self._require_project():
            return

        def _go(confirmed: bool | None) -> None:
            if not confirmed:
                return
            asyncio.create_task(self._do_full_reset())

        self.push_screen(
            ConfirmModal(
                "FULL reset?",
                "Clears DB + wipes .venv + re-runs `uv sync`.\nServer restarts. Proceed?",
            ),
            _go,
        )

    async def _do_full_reset(self) -> None:
        assert self.controller and self.cfg.project_dir()
        await full_reset(self.cfg.project_dir(), self.controller, self._log)

    def action_theme(self) -> None:
        self.push_screen(
            SettingsScreen(self.cfg, self._apply_visual_config),
            lambda _ok: self._apply_visual_config(),
        )

    def action_cycle_theme(self) -> None:
        """Cycle through color presets without opening the settings panel."""
        from .palette import PRESETS, apply_preset_to_config
        # Find current preset
        idx = 0
        for i, p in enumerate(PRESETS):
            if p.color_a.lower() == self.cfg.theme_color_a.lower() and p.mode == self.cfg.theme_mode:
                idx = i
                break
        nxt = PRESETS[(idx + 1) % len(PRESETS)]
        apply_preset_to_config(nxt, self.cfg)
        self.cfg.save()
        self._apply_visual_config()
        self._log(f"[launcher] theme → {nxt.name}")

    def action_cycle_anim(self) -> None:
        from .ascii_anim import ANIM_STYLES
        cur = self.cfg.animation_style if self.cfg.animation_style in ANIM_STYLES else "plasma"
        idx = ANIM_STYLES.index(cur)
        new = ANIM_STYLES[(idx + 1) % len(ANIM_STYLES)]
        self.cfg.animation_style = new
        self.cfg.save()
        self._apply_visual_config()
        self._log(f"[launcher] animation → {new}")

    async def action_request_quit(self) -> None:
        if self.controller and self.controller.state.status in ("running", "starting"):
            def _go(confirmed: bool | None) -> None:
                if confirmed:
                    asyncio.create_task(self._quit_after_stop())
            self.push_screen(
                ConfirmModal(
                    "Quit launcher?",
                    "The webagent server is still running. Stop it before exiting?",
                ),
                _go,
            )
        else:
            self.exit()

    async def _quit_after_stop(self) -> None:
        if self.controller:
            await self.controller.stop()
        self.exit()

    # ── button → action wiring ────────────────────────────────────────
    @on(Button.Pressed)
    async def _btn(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        mapping = {
            "btn-launch": self.action_launch,
            "btn-restart": self.action_restart,
            "btn-stop": self.action_stop_server,
            "btn-browser": self.action_open_browser,
            "btn-cleardb": self.action_clear_db,
            "btn-resetpy": self.action_reset_python,
            "btn-fullreset": self.action_full_reset,
            "btn-theme": self.action_theme,
            "btn-quit": self.action_request_quit,
        }
        fn = mapping.get(bid)
        if fn:
            result = fn()
            if asyncio.iscoroutine(result):
                await result

    # ── helpers ───────────────────────────────────────────────────────
    def _require_project(self) -> bool:
        if self.controller and self.cfg.is_valid_project():
            return True
        self._log("[launcher] ERROR: no valid project configured. Open settings or relaunch.")
        return False

    def _apply_visual_config(self) -> None:
        palette = build_palette_from_config(self.cfg)
        if self._stage is not None:
            self._stage.set_palette(palette)
            self._stage.set_style(self.cfg.animation_style)
            self._stage.set_speed(self.cfg.theme_speed)
            self._stage.set_intensity(self.cfg.animation_intensity)
            self._stage.set_ramp(self.cfg.char_ramp)
            self._stage.set_fps(self.cfg.fps)

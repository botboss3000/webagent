"""Main Textual application — the launcher home screen."""

from __future__ import annotations

import asyncio
import webbrowser
from pathlib import Path

from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical  # noqa: F401
from textual.widgets import Button, Footer, Label, RichLog, Static

from .chat_screen import ChatScreen
from .config import LauncherConfig
from .palette import build_palette_from_config
from .reset import clear_database, full_reset, reset_venv
from .screens import ConfirmModal, InstallScreen, SettingsPanel, SetupResult, SetupScreen
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
        # Ctrl+C is intentionally NOT bound to quit — it must stay free for the
        # chat editor's copy. Esc is the exit key; Q is a convenience alias.
        Binding("q", "request_quit", "Quit"),
        Binding("escape", "home_escape", "Exit", show=False),
        # Ctrl+Q = "go back to last" — a single priority toggle that flips
        # between home and chat from EITHER screen. Priority + a plain control
        # code make it the reliable fallback in any terminal.
        Binding("ctrl+q", "go_back_last", "Chat", show=False, priority=True),
        Binding("a", "open_chat", "Chat"),
        Binding("l", "launch", "Launch"),
        Binding("r", "restart", "Restart"),
        Binding("s", "stop_server", "Stop"),
        Binding("b", "open_browser", "Browser"),
        Binding("d", "clear_db", "Clear DB"),
        Binding("p", "reset_python", "Reset Py"),
        Binding("f", "full_reset", "Full reset"),
        Binding("u", "update", "Update"),
        Binding("t", "theme", "Theme"),
        Binding("c", "cycle_theme", "Cycle theme"),
        Binding("space", "cycle_anim", "Cycle anim"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg = LauncherConfig.load()
        self.controller: ServerController | None = None
        self._chat_screen: ChatScreen | None = None  # set while chat is open
        self._stage: AnimatedStage | None = None
        self._settings: SettingsPanel | None = None
        # Idle = pause the animation. Two independent reasons compose via OR:
        #   _blurred       → terminal/app lost focus
        #   _chat_covering → the chat screen is pushed over the home stage
        self._blurred = False
        self._chat_covering = False

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
            yield Button("Chat",    id="btn-chat",    classes="primary")
            yield Button("Launch",  id="btn-launch")
            yield Button("Restart", id="btn-restart")
            yield Button("Stop",    id="btn-stop")
            yield Button("Browser", id="btn-browser")
            yield Button("Theme",   id="btn-theme",   classes="muted")

        # middle (1fr) — stage on top; the bottom slot holds EITHER the log
        # pane OR the inline settings panel (toggled by Theme). The stage stays
        # visible and live-animating while settings are open.
        with Vertical(id="body"):
            yield self._stage
            with Container(id="log-pane"):
                yield RichLog(
                    highlight=False, markup=False, wrap=False, id="log"
                )
            self._settings = SettingsPanel(
                self.cfg, self._apply_visual_config, self._close_settings
            )
            yield self._settings

        # rows N-3 .. N-1
        with Horizontal(id="footer-buttons"):
            yield Button("Update",     id="btn-update",    classes="muted")
            yield Button("Clear DB",   id="btn-cleardb",   classes="danger")
            yield Button("Reset Py",   id="btn-resetpy",   classes="danger")
            yield Button("Full Reset", id="btn-fullreset", classes="danger")
            yield Button("Quit",       id="btn-quit",      classes="muted")

        # row N
        yield Static(
            "  A:chat  L:launch  R:restart  S:stop  B:browser  U:update  "
            "D:clear-db  P:reset-py  F:full-reset  T:theme  C:cycle  Esc/Q:quit",
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
            self.push_screen(SetupScreen(self.cfg), self._after_setup_choice)
        else:
            self._after_setup(True)

    def _after_setup_choice(self, result: "SetupResult | None") -> None:
        """First-run chooser result: install fresh (→ InstallScreen) or use an
        existing folder (already saved by the setup screen)."""
        if result is None:
            self._log("[launcher] setup cancelled — press [T]/relaunch to configure")
            return
        if result.action == "install":
            self.push_screen(InstallScreen(self.cfg, result.path), self._after_install)
        else:  # "existing" — cfg.project_path already saved
            self._after_setup(True)

    def _after_install(self, ok: bool | None) -> None:
        if ok and self.cfg.is_valid_project():
            self._log("[launcher] install complete")
            self._after_setup(True)
        else:
            self._log("[launcher] install did not complete — press [U] to retry, or relaunch")

    def _after_setup(self, ok: bool | None) -> None:
        if self.cfg.is_valid_project():
            self._init_controller()
            self._log(f"[launcher] project: {self.cfg.project_path}")
            self._log(
                "[launcher] auto-restart: "
                + ("ON (relaunch on crash)" if self.cfg.auto_restart_server else "OFF")
                + "  ·  health check: "
                + ("ON (relaunch if unresponsive)" if self.cfg.health_check_restart else "OFF")
            )
            # Auto-start the server so the chat can connect immediately.
            if self.cfg.auto_start_server and self.controller:
                self._log("[launcher] auto-starting server…")
                asyncio.create_task(self.controller.start())
            # Chat is the default view — open it on top of the control view.
            if self.cfg.chat_default_view:
                self.action_open_chat()
                self._log("[launcher] chat opened — Esc returns here")
            else:
                self._log("[launcher] press [A] for chat, [L] to launch, [T] theme, [Q] quit")
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
            auto_restart=self.cfg.auto_restart_server,
            health_check=self.cfg.health_check_restart,
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
        # Push the change straight to the chat (if open) so its server dot flips
        # the instant the server drops/recovers — not on the next 1 Hz poll.
        chat = self._chat_screen
        if chat is not None:
            try:
                chat.notify_server_state()
            except Exception:
                pass

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

    def action_open_chat(self) -> None:
        if not self._require_project():
            return
        # Avoid stacking duplicate chat screens.
        if isinstance(self.screen, ChatScreen):
            return
        # Pause the home stage while chat covers it; resume when chat is popped.
        self._chat_covering = True
        self._sync_stage_idle()
        chat = ChatScreen(self.cfg, self.controller)
        self._chat_screen = chat
        self.push_screen(chat, self._on_chat_closed)

    def _on_chat_closed(self, _result: object | None = None) -> None:
        self._chat_screen = None
        self._chat_covering = False
        self._sync_stage_idle()

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

    async def action_update(self) -> None:
        if not self._require_project():
            return

        def _go(confirmed: bool | None) -> None:
            if not confirmed:
                return
            asyncio.create_task(self._do_update())

        self.push_screen(
            ConfirmModal(
                "Update webAgent?",
                "Pulls the latest code (git pull, or a fresh download) and re-installs\n"
                "dependencies. Your local data is kept. Server restarts. Proceed?",
            ),
            _go,
        )

    async def _do_update(self) -> None:
        """Stop the server, refresh code + deps, then restart if it was running."""
        from . import bootstrap

        pdir = self.cfg.project_dir()
        if not pdir or not self.controller:
            return
        # Disarm the watchdog so it can't relaunch against half-updated files.
        self.controller.cancel_auto_restart()
        was_running = self.controller.state.status in ("running", "starting")
        if was_running:
            await self.controller.stop()
        ok = await bootstrap.update(pdir, self._log)
        self._log(f"[launcher] update {'succeeded' if ok else 'failed'}")
        if was_running:
            await asyncio.sleep(0.3)
            await self.controller.start()

    def action_theme(self) -> None:
        """Toggle the inline settings panel in the bottom (log) slot."""
        if self._settings is None:
            return
        if self._settings.display:
            self._close_settings()
        else:
            self._open_settings()

    async def action_home_escape(self) -> None:
        """Esc on the home screen: close the settings panel if open, else quit."""
        if self._settings is not None and self._settings.display:
            self._close_settings()
            return
        await self.action_request_quit()

    def action_go_back_last(self) -> None:
        """Ctrl+Q toggles between home and chat from either screen."""
        if isinstance(self.screen, ChatScreen):
            self.pop_screen()
        else:
            self.action_open_chat()

    def _open_settings(self) -> None:
        if self._settings is None:
            return
        self._settings.display = True
        try:
            self.query_one("#log-pane").display = False
        except Exception:
            pass
        self._settings.focus_first()

    def _close_settings(self) -> None:
        if self._settings is None:
            return
        self._settings.display = False
        try:
            self.query_one("#log-pane").display = True
        except Exception:
            pass
        # Persist the tuned values once, on close (not on every drag tick).
        try:
            self.cfg.save()
        except Exception:
            pass

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
        if self._settings is not None and self._settings.display:
            self._settings.sync_from_config()
        self._log(f"[launcher] theme → {nxt.name}")

    def action_cycle_anim(self) -> None:
        from .ascii_anim import ANIM_STYLES
        cur = self.cfg.animation_style if self.cfg.animation_style in ANIM_STYLES else "plasma"
        idx = ANIM_STYLES.index(cur)
        new = ANIM_STYLES[(idx + 1) % len(ANIM_STYLES)]
        self.cfg.animation_style = new
        self.cfg.save()
        self._apply_visual_config()
        if self._settings is not None and self._settings.display:
            self._settings.sync_from_config()
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
            "btn-chat": self.action_open_chat,
            "btn-launch": self.action_launch,
            "btn-restart": self.action_restart,
            "btn-stop": self.action_stop_server,
            "btn-browser": self.action_open_browser,
            "btn-update": self.action_update,
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

    # ── auto-idle (pause animation when unfocused / covered) ───────────
    def on_app_blur(self, event: events.AppBlur) -> None:
        self._blurred = True
        self._sync_stage_idle()

    def on_app_focus(self, event: events.AppFocus) -> None:
        self._blurred = False
        self._sync_stage_idle()

    def _sync_stage_idle(self) -> None:
        """Idle the stage when the app is unfocused OR the chat covers it.
        The stage ignores this while the style is OFF (it stays stopped)."""
        if self._stage is not None:
            self._stage.set_idle(self._blurred or self._chat_covering)

    def _apply_visual_config(self) -> None:
        palette = build_palette_from_config(self.cfg)
        if self._stage is not None:
            self._stage.set_palette(palette)
            self._stage.set_style(self.cfg.animation_style)
            self._stage.set_speed(self.cfg.theme_speed)
            self._stage.set_intensity(self.cfg.animation_intensity)
            self._stage.set_ramp(self.cfg.char_ramp)
            self._stage.set_fps(self.cfg.fps)

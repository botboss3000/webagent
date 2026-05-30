"""Main Textual application — the single-screen chat launcher.

The launcher is now ONE screen: the chat. This class is the headless "brain" —
it owns the ServerController + watchdogs, every server-control action, the
first-run install flow, the 23-theme registration, and OS-clipboard paste — but
it no longer renders a separate "home" page. At startup it pushes either the
InstallScreen (no project yet) or the ChatScreen (the single base view). The
server controls live in the chat's admin panel and call straight back into this
class's ``action_*`` methods, so the server lifecycle logic is unchanged.
"""

from __future__ import annotations

import asyncio
import os
import webbrowser
from pathlib import Path

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input, TextArea

from . import app_window, clipboard
from .chat_screen import ChatScreen
from .config import LauncherConfig
from .reset import clear_database, full_reset, reset_venv
from .screens import ConfirmModal, InstallScreen
from .server import HEALTH_URL, ServerController, ServerState
from .themes import CUSTOM_VAR_DEFAULTS, DEFAULT_THEME, THEME_ORDER, build_themes


class LauncherApp(App):
    TITLE = "webagent launcher"
    SUB_TITLE = "agent harness control surface"
    CSS_PATH = "styles.tcss"

    BINDINGS = [
        # Ctrl+C is intentionally NOT bound — it must stay free for the chat
        # editor's copy. Ctrl+V pastes from the REAL OS clipboard into the
        # focused field (priority so it preempts the framework's empty in-app
        # paste); fires on every screen.
        Binding("ctrl+v", "os_paste", "Paste", show=False, priority=True),
        # Ctrl+Q toggles the server-control (admin) panel from anywhere — the
        # closest analog to the old "go to the control surface".
        Binding("ctrl+q", "toggle_admin", "Admin", show=False, priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg = LauncherConfig.load()
        self.controller: ServerController | None = None
        self._chat_screen: ChatScreen | None = None  # the single base screen
        self._blurred = False
        # Server log is buffered here and replayed into the admin panel's log
        # widget when the chat mounts (so boot/auto-start lines aren't lost).
        self._log_lines: list[str] = []
        # Register the 23 reference themes so App.theme can switch them live.
        for theme in build_themes():
            self.register_theme(theme)

    def get_theme_variable_defaults(self) -> dict[str, str]:
        # Fallbacks for the custom theme variables ($dim/$tool/$bar-*/$sel/…) so
        # styles.tcss always parses, even before a custom theme is active.
        return dict(CUSTOM_VAR_DEFAULTS)

    # ── compose: a blank host; the real UI is the pushed screen ──────────
    def compose(self) -> ComposeResult:
        # The default screen renders nothing — ChatScreen / InstallScreen cover
        # it entirely. (An empty generator is a valid, intentionally-blank UI.)
        return
        yield  # pragma: no cover  (keeps this a generator)

    # ── lifecycle / boot → install → chat ───────────────────────────────
    def on_mount(self) -> None:
        # Apply the saved theme (live re-skin) before any screen mounts, and
        # refresh theme_variables so the chat's Rich-Text chrome reads the right
        # colors on its very first frame.
        name = self.cfg.theme_name if self.cfg.theme_name in THEME_ORDER else DEFAULT_THEME
        self.theme = name
        try:
            self.get_css_variables()
        except Exception:
            pass
        # Uptime heartbeat (1 Hz) — refreshes the admin panel's uptime line.
        self.set_interval(1.0, self._refresh_uptime)
        if not self.cfg.is_valid_project():
            # First run: full-screen installer with the animated logo. It hands
            # back via _on_install_done; chat is pushed once a project exists.
            self.push_screen(InstallScreen(self.cfg, self._on_install_done))
        else:
            self._after_setup()
            self._open_chat()

    def on_unmount(self) -> None:
        # Don't leave an orphaned app window pointing at a dead server.
        try:
            app_window.close_window()
        except Exception:
            pass

    def _open_chat(self) -> None:
        """Push the chat as the base screen (no-op if it's already showing)."""
        if isinstance(self.screen, ChatScreen):
            return
        chat = ChatScreen(self.cfg, self.controller)
        self._chat_screen = chat
        self.push_screen(chat)

    # ── install flow (first run + re-point) ─────────────────────────────
    def _on_install_done(self, ok: bool) -> None:
        """SetupPanel finished (install/adopt succeeded). Pop the installer,
        commit the target, and show the chat."""
        if isinstance(self.screen, InstallScreen):
            self.pop_screen()
        if ok and self.cfg.is_valid_project():
            asyncio.create_task(self._commit_target_switch())
        else:
            self._log("[launcher] setup incomplete — relaunch to configure")

    def _on_install_cancel(self) -> None:
        """Re-point cancelled — leave the current target (and server) untouched."""
        if isinstance(self.screen, InstallScreen):
            self.pop_screen()
        self._log("[launcher] location unchanged")

    def action_change_target(self) -> None:
        """Re-point this exe at a different folder. Opens the installer over the
        chat; Cancel leaves the current target unchanged."""
        self.push_screen(
            InstallScreen(self.cfg, self._on_install_done, self._on_install_cancel, repoint=True)
        )

    async def _commit_target_switch(self) -> None:
        old = self.controller
        new_dir = self.cfg.project_dir()
        # Re-confirming the SAME folder must not bounce a healthy server.
        if old is not None and new_dir is not None and self._same_dir(old.project_dir, new_dir):
            self._update_target_display()
            self._log(f"[launcher] location confirmed: {new_dir}")
            self._open_chat()
            return
        if old is not None:
            try:
                await old.stop()
            except Exception:
                pass
        self._after_setup()  # init controller for the new target (+ auto-start)
        self._open_chat()

    @staticmethod
    def _same_dir(a: Path, b: Path) -> bool:
        """True when two paths point at the same folder (case-insensitive on
        Windows, tolerant of trailing separators)."""
        try:
            na = os.path.normcase(os.path.normpath(str(Path(a).expanduser())))
            nb = os.path.normcase(os.path.normpath(str(Path(b).expanduser())))
            return na == nb
        except Exception:
            return False

    def _after_setup(self) -> None:
        self._update_target_display()
        if self.cfg.is_valid_project():
            self._init_controller()
            self._log(f"[launcher] project: {self.cfg.project_dir()}")
            self._log(
                "[launcher] auto-restart: "
                + ("ON (relaunch on crash)" if self.cfg.auto_restart_server else "OFF")
                + "  ·  health check: "
                + ("ON (relaunch if unresponsive)" if self.cfg.health_check_restart else "OFF")
            )
            if self.cfg.auto_start_server and self.controller:
                self._log("[launcher] auto-starting server…")
                asyncio.create_task(self.controller.start())
        else:
            self._log("[launcher] no project configured — use Location to set one")

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

    # ── status / log plumbing (routed to the chat's admin panel) ────────
    def _log(self, line: str) -> None:
        self._log_lines.append(line)
        if len(self._log_lines) > 1000:
            self._log_lines = self._log_lines[-1000:]
        chat = self._chat_screen
        if chat is not None:
            chat.write_admin_log(line)

    def _on_log_line(self, line: str) -> None:
        self._log(line)

    def _on_state_change(self, state: ServerState) -> None:
        # Push the change straight to the chat so its server dot + the admin
        # panel's status/launch label flip the instant the server drops/recovers.
        chat = self._chat_screen
        if chat is not None:
            try:
                chat.notify_server_state(state)
            except Exception:
                pass

    def _refresh_uptime(self) -> None:
        chat = self._chat_screen
        if chat is not None and self.controller is not None:
            try:
                chat.update_admin_uptime(self.controller.state)
            except Exception:
                pass

    def _update_target_display(self) -> None:
        chat = self._chat_screen
        if chat is not None:
            try:
                chat.update_admin_target(self.cfg.project_dir())
            except Exception:
                pass

    # ── server actions ──────────────────────────────────────────────────
    async def action_launch(self) -> None:
        """Two-step Launch: 1st click (stopped) → start; 2nd click (running) → browser."""
        if not self._require_project():
            return
        assert self.controller
        status = self.controller.state.status
        if status == "running":
            self.action_open_browser()
            return
        if status in ("starting", "stopping"):
            self._log(f"[launcher] server is {status}; please wait")
            return
        await self.controller.start()

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

    def action_open_app_window(self) -> None:
        """Open the webagent app in a chromeless, agent-controllable Chromium
        window (app mode). Local / Windows only."""
        if not self._require_project():
            return
        url = self.cfg.last_browser_url or HEALTH_URL
        if self.controller and self.controller.state.status != "running":
            self._log("[launcher] note: server isn't running yet — press Launch so the app loads")
        if not app_window.open_window(url, self._log):
            self._log("[launcher] app window unavailable — run Update to fetch Chromium")

    async def action_clear_db(self) -> None:
        if not self._require_project():
            return

        def _go(confirmed: bool | None) -> None:
            if confirmed:
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
            if confirmed:
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
            if confirmed:
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
            if confirmed:
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
        self.controller.cancel_auto_restart()
        was_running = self.controller.state.status in ("running", "starting")
        if was_running:
            await self.controller.stop()
        ok = await bootstrap.update(pdir, self._log)
        self._log(f"[launcher] update {'succeeded' if ok else 'failed'}")
        if was_running:
            await asyncio.sleep(0.3)
            await self.controller.start()

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

    # ── admin panel toggle + look ───────────────────────────────────────
    def action_toggle_admin(self) -> None:
        chat = self._chat_screen
        if chat is not None and isinstance(self.screen, ChatScreen):
            chat.action_toggle_admin()

    def action_theme(self) -> None:
        """Open the theme picker (Ctrl+T equivalent from the chat)."""
        chat = self._chat_screen
        if chat is not None:
            chat.action_theme_menu()

    def action_cycle_theme(self) -> None:
        """Cycle to the next of the 23 themes (used by the installer's Theme tab)."""
        cur = self.theme if self.theme in THEME_ORDER else DEFAULT_THEME
        idx = THEME_ORDER.index(cur)
        nxt = THEME_ORDER[(idx + 1) % len(THEME_ORDER)]
        self.theme = nxt
        self.cfg.theme_name = nxt
        try:
            self.cfg.save()
        except Exception:
            pass
        self._log(f"[launcher] theme → {nxt}")

    def action_cycle_anim(self) -> None:
        from .ascii_anim import ANIM_STYLES
        cur = self.cfg.animation_style if self.cfg.animation_style in ANIM_STYLES else "plasma"
        idx = ANIM_STYLES.index(cur)
        new = ANIM_STYLES[(idx + 1) % len(ANIM_STYLES)]
        self.cfg.animation_style = new
        try:
            self.cfg.save()
        except Exception:
            pass
        if self._chat_screen is not None:
            self._chat_screen.set_anim_style(new)
        self._log(f"[launcher] animation → {new}")

    # ── OS clipboard paste / copy ───────────────────────────────────────
    async def action_os_paste(self) -> None:
        """Ctrl+V → paste from the real OS clipboard into the focused field.

        Textual's own Ctrl+V only pastes its in-app clipboard, so without this
        you couldn't paste a path copied from Explorer into the install box. We
        read the actual OS clipboard and hand the text to the focused
        Input/TextArea through its normal Paste handler."""
        widget = self.focused
        if not isinstance(widget, (Input, TextArea)):
            return
        try:
            text = await asyncio.to_thread(clipboard.read_text)
        except Exception:
            text = ""
        if text:
            widget._forward_event(events.Paste(text))

    def copy_to_clipboard(self, text: str) -> None:
        """Mirror in-app copies (Ctrl+C in a field) to the real OS clipboard."""
        super().copy_to_clipboard(text)
        try:
            clipboard.write_text(text)
        except Exception:
            pass

    # ── auto-idle (pause the chat animation banner when unfocused) ──────
    def on_app_blur(self, event: events.AppBlur) -> None:
        self._blurred = True
        if self._chat_screen is not None:
            self._chat_screen.set_app_idle(True)

    def on_app_focus(self, event: events.AppFocus) -> None:
        self._blurred = False
        if self._chat_screen is not None:
            self._chat_screen.set_app_idle(False)

    # ── helpers ─────────────────────────────────────────────────────────
    def _require_project(self) -> bool:
        if self.controller and self.cfg.is_valid_project():
            return True
        self._log("[launcher] ERROR: no valid project configured. Use Location to set one.")
        return False

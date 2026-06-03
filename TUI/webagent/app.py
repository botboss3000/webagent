"""Textual TUI for the webAgent Server Manager.

A single chat screen: a transcript pane that streams the agent's text, tool
calls, and tool results, plus an input. Mutating tools are gated behind an
"Allow writes" toggle (Ctrl+W) unless Autonomous mode (Ctrl+A) is on.

The look-and-feel (23 themes + emoji/ASCII glyphs) is shared with the webAgent
launcher: theme/glyph assets are vendored alongside this package so the .exe
stays self-contained while feeling like the same product. Ctrl+T cycles themes.
Transcript text is Rich-drawn, so its colors are resolved to concrete hex from
the active theme via ``theme_colors`` (refreshed whenever the theme changes).
"""

from __future__ import annotations

import json
import re
import uuid
import webbrowser
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click, Paste
from textual.message import Message
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.strip import Strip
from textual.widgets import Checkbox, Collapsible, Input, Select, Static, TextArea

from .agent import AgentEvent, ServerManagerAgent
from .subagents import SubagentRegistry
from .ascii_anim import ANIM_LABELS, ANIM_STYLES
from .config import (
    PROVIDER_PRESETS,
    ProviderConfig,
    TuiConfig,
    _looks_like_project,
    db_path,
    provider_name_for_base,
    resolve_provider,
)
from .appclient import WebAppClient, WebAppError
from . import attach
from .clip import read_clipboard, read_clipboard_image, write_clipboard
from .db import Store
from .env_probe import probe_machine, server_health
from .glyphs import EMOJI, G
from .llm import LLMClient
from .model_windows import MODEL_CONTEXT_BY_ID, MODEL_CONTEXT_WINDOWS
from .selfinfo import check_self_update, gather
from .notify import Notifier
from .procscan import scan_webagent_processes
from .watchdog import Watchdog, set_active_watchdog
from .palette import PRESETS, palette_from_theme
from .theme_colors import chrome_colors
from .themes import CUSTOM_VAR_DEFAULTS, DEFAULT_THEME, THEME_LABELS, THEME_ORDER, build_themes


# Matches bare http(s) URLs so they can be turned into clickable terminal hyperlinks
# (OSC 8). Trailing punctuation and markup/closing brackets are excluded.
_URL_RE = re.compile(r'https?://[^\s\]\)>"\'`]+[^\s\]\)>"\'`.,;:!?]')


def _linkify_text(text: Text) -> Text:
    """Stylize bare URLs inside a Rich Text as clickable OSC-8 hyperlinks, in place."""
    plain = text.plain
    for m in _URL_RE.finditer(plain):
        text.stylize(f"link {m.group(0)}", m.start(), m.end())
    return text


class PromptInput(TextArea):
    """Multi-line input pill. Starts at 3 rows tall; expands up to 5 rows as text
    is added; submits on Enter. Ctrl+Enter and Shift+Enter start a new line.
    Border stays coloured always (never dims). Inherits clipboard-aware Ctrl+V
    paste (multi-line now allowed) and Ctrl+A select-all from TextArea."""

    DEFAULT_CSS = """
    PromptInput { height: 3; }
    """

    def on_mount(self) -> None:
        """Lock in the initial 3-row height so the widget doesn't start at 1 line."""
        self.styles.height = 3

    class Submitted(Message):
        """Fired on Enter with the current text."""
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

        @property
        def control(self) -> "PromptInput":
            return self._sender  # type: ignore[return-value]

    MAX_ROWS = 5

    BINDINGS = [
        Binding("ctrl+enter", "new_line", "New line", show=False),
        Binding("shift+enter", "new_line", "New line", show=False),
        Binding("ctrl+a", "select_all", "Select all", show=False),
    ]

    def key_enter(self) -> None:
        """Intercept Enter before TextArea's own handler inserts a newline."""
        self.action_submit()

    def action_new_line(self) -> None:
        """Insert a newline at the cursor without submitting."""
        self.insert("\n")

    def action_submit(self) -> None:
        text = self.text.strip()
        # Submit on text, or on attachments alone (an image with no caption).
        has_att = bool(getattr(self.app, "_attachments", None))
        if text or has_att:
            self.text = ""
            self.post_message(self.Submitted(text))

    def action_paste(self) -> None:
        # Image attachment only applies to the main chat input — config fields
        # (API key, base URL, …) keep plain text paste.
        if self.id == "prompt":
            # Ctrl+V: an image on the clipboard becomes an attachment; otherwise
            # the text is pasted — unless that text is itself a path to an image
            # file (e.g. a file copied in the OS file manager), which also attaches.
            img = read_clipboard_image()
            if img is not None:
                data, mime = img
                if self.app._attach_image_bytes(data, mime):  # type: ignore[attr-defined]
                    return
        text = read_clipboard() or self.app.clipboard
        if not text:
            return
        if self.id == "prompt" and self.app._maybe_attach_paths(text):  # type: ignore[attr-defined]
            return
        self.insert(text)

    async def _on_paste(self, event: Paste) -> None:
        # Bracketed paste / terminal drag-drop arrives here as text — usually the
        # dropped file's path. On the main input, if every token is an image file,
        # attach instead of inserting the raw path; otherwise normal text paste.
        if (self.id == "prompt" and event.text
                and self.app._maybe_attach_paths(event.text)):  # type: ignore[attr-defined]
            event.stop()
            event.prevent_default()
            return
        await super()._on_paste(event)

    def render_line(self, y: int) -> Strip:
        """Render a line, guarding against zero-width content (crashes rich's
        chop_cells / divide_line with ValueError: range() arg 3 must not be zero)."""
        if self.content_size.width <= 0:
            return Strip.blank(self.size.width, self.get_visual_style("text-area"))
        return super().render_line(y)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Auto-grow up to MAX_ROWS lines."""
        nlines = self.text.count("\n") + 1
        target = max(3, min(nlines + 1, self.MAX_ROWS))
        self.styles.height = target


class SpinnerBar(Widget):
    """A one-row activity spinner (``-`` ``/`` ``|`` ``\\``) shown whenever the agent
    is busy, so the user can see it isn't frozen. Blank at rest, and the timer is
    paused when idle so it costs ~0% CPU. Colour comes from the live theme.

    This widget is kept invisible — it only provides frame+timing for the HUD,
    which embeds the spinning character between the token in/out counts.
    """

    FRAMES = "-\\|/"

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)
        self._active = False
        self._frame = 0
        self._timer = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.1, self._tick, pause=True)  # ~10 fps, paused at rest

    def set_active(self, on: bool) -> None:
        if on == self._active:
            return
        self._active = on
        self._frame = 0
        if self._timer is not None:
            self._timer.resume() if on else self._timer.pause()
        self.refresh()

    def _tick(self) -> None:
        self._frame += 1
        # Push the new frame into the merged HUD bar so the slash animates live.
        try:
            self.app._update_hud()
        except Exception:
            pass

    def render(self) -> Text:
        return Text("")


class ServerStatusWidget(Widget):
    """The header's live server-status pill. Shows ``live`` / ``stopped`` at rest,
    and a spinning ``-\\|/`` with ``starting`` while the server is loading (an
    in-flight start/restart or an indeterminate health probe). Click → server panel.
    The timer is paused unless loading, so it costs ~0% CPU at rest. Colour comes
    from the live theme (read from ``app.cc`` at render time)."""

    FRAMES = "-\\|/"

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id, classes="hdr-btn")
        self._btn_action = "panel_server"   # picked up by the .hdr-btn click handler
        self._state = "n/a"
        self._frame = 0
        self._timer = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.1, self._tick, pause=True)
        self._sync()

    def set_state(self, state: str) -> None:
        self._state = state
        self._sync()

    def _loading(self) -> bool:
        return self._state in ("checking", "starting", "unknown")

    def _sync(self) -> None:
        if self._timer is not None:
            self._timer.resume() if self._loading() else self._timer.pause()
        self.refresh()

    def _tick(self) -> None:
        self._frame += 1
        self.refresh()

    def render(self) -> Text:
        cc = getattr(self.app, "cc", {})
        if self._state == "running":
            return Text(f"{G.DOT_LIVE} live", style=f"bold {cc.get('success', '#7be06a')}")
        if self._state == "stopped":
            return Text(f"{G.DOT_DEAD} stopped", style=f"bold {cc.get('error', '#ff5f56')}")
        spin = self.FRAMES[self._frame % len(self.FRAMES)]
        return Text(f"{spin} starting", style=f"bold {cc.get('tool', '#ff9d2f')}")


class PanelGrip(Static):
    """A 1-cell-wide vertical handle on the left edge of the side panel. Drag it
    (mouse or touch) to resize the panel; the chat column to its left flexes to fill
    the rest. Shown only while a panel is open."""

    def __init__(self, id: str | None = None) -> None:
        super().__init__("", id=id)
        self._dragging = False

    def on_mouse_down(self, event) -> None:
        self._dragging = True
        self.capture_mouse()
        event.stop()

    def on_mouse_up(self, event) -> None:
        self._dragging = False
        try:
            self.release_mouse()
        except Exception:
            pass
        event.stop()

    def on_mouse_move(self, event) -> None:
        if not self._dragging:
            return
        try:
            total = self.app.size.width
            panel = self.app.query_one("#side-panel", Vertical)
            new_w = total - int(event.screen_x)
            lo, hi = 24, max(28, int(total * 0.85))
            panel.styles.width = max(lo, min(hi, new_w))
        except Exception:
            pass
        event.stop()


def _scene_rows(app) -> list[tuple[str, "Select"]]:
    """The theme/animation ('Scene') controls — one labelled Select per setting.
    Shared shape so every change routes through app.apply_setting (live + persisted)."""
    cfg = app.cfg

    def safe(v, allowed, default):
        return v if v in allowed else default

    # The animated banner was removed, so only the theme picker remains here.
    return [
        ("Theme", Select([(THEME_LABELS.get(t, t), t) for t in THEME_ORDER],
                         value=safe(app.theme, set(THEME_ORDER), DEFAULT_THEME),
                         allow_blank=False, id="set-theme")),
    ]


class ConfirmModal(ModalScreen[bool]):
    """A details + confirm/cancel screen for important admin actions (Update,
    Uninstall). Dismisses True on confirm, False on cancel/Esc."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, body: str, confirm_label: str = "Confirm") -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-panel"):
            yield Static(self._title, id="confirm-title")
            yield Static(self._body, id="confirm-body", markup=False)
            with Horizontal(id="confirm-buttons"):
                yield Static(self._confirm_label, classes="confirm-btn confirm-yes", markup=False)
                yield Static("Cancel", classes="confirm-btn confirm-no", markup=False)

    @on(Click, ".confirm-yes")
    def _confirm(self, event: Click) -> None:
        self.dismiss(True)

    @on(Click, ".confirm-no")
    def _cancel_click(self, event: Click) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class ServerManagerApp(App):
    CSS_PATH = "styles.tcss"
    TITLE = "webAgent Server Manager"

    # Esc opens/closes the side menu (a panel); Ctrl+Q quits the app. The editing
    # keys (Ctrl+A/C/V) are handled by the focused input. Theme stays on Ctrl+T (not
    # advertised). priority=True so these fire even while the input is focused.
    BINDINGS = [
        Binding("escape", "exit", "Menu", priority=True),
        Binding("ctrl+q", "quit_app", "Quit", priority=True),
        Binding("ctrl+t", "cycle_theme", "Theme", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg = TuiConfig.load()
        self.project_root: Path | None = self.cfg.project_dir()
        self.facts = probe_machine()                   # static host facts (cached for the session)
        self.store = Store(db_path())
        self.agent: ServerManagerAgent | None = None
        self.provider: ProviderConfig
        self.llm: LLMClient
        self._apply_provider()                         # sets self.provider + self.llm
        self.cc: dict[str, str] = chrome_colors(self)  # concrete theme colors for Rich text
        # The agent always exists now — onboarding mode runs even with no checkout linked.
        self.agent = ServerManagerAgent(
            self.cfg, self.project_root, self.llm, self.store,
            set_project=self._link_project, provider=self.provider,
            request_exit=self._request_exit,
        )
        # -- Subagent orchestration (mk2 event-driven model) --
        # One shared registry tracks every in-flight + finished subagent; the
        # agent loop reads it (results buffer, pending list) and calls back into
        # the app to launch workers + drain steering messages.
        self._subagents = SubagentRegistry()
        self._steer_queue: list[str] = []
        self.agent.subagents = self._subagents
        self.agent.spawn_subagent = self._spawn_subagent
        self.agent.drain_steer = self._drain_steer
        # ── Live link to the RUNNING web app (drive + observe it) ──
        # A single persistent client (admin session + WebSocket stream). The two
        # mutes govern who talks: WebAgent muted (default) → Manager talks to me,
        # no app link; unmuted → Manager bridged to the target session; Manager
        # muted → my input goes straight to the app agent.
        self._webapp = WebAppClient()
        self.agent.webapp_client = self._webapp
        self._webapp_target: Optional[dict] = None   # {agent_id, agent_name, session_id, session_title}
        self._webagent_muted = True                  # default: no app connection
        self._manager_muted = False
        self._webapp_stream_on = False               # the WS stream worker is running
        self._ws_bubble = None                       # current streaming app-agent Static
        self._ws_bubble_text = ""                    # accumulated stream text for it
        # Sidebar view state: confirmations + the connect/config views render IN the
        # panel (no pop-up modals). _confirm_state carries the active confirm dialog.
        self._confirm_state: Optional[dict] = None   # {title, body, label, on_yes}
        self._reset_state: dict = {"db": True, "pages": True, "secrets": False,
                                   "users": False, "env": False, "agents": False}
        self._connect_agents: list[dict] = []        # agents fetched for the Connect view
        self._connect_sessions: list[dict] = []      # sessions for the picked agent
        self._connect_agent: Optional[dict] = None   # the agent currently expanded
        self._cfg_settings: dict = {}                # last-fetched app settings (config view)
        self._cfg_provider: dict = {}                # last-fetched LLM provider (config view)
        self._cfg_provider_pick: str = ""            # highlighted provider preset (App Config)
        self._diag_text: str = ""                    # last diagnostics readout (sidebar view)
        self._logs_text: str = ""                    # last server-logs readout (sidebar view)
        self.session_id = self.store.create_session(
            str(self.project_root) if self.project_root else "(onboarding)"
        )
        self._self_info = gather()            # how THIS manager runs (source/exe); static, cached
        self._self_update_state = "manager update: checking…"  # refreshed by a startup probe
        self._server_state = "n/a"   # cached server health for the status-bar dot
        self._do_autostart = True    # auto-start the managed server on open (tests disable)
        self._watchdog = None        # the autonomous monitor loop (started in on_mount)
        self._dot = None             # the server-status widget, updated in place by the poll
        self._anim = None            # the animated logo banner (collapses once chat starts)
        self._anim_on = self.cfg.anim_enabled
        self._spinner = None         # activity spinner (above the input), spins while busy
        self._stop_btn = None        # action-bar [Stop] pill
        self._cont_btn = None        # action-bar [Continue] pill
        self._busy = False           # an agent turn is currently running
        self._attachments: list[dict] = []   # pending image attachments for the next message
        self._s_in = 0               # session token accumulators (HUD)
        self._s_out = 0
        self._ctx_tokens = 0         # latest prompt size = current context usage
        self._panel_kind = None      # which side-panel category is open (None = closed)
        self._tool_group = None      # current open "N tool calls" Collapsible (None = none)
        self._tool_n = 0             # how many calls are in the current group
        self._tool_pending: list[dict] = []  # calls awaiting their result (fill in place)

    def _apply_provider(self) -> None:
        """(Re)resolve the AI provider for the current project and rebuild the LLM
        client. Managed mode → the linked repo's provider.json wins; onboarding →
        the app key. Called at startup and on every relink (live key re-pick)."""
        self.provider = resolve_provider(self.project_root, self.cfg)
        self.llm = LLMClient(self.provider)
        if getattr(self, "agent", None) is not None:
            self.agent.llm = self.llm
            self.agent.provider = self.provider

    # Declared so the stylesheet's custom tokens ($dim, $tool, $bar-bg, …) parse
    # even before one of our themes is active (Textual requirement).
    def get_theme_variable_defaults(self) -> dict[str, str]:
        return CUSTOM_VAR_DEFAULTS

    def copy_to_clipboard(self, text: str) -> None:
        """Route every copy/cut (input fields via Ctrl+C / Ctrl+X, and on-screen text
        selection) to the REAL OS clipboard. Textual's default only emits an OSC 52
        escape, which doesn't reach the clipboard on Windows conhost / many terminals —
        so we write it directly, then still call super() for terminals that honour OSC 52."""
        try:
            write_clipboard(text)
        except Exception:
            pass
        super().copy_to_clipboard(text)

    def compose(self) -> ComposeResult:
        # Custom chrome modelled on the launcher's chat screen: a header toolbar of
        # clickable CATEGORY buttons spanning the FULL width
        # above the middle body. The body splits into the chat column (left,
        # always visible) and a thin side panel (right) that appears only when a
        # category is opened — so opening a menu never hides the conversation.
        yield Horizontal(id="status")      # header: clickable category toolbar
        with Horizontal(id="body"):
            with Vertical(id="main"):      # the chat column (stays visible)
                self._anim = None                  # (the animated logo banner was removed)
                yield Static(self._title_text(), id="title", markup=False)  # plain text header
                yield VerticalScroll(id="log")     # transcript (mounted widgets)
                # Merged bar: token HUD (left) + [Stop] / [Continue] (right).
                # The spinner is invisible; its animated frame shows in the HUD's slash.
                self._spinner = SpinnerBar(id="spinner")
                self._stop_btn = Static("[Stop]", classes="act-btn disabled", markup=False)
                self._stop_btn._btn_action = "stop"          # type: ignore[attr-defined]
                self._cont_btn = Static("[Continue]", classes="act-btn", markup=False)
                self._cont_btn._btn_action = "continue"      # type: ignore[attr-defined]
                with Horizontal(id="hud"):
                    yield Static("", id="hud-text")       # token + context text (left)
                    yield self._spinner                   # invisible, drives the slash
                    yield self._stop_btn
                    yield self._cont_btn
                cta = Static(self._cta_label(), id="cta", markup=False)
                cta.display = self.project_root is None   # onboarding-only call-to-action
                yield cta
                # Pending image attachments: one removable chip each. Hidden when empty.
                attach_row = Horizontal(id="attach-row")
                attach_row.display = False
                yield attach_row
                with Horizontal(id="input-row"):
                    yield PromptInput(
                        placeholder="Ask the Server Manager…",
                        soft_wrap=True, compact=True, show_line_numbers=False,
                        id="prompt",
                    )
                    yield Static(">>>", id="send-btn", markup=False)
            # The drag-to-resize handle sits between the chat column and the panel.
            grip = PanelGrip(id="panel-grip")
            grip.display = False
            yield grip
            # The docked side panel (Admin / Git / WEBAGENT / Server / Config / …).
            # Hidden by default; shown when a category button is clicked.
            panel = Vertical(id="side-panel")
            panel.display = False
            yield panel
        

    async def on_mount(self) -> None:
        # Register the 23 shared themes and activate the saved one.
        for theme in build_themes():
            self.register_theme(theme)
        self.theme = self.cfg.theme_name if self.cfg.theme_name in THEME_ORDER else DEFAULT_THEME
        self.cc = chrome_colors(self)
        if self._anim is not None:
            self._retint_anim()
            self._anim.set_idle(not self._anim_on)
        self._server_state = "checking" if self.project_root else "n/a"
        self._refresh_status()   # paint the header (server pill spins while we probe)
        self._server_state = await server_health() if self.project_root else "n/a"
        self._render_welcome(self._server_state)
        self._refresh_title()
        self._refresh_status()
        self._update_hud()
        self.query_one("#prompt", PromptInput).focus()
        # Keep the server dot live in managed mode (cheap localhost /health poll).
        self.set_interval(3.0, self._poll_server)
        # Scan for running webAgent server PIDs (and stale/zombie ones) on open.
        self.run_worker(self._scan_pids_on_open(), group="pidscan", exclusive=True)
        # Auto-start the managed server on open, so a manual Launch is unnecessary.
        if self._do_autostart and self.project_root is not None:
            self.run_worker(self._autostart_server(), group="server", exclusive=True)
        # Check (in the background) whether a newer manager is available upstream.
        self.run_worker(self._check_self_update(), group="selfupd", exclusive=True)
        # Start the autonomous watchdog. It idles in onboarding mode / when disabled
        # (re-read each tick) and picks up a checkout linked mid-session, so we can
        # start it unconditionally. It notifies via desktop toast + the transcript
        # and can auto-restart the server within the configured autonomy level.
        self._watchdog = Watchdog(
            get_project_root=lambda: self.project_root,
            restart_server=self._watchdog_restart,
            notifier=Notifier(log=self._log_watchdog),
            log=self._log_watchdog,
            inject_event=self._watchdog_inject,
        )
        set_active_watchdog(self._watchdog)
        self.run_worker(self._watchdog.run(), group="watchdog", exclusive=True)

    # ── welcome / situation ───────────────────────────────────────────────
    def _recommended_install_path(self) -> str:
        return "C:\\webagent" if self.facts.os_label == "Windows" else "~/webagent"

    def _host_line(self) -> str:
        f = self.facts
        py = f.system_python or f.runtime_python
        return (f"[{self.cc['dim']}]host:[/] {f.os_label} ({f.arch}) {G.SEP} "
                f"Python {py} {G.SEP} git {'found' if f.git_present else 'missing'}")

    def _tip_line(self) -> str:
        return (f"[{self.cc['dim']}]tip: drag to select {G.SEP} Ctrl+C copies {G.SEP} "
                f"Shift+drag for your terminal's native select/copy[/]")

    def _render_welcome(self, server_status: str) -> None:
        c = self.cc
        if self.project_root:
            self._log(f"[b {c['primary']}]{G.ADMIN} webAgent Server Manager[/] "
                      f"[{c['dim']}]— managing your checkout[/]")
            self._log(f"[{c['dim']}]project:[/] {self.project_root}")
            self._log(self._host_line())
            srv = (f"[{c['secondary']}]running[/] at http://localhost:8080"
                   if server_status == "running" else f"{server_status}")
            self._log(f"[{c['dim']}]server:[/] {srv}")
            if self.provider.configured:
                self._log(f"[{c['dim']}]model:[/] {self.provider.model}")
            else:
                self._log(f"[{c['tool']}]{G.WARN} No AI key.[/] Set one to enable the agent.")
            self._log(f"[{c['dim']}]Ask me to check status, diagnose an issue, change code, "
                      f"run it, or manage git.[/]")
        else:
            self._log(f"[b {c['primary']}]{G.ADMIN} webAgent Server Manager[/] "
                      f"[{c['dim']}]— let's get you set up[/]")
            self._log(self._host_line())
            if self.provider.configured:
                self._log(f"[{c['dim']}]model:[/] {self.provider.model}")
            else:
                self._log(f"[{c['tool']}]{G.WARN} No AI key configured yet.[/] "
                          "Set the app key (LLM_API_KEY) to power onboarding.")
            self._log(f"[{c['dim']}]No webAgent repo is linked yet. I can:[/]")
            self._log(f"  {G.BULLET} install webAgent for you (recommended: {self._recommended_install_path()})")
            self._log(f"  {G.BULLET} link an existing copy — tell me its folder and I'll manage it")
            self._log(f"  {G.BULLET} tell you about webAgent, or help with general questions")
            self._log(f"[{c['accent']}]{G.BULLET} New here? Tap [b]Click here to get started[/] "
                      f"below and I'll install and set everything up.[/]")
        self._log(self._tip_line())

    async def _build_situation(self) -> str:
        """The per-turn snapshot handed to the agent so it never guesses the state."""
        f = self.facts
        has = self.project_root is not None
        status = await server_health()
        py = f.system_python or f.runtime_python
        pyflag = "" if f.system_python_supported in (True, None) else " (UNSUPPORTED; needs 3.11-3.12)"
        browser = "supported" if f.browser_capable else "NOT available on this platform"
        key = (f"configured (model {self.provider.model})" if self.provider.configured
               else "NOT configured")
        actions = self.agent.registry.names(has_project=has) if self.agent else []
        lines = [
            f"- Host: {f.os_label} ({f.arch}); Python {py}{pyflag}; "
            f"git {'present' if f.git_present else 'MISSING'}; headless browser {browser}.",
            f"- Mode: {'MANAGED - a webAgent checkout is linked.' if has else 'ONBOARDING - no webAgent repo linked yet.'}",
            f"- Project: {self.project_root if has else '(none)'}.",
            f"- Server: {status}" + (" at http://localhost:8080." if status == "running" else "."),
            f"- AI key: {key}.",
            f"- Self (this manager): running from {self._self_info.mode} "
            f"(v{self._self_info.version}, build {self._self_info.build_commit or 'unstamped'}); "
            f"{self._self_update_state}. Update yourself with self_update + self_restart.",
            "- Available actions now: " + (", ".join(actions) if actions
                else "conversation/guidance only - link a checkout to enable tools."),
        ]
        pending = self._subagents.describe_pending() if getattr(self, "_subagents", None) else ""
        if pending:
            lines.append("- " + pending.replace(chr(10), chr(10) + "  "))
        return "\n".join(lines)

    async def _link_project(self, path: str) -> str:
        """Link to an existing webAgent checkout (the agent's set_project hook).
        Re-picks the AI key live so the repo's credentials take over."""
        p = Path(path).expanduser()
        try:
            exists, is_dir = p.exists(), p.is_dir()
        except OSError as e:
            return f"Can't access {p}: {e}"
        if not exists:
            return f"That path doesn't exist: {p}"
        if not is_dir:
            return f"That isn't a folder: {p}"
        if not _looks_like_project(p):
            return (f"{p} doesn't look like a webAgent checkout (it needs run.py and an app/ "
                    "folder). Linking arbitrary folders for general coding is coming soon.")
        old_llm = self.llm
        self.project_root = p.resolve()
        self.cfg.project_path = str(self.project_root)
        self.cfg.save()
        self._apply_provider()                         # the repo's provider.json wins now
        if self.agent is not None:
            self.agent.project_root = self.project_root
        if old_llm is not None and old_llm is not self.llm:
            await old_llm.aclose()
        self._refresh_status()
        self._hide_cta()
        if self.provider.configured:
            keynote = f"using this repo's AI key (model {self.provider.model})"
        else:
            keynote = "this repo has no AI key set - still using the app key; say 'set my key' to change it"
        self._log(f"[{self.cc['secondary']}]{G.OK} linked to {self.project_root}[/] "
                  f"[{self.cc['dim']}]- {keynote}[/]")
        return f"Linked to {self.project_root}. Managed mode is active, {keynote}."

    # ── transcript: mounted widgets in a scrolling column ─────────────────
    def _mount(self, widget: Widget) -> None:
        """Mount a widget at the end of the transcript and keep it scrolled to the
        bottom. Fire-and-forget mount (Textual processes it on the next cycle)."""
        try:
            log = self.query_one("#log", VerticalScroll)
            log.mount(widget)
            log.scroll_end(animate=False)
        except Exception:
            pass

    def _scroll_end(self) -> None:
        try:
            self.query_one("#log", VerticalScroll).scroll_end(animate=False)
        except Exception:
            pass

    def _log(self, markup: str) -> None:
        """A single transcript line carrying Rich console markup (theme colours).
        Rendered as a markup STRING (Static handles it leniently) — pre-parsing to a
        Text is unsafe here because ASCII glyph fallbacks like '[admin]' look like
        markup tags. URL linkifying is done on the plain-text paths instead."""
        self._mount(Static(markup, classes="msg-line"))

    def _log_block(self, text: str) -> None:
        """Mount raw multi-line text (server logs / diagnostics) WITHOUT markup
        parsing — they contain brackets and tracebacks that aren't Rich markup.
        URLs are still made clickable."""
        self._mount(Static(_linkify_text(Text(text, style=self.cc["dim"])),
                           classes="msg-block", markup=False))

    def _log_assistant(self, text: str) -> None:
        """Render the agent's reply as Markdown (so code fences, lists, and emphasis
        format nicely) — markup is off, so the model's text can't break parsing."""
        self._end_tool_group()
        try:
            from rich.markdown import Markdown
            renderable = Markdown(text) if text.strip() else Text("(no reply)", style=self.cc["dim"])
        except Exception:
            renderable = Text(text, style=self.cc["fg"])
        self._mount(Static(renderable, classes="msg-agent", markup=False))

    # ── custom chrome: status bar (header) + hint bar (footer) ────────────
    def _add_hdr(self, bar: Horizontal, content, action: str | None) -> Static:
        btn = Static(content, classes="hdr-btn" if action else "hdr-note", markup=False)
        if action:
            btn._btn_action = action  # type: ignore[attr-defined]
        bar.mount(btn)
        return btn

    def _refresh_status(self) -> None:
        """(Re)build the header toolbar (replaces the stock Header): one clickable
        CATEGORY button per group — Admin · Git (managed mode) · App — each of which
        opens a thin right-side panel of buttons. The theme is cycled with Ctrl+T (no
        header button). The last item is the live SERVER STATUS itself
        (live / stopped / checking); clicking it opens the server panel (Start /
        Restart / Kill). No title or model text."""
        c = self.cc
        try:
            bar = self.query_one("#status", Horizontal)
        except Exception:
            return
        bar.remove_children()

        def cat(label: str, kind: str, action: str) -> None:
            # The pill of the open category gets the .hdr-on highlight class.
            w = self._add_hdr(bar, Text(label, style=f"bold {c['accent']}"), action)
            if self._panel_kind == kind:
                w.add_class("hdr-on")

        # Far-left = ✕ close button (shown only while a panel is open) + write-gate toggle.
        close_btn = self._add_hdr(bar, Text(f"{G.CROSS} ", style=f"bold {c['accent']}"), "close_panel")
        close_btn.display = self._panel_open()
        mode = "auto" if self.cfg.autonomous else "write" if self.cfg.writes_enabled else "read"
        mcol = {"read": c["dim"], "write": c["secondary"], "auto": c["tool"]}[mode]
        self._add_hdr(bar, Text(mode, style=f"bold {mcol}"), "mode_cycle")

        cat("Admin", "admin", "panel_admin")
        if self.project_root:
            cat("Git", "git", "panel_git")   # source control: fetch / pull / push
        cat("WEBAGENT", "connect", "panel_connect")   # connect to a live web agent/session
        # The live server STATUS sits right after WEBAGENT (managed mode); click → server panel.
        # Rebuilt each refresh, so keep self._dot pointing at the current widget.
        self._dot = None
        if self.project_root:
            self._dot = ServerStatusWidget()
            if self._panel_kind == "server":
                self._dot.add_class("hdr-on")
            bar.mount(self._dot)
            self._dot.set_state(self._server_state)
        else:
            self._add_hdr(bar, Text("onboarding", style=c["secondary"]), None)

    def _title_text(self) -> Text:
        """The plain-text app header (replaces the animated logo banner):
        'WEBAGENT' on top, 'Server Manager' beneath, coloured from the theme."""
        c = self.cc
        t = Text(justify="center", no_wrap=True, overflow="crop")
        t.append("WEBAGENT\n", style=f"bold {c['accent']}")
        t.append("Server Manager", style=c["dim"])
        return t

    def _refresh_title(self) -> None:
        try:
            self.query_one("#title", Static).update(self._title_text())
        except Exception:
            pass

    # ── session HUD (tokens + context gauge) ──────────────────────────────
    @staticmethod
    def _fmt_tokens(n) -> str:
        try:
            n = int(n)
        except (TypeError, ValueError):
            return "0"
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

    def _context_max(self, model: str):
        m = (model or "").lower()
        if m in MODEL_CONTEXT_BY_ID:
            return MODEL_CONTEXT_BY_ID[m]
        for sub, mx in MODEL_CONTEXT_WINDOWS:   # longest-substring entries first
            if sub in m:
                return mx
        return None

    def _ctx_gauge(self) -> Text:
        """Compact context usage: a single 'ctx N%' (or a raw token count if the
        model's window is unknown) — no bar, to keep the HUD tight."""
        c = self.cc
        g = Text(no_wrap=True)
        used = self._ctx_tokens
        if used <= 0:
            g.append("ctx --", style=c["dim"])
            return g
        mx = self._context_max(self.provider.model) if self.provider.configured else None
        if not mx:
            g.append(f"ctx {self._fmt_tokens(used)}", style=c["dim"])
            return g
        frac = max(0.0, min(1.0, used / mx))
        col = c["error"] if frac >= 0.85 else c["tool"] if frac >= 0.6 else c["success"]
        g.append("ctx ", style=c["dim"])
        g.append(f"{int(frac * 100)}%", style=col)
        return g

    def _update_hud(self) -> None:
        c = self.cc
        t = Text(no_wrap=True, overflow="crop")
        t.append(f"{self._fmt_tokens(self._s_in)} in ", style=f"bold {c['success']}")

        # The slash between in/out animates as a spinner when busy.
        if self._spinner and self._spinner._active:
            frame = self._spinner.FRAMES[self._spinner._frame % len(self._spinner.FRAMES)]
            t.append(frame, style=f"bold {c.get('accent', '#46d4ff')}")
        else:
            t.append("/", style=c["dim"])

        t.append(f" {self._fmt_tokens(self._s_out)} out", style=f"bold {c['success']}")
        t.append(" | ", style=c["dim"])
        t.append_text(self._ctx_gauge())
        try:
            self.query_one("#hud-text", Static).update(t)
        except Exception:
            pass

    async def _poll_server(self) -> None:
        """Poll server health. When running/stopped flips, rebuild the toolbar so
        the Start↔Stop panel switches; otherwise just update the status pill in place
        (which keeps its own spinner running while loading)."""
        new = await server_health() if self.project_root else "n/a"
        changed = new != self._server_state
        self._server_state = new
        if changed:
            self._refresh_status()
        elif self._dot is not None:
            try:
                self._dot.set_state(new)
            except Exception:
                pass

    @on(Click, ".hdr-btn")
    def _on_hdr_click(self, event: Click) -> None:
        action = getattr(event.widget, "_btn_action", None)
        fn = getattr(self, f"action_{action}", None) if action else None
        if fn is not None:
            fn()

    @on(Click, ".act-btn")
    def _on_act_click(self, event: Click) -> None:
        action = getattr(event.widget, "_btn_action", None)
        fn = getattr(self, f"action_{action}", None) if action else None
        if fn is not None:
            fn()

    # ── onboarding call-to-action (the tappable "get started" button) ──────
    def _cta_label(self) -> str:
        return "🚀  Click here to get started" if EMOJI else "»  Click here to get started"

    def _hide_cta(self) -> None:
        try:
            self.query_one("#cta", Static).display = False
        except Exception:
            pass

    @on(Click, "#cta")
    def _on_cta_click(self, event: Click) -> None:
        self.action_get_started()

    def action_get_started(self) -> None:
        """Hand the agent a kickoff message that drives the full guided install.
        The tap is explicit consent, so we enable writes (the install steps are
        mutating) and hide the button, then run the turn."""
        if self.project_root is not None:
            return
        if not self.provider.configured:
            self._log(f"[{self.cc['tool']}]{G.WARN} No AI key configured.[/] "
                      "Set LLM_API_KEY (the app key), or link a repo that has one, then tap again.")
            return
        if not (self.cfg.writes_enabled or self.cfg.autonomous):
            self.cfg.writes_enabled = True
            self.cfg.save()
            self._refresh_status()
            self._log(f"[{self.cc['secondary']}]{G.BULLET} writes enabled for setup[/]")
        self._hide_cta()
        kickoff = (
            "Let's get started setting up webAgent on this device. Walk me through the full "
            "install step by step — check readiness, clone, build the environment, seed the "
            "config, verify, and link it — confirming the install folder with me first. Handle "
            "problems automatically as they arise (e.g. the headless browser isn't available on "
            "Android/Termux: skip it and explain, don't treat it as a failure). When it's "
            "running, set up the home-screen shortcut and tell me how to add it. Keep me posted "
            "in plain language at each step."
        )
        self._log_user("Click here to get started")
        self._run_turn(kickoff)

    # ── direct (button / auto) server control — explicit intent, not gated ──
    def _server_ctx(self):
        from .tools.base import ToolContext
        return ToolContext(
            project_root=self.project_root,
            writes_enabled=True,
            autonomous=self.cfg.autonomous,
            log=lambda s: None,
            audit=lambda tool, args, ok, detail: self.store.log_action(
                self.session_id, tool, args, ok, detail),
            session_id=self.session_id,
        )

    def _admin_ctx(self):
        """A ToolContext for direct admin actions (Update / Uninstall): writes are on
        (the confirm dialog IS the consent) and request_exit is wired so a restart or
        uninstall can close the manager."""
        from .tools.base import ToolContext
        return ToolContext(
            project_root=self.project_root,
            writes_enabled=True,
            autonomous=self.cfg.autonomous,
            log=lambda s: None,
            audit=lambda tool, args, ok, detail: self.store.log_action(
                self.session_id, tool, args, ok, detail),
            session_id=self.session_id,
            set_project=self._link_project,
            app_provider=self.provider,
            request_exit=self._request_exit,
        )

    async def _do_server(self, which: str) -> None:
        from .tools import server as srv
        fn = {"start": srv.server_start, "stop": srv.server_stop,
              "restart": srv.server_restart}[which]
        if which in ("start", "restart"):
            self._server_state = "starting"     # spin the header pill while it boots
            self._refresh_status()
        msg = await fn(self._server_ctx())
        self._log(f"[{self.cc['dim']}]{msg}[/]")
        self._server_state = await server_health() if self.project_root else "n/a"
        self._refresh_status()

    def _log_watchdog(self, text: str) -> None:
        """Transcript sink for the watchdog/notifier. Rendered as a distinct
        notification block — bordered, accent-coloured, with its own CSS class
        (``msg-notify``) so it stands apart from user/agent/web-app messages."""
        try:
            # Strip any leading bell/emoji prefix if present; the CSS border + accent
            # colour already signal "this is a notification" — cleaner without it.
            clean = text.lstrip("\u2600\ufe0f \U0001f514")  # strip ☀️/🔔 + spaces
            self._mount(Static(Text(clean, style=self.cc["accent"]),
                               classes="msg-notify", markup=False))
        except Exception:
            pass

    async def _watchdog_restart(self) -> str:
        """Restart hook handed to the watchdog for autonomous recovery. Reuses the
        same gated-off direct server context the UI buttons use and refreshes the
        header pill so the UI reflects the restart."""
        from .tools import server as srv
        self._server_state = "starting"
        self._refresh_status()
        msg = await srv.server_restart(self._server_ctx())
        self._server_state = await server_health() if self.project_root else "n/a"
        self._refresh_status()
        return msg

    def _watchdog_inject(self, text: str) -> None:
        """Watchdog self-heal hook (mk2): a serious condition becomes an agent event.
        Surface it, then -- if the agent is idle -- trigger an autonomous turn to
        diagnose + remediate; if it's mid-turn, queue it so the running turn folds
        it in. Only ever called under self_heal autonomy (the watchdog gates it)."""
        self._log_watchdog(f"{G.WARN} watchdog -> agent: {text}")
        if not self.provider.configured:
            return
        msg = f"[watchdog self-heal] {text} Diagnose and fix this autonomously if you can."
        if self._busy:
            self._steer_queue.append(msg)
        else:
            self._run_turn(msg, synthetic=True)

    async def _autostart_server(self) -> None:
        """Start the managed server on open if it isn't already up — so a manual
        Launch control is unnecessary. Best-effort; never double-starts."""
        from .tools import server as srv
        if self.project_root is None:
            return
        if await server_health() == "running":
            self._server_state = "running"
            self._refresh_status()
            return
        if srv._venv_python(self.project_root) is None:
            self._server_state = "stopped"
            self._refresh_status()
            return
        self._log(f"[{self.cc['dim']}]auto-starting the server…[/]")
        await self._do_server("start")

    async def _check_self_update(self) -> None:
        """Best-effort startup probe: is a newer manager available upstream? Caches
        the result for the per-turn situation and flags it once in the transcript."""
        try:
            st = await check_self_update(self._self_info)
        except Exception:
            self._self_update_state = "manager update: check failed"
            return
        self._self_update_state = "manager " + st.summary
        if st.behind:
            self._log(f"[{self.cc['tool']}]{G.WARN} A newer Server Manager is available "
                      f"({st.summary}).[/] [{self.cc['dim']}]Ask me to 'update yourself' to install it.[/]")

    async def _request_exit(self) -> None:
        """Close the manager shortly after the current message renders, so a staged
        self-update swap / source reload (scheduled by self_restart) can finish."""
        self._log(f"[{self.cc['tool']}]{G.BULLET} restarting the manager…[/]")
        self.set_timer(1.2, self.exit)

    # ── actions ──────────────────────────────────────────────────────────
    def action_exit(self) -> None:
        # Esc toggles the side MENU: it closes an open confirm dialog or panel, and
        # otherwise OPENS the App panel (the keyboard way in). Quitting is Ctrl+Q.
        if isinstance(self.screen, ConfirmModal):
            self.screen.dismiss(False)
            return
        if self._panel_open():
            self._close_panel()
        else:
            self._open_panel("admin")

    def action_close_panel(self) -> None:
        """Close the side panel. Triggered by the ✕ header button — always closes
        regardless of which panel is open (or no-ops if none)."""
        self._close_panel()

    def action_quit_app(self) -> None:
        self.exit()

    # ── header categories → thin docked right-side panel ──────────────────
    def _panel_open(self) -> bool:
        return self._panel_kind is not None

    def _close_panel(self) -> None:
        self._panel_kind = None
        self._refresh_status()
        self.run_worker(self._render_panel(None), group="panel", exclusive=True)

    def _open_panel(self, kind: str) -> None:
        """Open (or, if already open for this category, close) the docked side panel.
        The chat column stays visible to its left; the panel is rebuilt from live
        state each time so mode highlights / server buttons / the key field are fresh."""
        if self._panel_kind == kind:
            self._close_panel()
            return
        self._panel_kind = kind
        self._refresh_status()
        self.run_worker(self._render_panel(kind), group="panel", exclusive=True)

    def _rebuild_panel(self) -> None:
        """Re-render the open panel in place (after a state change, e.g. a saved key)."""
        if self._panel_kind:
            self.run_worker(self._render_panel(self._panel_kind), group="panel", exclusive=True)

    async def _render_panel(self, kind: str | None) -> None:
        """Swap the docked panel's contents (or hide it when kind is None). Removal is
        awaited before mounting so widget IDs never collide; an exclusive worker group
        means rapid category switches cancel the previous render cleanly."""
        try:
            panel = self.query_one("#side-panel", Vertical)
        except Exception:
            return
        await panel.remove_children()
        try:
            grip = self.query_one("#panel-grip", PanelGrip)
        except Exception:
            grip = None
        if kind is None:
            panel.display = False
            if grip is not None:
                grip.display = False
            return
        panel.set_class(self.cfg.side_expanded, "side-wide")
        await panel.mount(*self._panel_widgets(kind))
        panel.display = True
        if grip is not None:
            grip.display = True

    def _panel_btn(self, label: str, action: str, active: bool = False) -> Static:
        b = Static(label, classes="panel-btn panel-btn-active" if active else "panel-btn",
                   markup=False)
        b._btn_action = action  # type: ignore[attr-defined]
        return b

    def _value_btn(self, label: str, action: str, cls: str, value) -> Static:
        """A panel button that carries a data value (agent/session) for its own
        class-specific click handler; the generic .panel-btn handler skips it."""
        b = Static(label, classes=f"panel-btn {cls}", markup=False)
        b._btn_action = action       # type: ignore[attr-defined]
        b._btn_value = value         # type: ignore[attr-defined]
        return b

    def _panel_widgets(self, kind: str) -> list[Widget]:
        """Build the widgets for one panel view. Every view starts with an
        expand/collapse toggle + a TITLE; then the view's content (category buttons,
        Scene/App controls, or the confirm / connect / config views)."""
        c = self.cc
        title = {"admin": "ADMIN", "scene": "THEME", "server": "SERVER",
                 "git": "GIT", "connect": "WEBAGENT", "config": "APP CONFIG",
                 "sessions": "ALL SESSIONS", "reset": "RESET",
                 "diag": "DIAGNOSTICS", "logs": "SERVER LOGS",
                 "confirm": (self._confirm_state or {}).get("title", "CONFIRM")}.get(kind, kind.upper())
        # Top row in EVERY view: an expand/collapse toggle, then the title.
        exp_label = "[›‹ narrow]" if self.cfg.side_expanded else "[‹› wide]"
        out: list[Widget] = [
            Horizontal(self._panel_btn(exp_label, "panel_expand"),
                       Static(Text(title, style=f"bold {c['accent']}"), id="panel-title"),
                       classes="panel-top"),
        ]
        if kind == "confirm":
            st = self._confirm_state or {}
            out.append(Static(Text(st.get("body", ""), style=c["fg"]), classes="confirm-body", markup=False))
            out.append(Horizontal(
                self._panel_btn(f"[{st.get('label', 'Confirm')}]", "confirm_yes"),
                self._panel_btn("[Cancel]", "confirm_no"), classes="panel-row"))
            return out
        if kind == "reset":
            return out + self._reset_widgets()
        if kind == "git":
            return out + self._git_widgets()
        if kind == "connect":
            return out + self._connect_widgets()
        if kind == "sessions":
            return out + self._sessions_widgets()
        if kind == "config":
            return out + self._config_widgets()
        if kind == "scene":
            for label, sel in _scene_rows(self):
                out.append(Horizontal(Static(label, classes="set-label"), sel, classes="set-row"))
            return out
        if kind in ("diag", "logs"):
            return out + self._readout_widgets(kind)
        specs = {
            "admin": [("[App Config]", "panel_config"),
                      ("[Commands]", "help"), ("[Update]", "admin_update"),
                      ("[Install]", "install"), ("[Reset]", "admin_reset"),
                      ("[Uninstall]", "admin_uninstall"),
                      ("[Diagnostics]", "diagnostics"), ("[Logs]", "server_logs")],
            "server": [("[Start]", "server_start"), ("[Restart]", "server_restart"),
                       ("[Kill]", "server_stop")],
        }.get(kind, [])
        for label, action in specs:
            out.append(self._panel_btn(label, action))
        return out

    # ── Git view (source control: token + fetch / pull / push) ────────────────
    def _git_widgets(self) -> list[Widget]:
        """The Git panel: a GitHub token field (Save/Clear) used to authenticate
        network ops, then Fetch / Pull / Push — each hands the agent a plain-language
        request so it runs the matching git_tool op (with the op-safety rules)."""
        c = self.cc
        out: list[Widget] = []
        out.append(Static(Text("GitHub token", style=c["dim"]), classes="panel-sub"))
        out.append(Input(value=self.cfg.git_token, id="git-token-input",
                         placeholder="paste GitHub token…", password=True))
        out.append(Horizontal(self._panel_btn("[Save]", "git_token_save"),
                              self._panel_btn("[Clear]", "git_token_clear"),
                              classes="panel-row"))
        tnote = "✓ token set" if self.cfg.git_token else "no token (using the host's git credentials)"
        tcol = c["secondary"] if self.cfg.git_token else c["dim"]
        out.append(Static(Text(tnote, style=tcol), id="git-token-status", classes="panel-sub"))
        out.append(Static(Text("Actions", style=c["dim"]), classes="panel-sub"))
        out.append(self._panel_btn("[Fetch]", "git_fetch"))
        out.append(self._panel_btn("[Pull]", "git_pull"))
        out.append(self._panel_btn("[Commit]", "git_commit"))
        out.append(self._panel_btn("[Commit + Pull]", "git_commit_pull"))
        out.append(self._panel_btn("[Push]", "git_push"))
        return out

    # ── WEBAGENT / Connect view (agent + session as DROPDOWNS, set target, mutes) ──
    def _connect_widgets(self) -> list[Widget]:
        c = self.cc
        out: list[Widget] = []
        t = self._webapp_target
        if t:
            out.append(Static(Text(f"Target: {t['agent_name']}\nsession {t['session_id'][:18]}",
                                   style=c["secondary"]), classes="panel-sub", markup=False))
            wlabel = "[Unmute WebAgent]" if self._webagent_muted else "[Mute WebAgent]"
            out.append(self._panel_btn(wlabel, "toggle_webagent", not self._webagent_muted))
            if not self._webagent_muted:
                mlabel = "[Unmute Manager]" if self._manager_muted else "[Mute Manager]"
                out.append(self._panel_btn(mlabel, "toggle_manager", self._manager_muted))
        # Agent dropdown.
        out.append(Static(Text("Agent", style=c["dim"]), classes="panel-sub"))
        if not self._connect_agents:
            out.append(Static(Text("(no agents loaded — Refresh)", style=c["dim"]), classes="panel-sub"))
        else:
            a_opts = [(a.get("name", "(agent)"), a.get("id", "")) for a in self._connect_agents]
            a_ids = [v for _, v in a_opts]
            cur_a = self._connect_agent.get("id") if self._connect_agent else None
            out.append(Select(a_opts,
                              value=cur_a if cur_a in a_ids else Select.BLANK,
                              prompt="Select an agent…",
                              id="connect-agent-select", classes="connect-select"))
        # Session dropdown — only once an agent is chosen.
        if self._connect_agent is not None:
            out.append(Static(Text(f"Session · {self._connect_agent.get('name', '')}",
                                   style=c["dim"]), classes="panel-sub"))
            s_opts: list[tuple[str, str]] = [("+ New session", "__new__")]
            for s in self._connect_sessions:
                stitle = (s.get("title") or "(untitled)").strip()[:28]
                s_opts.append((stitle, s.get("id", "")))
            s_ids = [v for _, v in s_opts]
            cur_s = t.get("session_id") if t else None
            out.append(Select(s_opts,
                              value=cur_s if (cur_s and cur_s in s_ids) else Select.BLANK,
                              prompt="Select a session…",
                              id="connect-session-select", classes="connect-select"))
        out.append(self._panel_btn("[Refresh]", "connect_refresh"))
        return out

    # ── Diagnostics / Logs readout (rendered IN the sidebar, wrapping) ─────────
    def _readout_widgets(self, kind: str) -> list[Widget]:
        """A read-only panel view for Diagnostics or Server Logs. The text wraps to
        the panel width and the panel itself scrolls — so it reads as a half-screen
        side view instead of dumping a wall of text into the transcript."""
        c = self.cc
        out: list[Widget] = []
        text = (self._diag_text if kind == "diag" else self._logs_text)
        out.append(self._panel_btn("[Refresh]", "diag_refresh" if kind == "diag" else "logs_refresh"))
        body = text or "(loading…)"
        out.append(Static(_linkify_text(Text(body, style=c["dim"])),
                          classes="readout-body", markup=False))
        return out

    # ── Sessions view (resume a previous TUI session) ─────────────────────────
    def _sessions_widgets(self) -> list[Widget]:
        c = self.cc
        out: list[Widget] = []
        sessions = self.store.list_sessions(limit=50)
        if not sessions:
            out.append(Static(Text("No previous sessions.", style=c["dim"]), classes="panel-sub"))
            return out
        out.append(Static(Text(f"{len(sessions)} session(s) — click to resume",
                               style=c["dim"]), classes="panel-sub"))
        for s in sessions:
            sid = s["id"]
            title = s.get("title") or "(untitled)"
            short_id = sid[:12]
            when = ""
            if s.get("updated_at"):
                import time as _time
                age = _time.time() - s["updated_at"]
                if age < 120:
                    when = "just now"
                elif age < 7200:
                    when = f"{int(age // 60)}m ago"
                elif age < 86400:
                    when = f"{int(age // 3600)}h ago"
                else:
                    when = f"{int(age // 86400)}d ago"
            label = f"{title}  [{short_id}]"
            if when:
                label += f"  {when}"
            active = " panel-btn-active" if sid == self.session_id else ""
            out.append(self._value_btn(label, "resume_session",
                                       "resume-session" + active, s))
        out.append(Static(Text("Click a session to switch to it.",
                               style=c["dim"]), classes="panel-sub"))
        return out

    # ── Config view (app-settings.json + the LLM auth key) ────────────────────
    def _config_widgets(self) -> list[Widget]:
        c = self.cc
        s, p = self._cfg_settings, self._cfg_provider
        out: list[Widget] = [Static(Text("App settings", style=f"bold {c['accent']}"), classes="panel-sub")]
        if not s:
            out.append(Static(Text("(loading… or Refresh)", style=c["dim"]), classes="panel-sub"))
        else:
            modes = ["public_anonymous", "public_registered", "admin_approval", "private"]
            am = s.get("access_mode", "public_anonymous")
            out.append(Static(Text("Access mode", style=c["dim"]), classes="panel-sub"))
            out.append(Select([(m, m) for m in modes],
                              value=am if am in modes else "public_anonymous",
                              allow_blank=False, id="cfg-access"))
            out.append(Static(Text("Presentation mode", style=c["dim"]), classes="panel-sub"))
            out.append(Select([("On", True), ("Off", False)],
                              value=bool(s.get("presentation_mode", False)),
                              allow_blank=False, id="cfg-present"))
            out.append(Static(Text("Render recorder (browser capture)", style=c["dim"]), classes="panel-sub"))
            out.append(Select([("On", True), ("Off", False)],
                              value=bool(s.get("render_recording_enabled", False)),
                              allow_blank=False, id="cfg-recorder"))
            out.append(Static(Text("Max tool calls/turn", style=c["dim"]), classes="panel-sub"))
            out.append(Input(value=str(s.get("max_tool_calls", 25)),
                             id="cfg-max-tool-calls", placeholder="25 (0 = unlimited)"))
            out.append(Static(Text("Max wall clock (seconds)", style=c["dim"]), classes="panel-sub"))
            out.append(Input(value=str(s.get("max_wall_seconds", 600)),
                             id="cfg-max-wall", placeholder="600 (0 = unlimited)"))
            out.append(Static(Text("Max identical tool calls", style=c["dim"]), classes="panel-sub"))
            out.append(Input(value=str(s.get("max_identical_tool_calls", 0)),
                             id="cfg-max-identical", placeholder="0 = disabled"))
            out.append(Static(Text("Run resume attempts", style=c["dim"]), classes="panel-sub"))
            out.append(Input(value=str(s.get("run_max_resume_attempts", 3)),
                             id="cfg-max-resume", placeholder="3"))
            out.append(Static(Text("Frozen threshold (seconds)", style=c["dim"]), classes="panel-sub"))
            out.append(Input(value=str(s.get("run_frozen_threshold_seconds", 360)),
                             id="cfg-frozen", placeholder="360"))
            out.append(Static(Text("Stream buffer retention (s)", style=c["dim"]), classes="panel-sub"))
            out.append(Input(value=str(s.get("stream_buffer_retention_seconds", 60)),
                             id="cfg-buffer", placeholder="60"))
            out.append(self._panel_btn("[Save settings]", "cfg_save_settings"))
        # ── LLM auth key ─────────────────────────────────────────────────
        # Provider quick-pick pills (moved here from the old App panel): one tap
        # fills Base URL + Model below; "Custom" leaves them for manual entry.
        out.append(Static(Text("LLM auth key", style=f"bold {c['accent']}"), classes="panel-sub"))
        names = [n for n, _, _ in PROVIDER_PRESETS]
        cur_prov = p.get("provider", "") or ""
        self._cfg_provider_pick = cur_prov if cur_prov in names else "Custom"
        row: list[Widget] = []
        for n in names:
            active = " panel-btn-active" if n == self._cfg_provider_pick else ""
            row.append(self._value_btn(n, "cfg_provider_pick", "cfg-provider-pick" + active, n))
            if len(row) == 2:
                out.append(Horizontal(*row, classes="panel-row"))
                row = []
        if row:
            out.append(Horizontal(*row, classes="panel-row"))
        # The four key fields as borderless lines sharing one tinted box (no labels —
        # the placeholders name each line). ~8 lines tall: 4 inputs + a gap each.
        out.append(Vertical(
            Input(value=p.get("provider", ""), id="cfg-provider", placeholder="provider (e.g. openrouter)"),
            Input(value=p.get("base_url", ""), id="cfg-baseurl", placeholder="base URL  https://…/v1"),
            Input(value=p.get("model", ""), id="cfg-model", placeholder="model id"),
            Input(value=p.get("api_key", ""), id="cfg-apikey",
                  placeholder="API key  (blank = keep current)", password=True),
            classes="keybox"))
        out.append(self._panel_btn("[Save auth key]", "cfg_save_auth"))
        out.append(self._panel_btn("[Refresh]", "cfg_refresh"))
        return out

    def action_panel_admin(self) -> None:
        self._open_panel("admin")

    def action_panel_scene(self) -> None:
        self._open_panel("scene")

    def action_panel_server(self) -> None:
        if self.project_root is None:
            return
        self._open_panel("server")

    def action_panel_git(self) -> None:
        if self.project_root is None:
            return
        self._open_panel("git")

    def action_panel_connect(self) -> None:
        self._open_panel("connect")
        if self._panel_kind == "connect":
            self.run_worker(self._load_connect_agents(), group="connectload", exclusive=True)

    def action_panel_config(self) -> None:
        self._open_panel("config")
        if self._panel_kind == "config":
            self.run_worker(self._load_config(), group="cfgload", exclusive=True)

    def action_panel_expand(self) -> None:
        """Toggle the sidebar width (narrow ↔ wide); persists and applies live."""
        self.cfg.side_expanded = not self.cfg.side_expanded
        self.cfg.save()
        try:
            self.query_one("#side-panel", Vertical).set_class(self.cfg.side_expanded, "side-wide")
        except Exception:
            pass
        self._rebuild_panel()   # flip the toggle label

    # ── in-sidebar confirmations (replace pop-up modals) ──────────────────────
    def _open_sidebar_confirm(self, title: str, body: str, label: str, on_yes) -> None:
        self._confirm_state = {"title": title, "body": body, "label": label, "on_yes": on_yes}
        self._panel_kind = "confirm"
        self._refresh_status()
        self.run_worker(self._render_panel("confirm"), group="panel", exclusive=True)

    def action_confirm_yes(self) -> None:
        st = self._confirm_state or {}
        on_yes = st.get("on_yes")
        self._confirm_state = None
        self._close_panel()
        if callable(on_yes):
            on_yes()

    def action_confirm_no(self) -> None:
        self._confirm_state = None
        self._close_panel()

    # ── connect view: target + the two mutes ──────────────────────────────────
    async def _load_connect_agents(self) -> None:
        try:
            self._connect_agents = await self._webapp.list_agents()
        except WebAppError as e:
            self._connect_agents = []
            self._log(f"[{self.cc['tool']}]{G.WARN} {e}[/]")
        self._rebuild_panel()

    async def _load_connect_sessions(self) -> None:
        agent = self._connect_agent or {}
        try:
            self._connect_sessions = await self._webapp.list_sessions(agent_id=agent.get("id", ""))
        except WebAppError as e:
            self._connect_sessions = []
            self._log(f"[{self.cc['tool']}]{G.WARN} {e}[/]")
        self._rebuild_panel()

    def action_connect_refresh(self) -> None:
        self.run_worker(self._load_connect_agents(), group="connectload", exclusive=True)

    def action_toggle_webagent(self) -> None:
        self.set_webagent_muted(not self._webagent_muted)
        self._rebuild_panel()

    def action_toggle_manager(self) -> None:
        self.set_manager_muted(not self._manager_muted)
        self._rebuild_panel()

    @on(Click, ".cfg-provider-pick")
    def _on_cfg_provider_pick(self, event: Click) -> None:
        """Pick a provider preset in App Config: remember it, fill Base URL + Model from
        the preset (Custom leaves them as typed), and move the highlight in place — so a
        key already typed into the fields isn't lost to a rebuild."""
        name = getattr(event.widget, "_btn_value", None)
        if name is None:
            return
        self._cfg_provider_pick = str(name)
        self._apply_provider_preset(self._cfg_provider_pick)
        for b in self.query(".cfg-provider-pick"):
            b.set_class(getattr(b, "_btn_value", None) == name, "panel-btn-active")

    @on(Click, ".resume-session")
    def _on_resume_session(self, event: Click) -> None:
        s = getattr(event.widget, "_btn_value", None)
        if not s or not s.get("id"):
            return
        sid = s["id"]
        if sid == self.session_id:
            self._log(f"[{self.cc['dim']}]{G.BULLET} already on that session.[/]")
            self._close_panel()
            return
        # Switch to the chosen session — rebuild the transcript from its history
        import asyncio
        self._end_tool_group()
        # Clear the transcript and load the selected session's messages
        try:
            log = self.query_one("#log", VerticalScroll)
            log.remove_children()
        except Exception:
            pass
        old_sid = self.session_id[:12]
        self.session_id = sid
        self._dismiss_banner()
        # Reload all messages from DB into the transcript
        msgs = self.store.history(sid)
        for m in msgs:
            role = m.get("role", "")
            content = m.get("content", "")
            if role == "user":
                if m.get("content_kind") == "json":
                    try:
                        payload = json.loads(content) or {}
                    except Exception:
                        payload = {}
                    self._log_user(payload.get("text") or "", payload.get("images") or [])
                else:
                    self._log_user(content)
            elif role == "assistant":
                self._log_assistant(content)
            elif role == "tool" and m.get("tool_name"):
                tool_name = m["tool_name"]
                try:
                    args = json.loads(m.get("tool_calls") or "{}") if m.get("tool_calls") else {}
                except Exception:
                    args = {}
                self._add_tool_call(tool_name, args)
                try:
                    self._fill_tool_result(tool_name, content)
                except Exception:
                    pass
        # Notice banner
        title = s.get("title") or "(untitled)"
        self._mount(Static(
            Text(f" {G.OK} Resumed session: {title} [{sid[:12]}]",
                 style=f"bold {self.cc['secondary']}"),
            classes="msg-line",
            markup=False,
        ))
        self._close_panel()

    # ── config view: app-settings + LLM auth key ──────────────────────────────
    async def _load_config(self) -> None:
        try:
            self._cfg_settings = await self._webapp.get_app_settings()
        except WebAppError as e:
            self._cfg_settings = {}
            self._log(f"[{self.cc['tool']}]{G.WARN} settings: {e}[/]")
        try:
            self._cfg_provider = await self._webapp.get_provider()
        except WebAppError as e:
            self._cfg_provider = {}
            self._log(f"[{self.cc['tool']}]{G.WARN} provider: {e}[/]")
        self._rebuild_panel()

    def action_cfg_refresh(self) -> None:
        self.run_worker(self._load_config(), group="cfgload", exclusive=True)

    def action_cfg_save_settings(self) -> None:
        try:
            access = self.query_one("#cfg-access", Select).value
            present = self.query_one("#cfg-present", Select).value
            recorder = self.query_one("#cfg-recorder", Select).value
            max_tc = self.query_one("#cfg-max-tool-calls", Input).value.strip()
            max_wall = self.query_one("#cfg-max-wall", Input).value.strip()
            max_iden = self.query_one("#cfg-max-identical", Input).value.strip()
            max_resume = self.query_one("#cfg-max-resume", Input).value.strip()
            frozen = self.query_one("#cfg-frozen", Input).value.strip()
            buffer_ = self.query_one("#cfg-buffer", Input).value.strip()
        except Exception:
            return
        patch = {}
        if access is not Select.BLANK:
            patch["access_mode"] = access
        patch["presentation_mode"] = bool(present)
        patch["render_recording_enabled"] = bool(recorder)
        if max_tc:
            patch["max_tool_calls"] = max(0, int(max_tc))
        if max_wall:
            patch["max_wall_seconds"] = max(0, int(max_wall))
        if max_iden:
            patch["max_identical_tool_calls"] = max(0, int(max_iden))
        if max_resume:
            patch["run_max_resume_attempts"] = max(0, int(max_resume))
        if frozen:
            patch["run_frozen_threshold_seconds"] = max(15, int(frozen))
        if buffer_:
            patch["stream_buffer_retention_seconds"] = max(0, min(3600, int(buffer_)))
        self.run_worker(self._save_settings(patch), group="cfgsave", exclusive=True)

    async def _save_settings(self, patch: dict) -> None:
        try:
            merged = {**(self._cfg_settings or await self._webapp.get_app_settings()), **patch}
            self._cfg_settings = await self._webapp.set_app_settings(merged)
            self._log(f"[{self.cc['secondary']}]{G.OK} app settings saved.[/]")
        except WebAppError as e:
            self._log(f"[{self.cc['error']}]{G.ERR} {e}[/]")
        self._rebuild_panel()

    def action_cfg_save_auth(self) -> None:
        try:
            prov = self.query_one("#cfg-provider", Input).value.strip()
            base = self.query_one("#cfg-baseurl", Input).value.strip()
            model = self.query_one("#cfg-model", Input).value.strip()
            key = self.query_one("#cfg-apikey", Input).value.strip()
        except Exception:
            return
        self.run_worker(self._save_auth(prov, base, model, key), group="cfgsave", exclusive=True)

    async def _save_auth(self, provider: str, base_url: str, model: str, api_key: str) -> None:
        try:
            cfg = dict(self._cfg_provider or await self._webapp.get_provider())
            if provider:
                cfg["provider"] = provider
            if base_url:
                cfg["base_url"] = base_url
            if model:
                cfg["model"] = model
            if api_key:
                cfg["api_key"] = api_key
            await self._webapp.set_provider(cfg)
            self._cfg_provider = cfg
            self._log(f"[{self.cc['secondary']}]{G.OK} LLM auth key saved"
                      + (" (key updated)" if api_key else "") + ".[/]")
        except WebAppError as e:
            self._log(f"[{self.cc['error']}]{G.ERR} {e}[/]")
        self._rebuild_panel()

    # ── panel interactions: button clicks, click-outside, settings, AI key ──
    # Buttons whose action should NOT close the panel (they edit it in place).
    _KEEP_OPEN = {"panel_expand",
                  "panel_connect", "panel_config",
                  "git_token_save", "git_token_clear",
                  "toggle_webagent", "toggle_manager", "connect_refresh",
                  "cfg_save_settings", "cfg_save_auth", "cfg_refresh",
                  "diag_refresh", "logs_refresh",
                  "reset_do"}

    @on(Click, ".panel-btn")
    def _on_panel_btn(self, event: Click) -> None:
        # Buttons that carry a data value have their own class handler (connect-*).
        if getattr(event.widget, "_btn_value", None) is not None:
            return
        action = getattr(event.widget, "_btn_action", None)
        if action in self._KEEP_OPEN:
            fn = getattr(self, f"action_{action}", None)
            if fn is not None:
                fn()
            return
        self._close_panel()
        if action:
            fn = getattr(self, f"action_{action}", None)
            if fn is not None:
                fn()

    def on_click(self, event: Click) -> None:
        # Click-outside no longer closes the panel — only Esc or the ✕ header
        # button close it. This ensures clicking in the main/chat area never
        # dismisses an open side panel. Stay no-op here.
        pass

    @on(Select.Changed)
    def _on_setting_change(self, event: Select.Changed) -> None:
        sid = event.select.id or ""
        # WEBAGENT/Connect dropdowns: pick an agent (loads its sessions), then a session.
        if sid == "connect-agent-select":
            if event.value is Select.BLANK:
                return
            aid = event.value
            if self._connect_agent and self._connect_agent.get("id") == aid:
                return   # programmatic re-set on rebuild — ignore
            self._connect_agent = next(
                (a for a in self._connect_agents if a.get("id") == aid), None)
            self._connect_sessions = []
            self.run_worker(self._load_connect_sessions(), group="connectload", exclusive=True)
            return
        if sid == "connect-session-select":
            if event.value is Select.BLANK:
                return
            a = self._connect_agent or {}
            if event.value == "__new__":
                import uuid
                self.set_webapp_target(a.get("id", ""), a.get("name", "agent"),
                                       f"tui-{uuid.uuid4().hex[:16]}", "(new)")
            else:
                t = self._webapp_target
                if t and t.get("session_id") == event.value:
                    return   # programmatic re-set on rebuild — ignore
                s = next((x for x in self._connect_sessions if x.get("id") == event.value), None)
                if s:
                    self.set_webapp_target(a.get("id", ""), a.get("name", "agent"),
                                           s.get("id", ""), s.get("title", ""))
            self._rebuild_panel()
            return
        if event.value is Select.BLANK:
            return
        # Config-view selects (cfg-*) are read on [Save], not applied live.
        if sid.startswith("cfg-"):
            return
        # Scene controls apply live; the panel stays open so several can be tweaked.
        self.apply_setting(sid, event.value)

    def _apply_provider_preset(self, name: str) -> None:
        """Fill the App Config Base URL + Model fields from a provider preset."""
        preset = next((p for p in PROVIDER_PRESETS if p[0] == name), None)
        if preset is None or name == "Custom":
            return
        _, base, model = preset
        try:
            self.query_one("#cfg-provider", Input).value = name
            self.query_one("#cfg-baseurl", Input).value = base
            self.query_one("#cfg-model", Input).value = model
        except Exception:
            pass

    # ── Git panel: store the GitHub token + run fetch / pull / push ────────
    @on(Input.Submitted, "#git-token-input")
    def _git_token_enter(self, event: Input.Submitted) -> None:
        self.action_git_token_save()   # Enter in the token field == [Save]

    def action_git_token_save(self) -> None:
        """Persist the GitHub token used to authenticate fetch/pull/push. Stored in
        the TUI's own config (data dir), never written into the repo's .git/config."""
        try:
            tok = self.query_one("#side-panel", Vertical).query_one("#git-token-input", Input).value.strip()
        except Exception:
            return
        if tok:
            self.cfg.git_token = tok
            self.cfg.save()
            self._log(f"[{self.cc['secondary']}]{G.OK} GitHub token saved[/] "
                      f"[{self.cc['dim']}]— used to authenticate fetch/pull/push[/]")
        else:
            self._log(f"[{self.cc['tool']}]{G.WARN} no token entered — paste one and Save again.[/]")
        self._rebuild_panel()

    def action_git_token_clear(self) -> None:
        self.cfg.git_token = ""
        self.cfg.save()
        self._log(f"[{self.cc['dim']}]{G.BULLET} GitHub token cleared "
                  f"(falling back to the host's git credentials, if any).[/]")
        self._rebuild_panel()

    def _git_request(self, prompt: str, label: str, needs_writes: bool) -> None:
        """Hand the agent a plain-language git request (the button → server-manager
        bridge). Mutating ops (pull/push) arm writes first — the click is the consent,
        mirroring the get-started flow — so the agent's git_tool isn't refused."""
        if self.project_root is None:
            self._log(f"[{self.cc['dim']}]no checkout linked — nothing to {label}[/]")
            return
        if not self.provider.configured:
            self._log(f"[{self.cc['tool']}]{G.WARN} No AI key configured.[/] "
                      "Set a provider + key in Admin ▸ App Config first.")
            return
        if needs_writes and not (self.cfg.writes_enabled or self.cfg.autonomous):
            self.cfg.writes_enabled = True
            self.cfg.save()
            self._refresh_status()
            self._log(f"[{self.cc['secondary']}]{G.BULLET} writes enabled for git {label}[/]")
        self._log_user(f"Git: {label}")
        self._run_turn(prompt)

    def action_git_fetch(self) -> None:
        self._git_request(
            "Fetch from the git remote (run git_tool with operation 'fetch') and tell me "
            "what's new on the upstream branch. Don't merge or change my working tree.",
            "fetch", needs_writes=False)

    def action_git_pull(self) -> None:
        self._git_request(
            "Pull the latest changes from the git remote into the current branch (git_tool "
            "operation 'pull'). If there are uncommitted local changes that would block it, or "
            "a merge conflict arises, stop and tell me before doing anything destructive. "
            "Summarize what changed.",
            "pull", needs_writes=True)

    def action_git_commit(self) -> None:
        self._git_request(
            "Commit my current changes (git_tool). First run git status to see what's staged and "
            "unstaged; stage all tracked changes, then commit with a concise, descriptive message "
            "summarizing what changed. Do NOT push. If there's nothing to commit, tell me.",
            "commit", needs_writes=True)

    def action_git_commit_pull(self) -> None:
        self._git_request(
            "Commit my current changes, then pull the latest from the remote (git_tool). "
            "Steps: 1) git status; 2) stage all tracked changes and commit with a concise, "
            "descriptive message; 3) pull from the remote on the current branch. If a merge "
            "conflict arises or the pull would be destructive, stop and tell me before continuing. "
            "Do not push. Summarize what was committed and what came down from the remote.",
            "commit + pull", needs_writes=True)

    def action_git_push(self) -> None:
        self._git_request(
            "Push my committed changes to the git remote on the current branch (git_tool "
            "operation 'push'). Never force-push. First check git status; if there's nothing to "
            "push or the push is rejected, tell me why instead of forcing it.",
            "push", needs_writes=True)

    # ── App panel: set the write gate directly (Read / Write / Auto) ───────
    def _set_mode(self, mode: str) -> None:
        """Set the agent's write gate directly. read = no writes; write = writes on;
        auto = writes on + autonomous (mutating tools run without per-call gating)."""
        self.cfg.writes_enabled = mode in ("write", "auto")
        self.cfg.autonomous = (mode == "auto")
        self.cfg.save()
        self._refresh_status()
        label = {"read": "read-only", "write": "writes", "auto": "autonomous"}[mode]
        self._log(f"[{self.cc['secondary']}]{G.BULLET} mode: {label}[/]")

    def action_mode_cycle(self) -> None:
        """Header mode pill: cycle read → write → auto on each click."""
        order = ["read", "write", "auto"]
        cur = "auto" if self.cfg.autonomous else "write" if self.cfg.writes_enabled else "read"
        self._set_mode(order[(order.index(cur) + 1) % len(order)])

    def action_mode_read(self) -> None:
        self._set_mode("read")

    def action_mode_write(self) -> None:
        self._set_mode("write")

    def action_mode_auto(self) -> None:
        self._set_mode("auto")

    # ── Admin panel: Install (guided setup, with a warning + Yes/No first) ──
    def action_install(self) -> None:
        """Run the guided install. Onboarding mode → show a warning + confirm, then
        drive the full setup. Managed mode → there's already a linked checkout, so
        point the user at Update instead of re-installing."""
        if self.project_root is not None:
            self._log(f"[{self.cc['dim']}]a webAgent checkout is already linked "
                      f"({self.project_root}) — use Admin ▸ Update to upgrade it.[/]")
            return
        body = "\n".join([
            f"This installs webAgent on this device (recommended folder: "
            f"{self._recommended_install_path()}):",
            "  • clone the repo from GitHub",
            "  • build a Python virtualenv + install dependencies",
            "  • download the headless browser (desktop only)",
            "  • seed the config and your AI key, then verify",
            "",
            "It enables writes for the setup and may take several minutes.",
        ])
        self._open_sidebar_confirm("Install webAgent", body, "Install now",
                                   lambda: self._after_install_confirm(True))

    def _after_install_confirm(self, ok: bool | None) -> None:
        if ok:
            self.action_get_started()

    def action_server_restart(self) -> None:
        if self.project_root is None:
            self._log(f"[{self.cc['dim']}]no server to restart in onboarding mode[/]")
            return
        self.run_worker(self._do_server("restart"), group="server", exclusive=True)

    def action_server_stop(self) -> None:
        if self.project_root is None:
            self._log(f"[{self.cc['dim']}]no server to stop in onboarding mode[/]")
            return
        self.run_worker(self._do_server("stop"), group="server", exclusive=True)

    def action_server_start(self) -> None:
        if self.project_root is None:
            self._log(f"[{self.cc['dim']}]no server to start in onboarding mode[/]")
            return
        self.run_worker(self._do_server("start"), group="server", exclusive=True)

    def action_server_logs(self) -> None:
        if self.project_root is None:
            self._log(f"[{self.cc['dim']}]no server logs in onboarding mode (link a checkout first)[/]")
            return
        # Open Logs as a wide, scrolling sidebar half-view (not a transcript dump).
        self._logs_text = ""
        self.cfg.side_expanded = True
        self._open_panel("logs")
        self.run_worker(self._show_logs(), group="diag", exclusive=True)

    def action_logs_refresh(self) -> None:
        self.run_worker(self._show_logs(), group="diag", exclusive=True)

    async def _show_logs(self) -> None:
        from .tools import server as srv
        msg = await srv.server_logs(self._server_ctx(), lines=60)
        self._logs_text = msg
        if self._panel_kind == "logs":
            self._rebuild_panel()
        else:
            self._log_block(msg)

    def action_diagnostics(self) -> None:
        if self.project_root is None:
            self._log(f"[{self.cc['dim']}]no diagnostics in onboarding mode (link a checkout first)[/]")
            return
        # Open Diagnostics as a wide, scrolling sidebar half-view.
        self._diag_text = ""
        self.cfg.side_expanded = True
        self._open_panel("diag")
        self.run_worker(self._show_diagnostics(), group="diag", exclusive=True)

    def action_diag_refresh(self) -> None:
        self.run_worker(self._show_diagnostics(), group="diag", exclusive=True)

    async def _show_diagnostics(self) -> None:
        from .tools import diagnostics as diag
        msg = await diag.read_diagnostics(self._server_ctx(), limit=20)
        self._diag_text = msg
        if self._panel_kind == "diag":
            self._rebuild_panel()
        else:
            self._log_block(msg)

    # ── admin panel: Commands (a user-facing reference, printed to the transcript) ──
    def action_help(self) -> None:
        """Print a reference of the controls, keyboard shortcuts, things to ask the
        agent, and the terminal commands for install / launch / uninstall."""
        c = self.cc
        self._close_panel()

        def head(t: str) -> None:
            self._log(f"\n[b {c['accent']}]{t}[/]")

        def line(label: str, what: str) -> None:
            self._log(f"  [{c['secondary']}]{label}[/]  [{c['dim']}]{what}[/]")

        self._log(f"\n[b {c['primary']}]{G.ADMIN} webAgent — command reference[/]")

        head("On-screen controls")
        line("Admin", "App Config · Commands · Update · Install · Reset · Uninstall · Diagnostics · Logs")
        line("Git", "GitHub token (Save/Clear), then Fetch · Pull · Commit · Commit+Pull · Push")
        line("WEBAGENT", "connect to a live web agent + session (dropdowns), mute/unmute")
        line("App Config", "AI provider + key, plus the app's server settings (in Admin)")
        line("mode pill", "far-left header word — click to cycle Read → Write → Auto")
        line("theme", "Ctrl+T cycles the 23 colour themes (no header button)")
        line("status pill", "click the live/stopped status → Start · Restart · Kill")
        line("Stop / Continue", "above the input — cancel a turn / resume it")
        line("click a pill", "opens its menu; click outside or Esc to close it")

        head("Keyboard")
        line("Enter", "send your message")
        line("Esc", "open the side menu (or close it)")
        line("Ctrl+Q", "quit the manager")
        line("Ctrl+T", "cycle theme")
        line("Ctrl+A / C / V / X", "select-all / copy / paste / cut (input field)")
        line("Shift+drag", "select transcript text with your terminal's native copy")
        if self.facts.is_termux:
            line("Vol-Up then K", "toggle the Android keyboard (or Termux drawer ▸ KEYBOARD)")

        head("Ask the agent (plain language)")
        for ask in ("install webAgent  /  link <folder>",
                    "check the server status  /  is it running?",
                    "diagnose the problem  /  show the logs",
                    "start / stop / restart the server",
                    "update yourself   (the manager pulls/rebuilds + restarts)",
                    "commit and push my changes",
                    "edit <file> / run <command>  (needs Write or Auto mode)"):
            self._log(f"  [{c['dim']}]{G.BULLET} {ask}[/]")

        head("Terminal commands")
        termux = [
            "# Install on Android/Termux (one line):",
            "pkg install -y curl && curl -fsSL https://webagent.live/termux | bash",
            "# Launch afterwards:",
            "webagent",
            "# Need Python 3.11/3.12 on Termux? use an Ubuntu proot:",
            "pkg install -y proot-distro && proot-distro install ubuntu",
            "proot-distro login ubuntu   # then: apt install python3.11 python3.11-venv git",
            "# Uninstall (Termux):",
            'rm -f "$PREFIX/bin/webagent" "$HOME/.shortcuts/webagent.sh"',
            "rm -rf ~/webagent ~/.local/share/webagent",
            "pip uninstall -y webagent",
        ]
        desktop = [
            "# Desktop: run from source / the packaged app:",
            "run.bat            (Windows, from the webagent folder)",
            "webagent.exe   (frozen build)",
        ]
        self._log_block("\n".join((termux if self.facts.is_termux else desktop)
                                  + ["", "Web UI when the server runs: http://localhost:8080/index.html"]))

    # ── admin panel: Update / Uninstall (each with an info + confirm screen) ──
    def _update_info(self) -> str:
        si = self._self_info
        if si.mode == "source":
            how = (f"Pulls the latest code from the repo and reloads on restart.\n"
                   f"Source: {si.repo_root}")
        else:
            how = ("Rebuilds the app from fresh source and swaps it in on restart "
                   "(needs git + Python 3.11/3.12 installed).")
        return "\n".join([
            how,
            f"Version {si.version} (build {si.build_commit or 'unstamped'}).",
            f"{self._self_update_state}.",
            "",
            "A backup is taken first and force is never used. The manager will close "
            "and relaunch to apply the update.",
        ])

    def action_admin_update(self) -> None:
        self._open_sidebar_confirm("Update webAgent", self._update_info(), "Update now",
                                   lambda: self._after_update_confirm(True))

    def _after_update_confirm(self, ok: bool | None) -> None:
        if ok:
            self.run_worker(self._do_update(), group="admin", exclusive=True)

    async def _do_update(self) -> None:
        from .tools import selfupdate
        self._log(f"[{self.cc['tool']}]{G.BULLET} updating — backing up, fetching, and restarting…[/]")
        msg = await selfupdate.self_update(self._admin_ctx(), make_backup=True, restart=True)
        self._log_block(msg)

    def _uninstall_info(self) -> str:
        from .config import data_dir
        si = self._self_info
        if not self.facts.is_termux:
            return ("Automatic uninstall is available on Android/Termux only. On this platform, "
                    "remove the manager's install by hand: its launcher/.exe, the cloned repo, "
                    "and its data folder.")
        repo = si.repo_root or "~/webagent"
        return "\n".join([
            "This permanently removes webAgent from this device:",
            "  • launcher:  $PREFIX/bin/webagent",
            "  • home-screen shortcut:  ~/.shortcuts/webagent.sh",
            f"  • repo + virtualenv:  {repo}",
            f"  • manager data (history, config, cached guide):  {data_dir()}",
            "  • the Python package (pip uninstall webagent)",
            "",
            "This cannot be undone. The manager closes immediately afterward.",
        ])

    def action_admin_uninstall(self) -> None:
        self._open_sidebar_confirm("Uninstall webAgent", self._uninstall_info(),
                                   "Remove everything",
                                   lambda: self._after_uninstall_confirm(True))

    def _after_uninstall_confirm(self, ok: bool | None) -> None:
        if ok:
            self.run_worker(self._do_uninstall(), group="admin", exclusive=True)

    async def _do_uninstall(self) -> None:
        from .tools import manage
        msg = await manage.uninstall(self._admin_ctx())
        self._log_block(msg)
        if self.facts.is_termux:
            self.set_timer(2.0, self.exit)

    # ── admin panel: Reset (wipe the app's data back to a clean state) ──
    def _reset_widgets(self) -> list[Widget]:
        """Build the reset panel: checkboxes for each wipe group + a Reset button."""
        c = self.cc
        from .tools.reset import _PG_PROVIDERS, _active_provider
        is_pg = (self.project_root is not None
                 and _active_provider(self.project_root) in _PG_PROVIDERS)

        db_label = ("  database  (Postgres — schema dropped & recreated, NOT backed up)"
                    if is_pg else
                    "  database  (local.db + journal/wal/shm sidecars)")
        pages_label = "  generated pages  (visuals/users/)"
        secrets_label = "  app secrets  (AI keys, OAuth tokens, integration creds, scheduler/db-mode config)"
        users_label = "  local accounts  (passwords + remember-me tokens)"
        env_label = "  .env file  (environment config — app won't boot without it)"
        agents_label = "  agent templates  (data/agents/*.json — zero agents after)"

        # Default state: database + pages always checked (always wiped).
        st = self._reset_state
        out: list[Widget] = [
            Static(Text("What to reset:", style=c["dim"]), classes="panel-sub"),
            Checkbox(db_label, id="reset-db", value=st.get("db", True)),
            Checkbox(pages_label, id="reset-pages", value=st.get("pages", True)),
            Checkbox(secrets_label, id="reset-secrets", value=st.get("secrets", False)),
            Checkbox(users_label, id="reset-users", value=st.get("users", False)),
            Checkbox(env_label, id="reset-env", value=st.get("env", False)),
            Checkbox(agents_label, id="reset-agents", value=st.get("agents", False)),
            Static(Text("", id="reset-note", style=c["tool"]), classes="panel-sub"),
            Horizontal(
                self._panel_btn("[Reset now]", "reset_do"),
                self._panel_btn("[Cancel]", "confirm_no"),
                classes="panel-row"),
        ]
        return out

    def action_admin_reset(self) -> None:
        if self.project_root is None:
            self._log(f"[{self.cc['dim']}]nothing to reset in onboarding mode (link a checkout first)[/]")
            return
        self._reset_state = {"db": True, "pages": True, "secrets": False,
                             "users": False, "env": False, "agents": False}
        self._panel_kind = "reset"
        self._refresh_status()
        self.run_worker(self._render_panel("reset"), group="panel", exclusive=True)

    def action_reset_do(self) -> None:
        """Read the checkboxes, run the reset, close the panel."""
        self.run_worker(self._do_reset(), group="admin", exclusive=True)

    async def _do_reset(self) -> None:
        from .tools import reset
        # Read checkbox states from the panel widgets.
        try:
            db = self.query_one("#reset-db", Checkbox).value
            pages = self.query_one("#reset-pages", Checkbox).value
            secrets = self.query_one("#reset-secrets", Checkbox).value
            users = self.query_one("#reset-users", Checkbox).value
            env = self.query_one("#reset-env", Checkbox).value
            agents = self.query_one("#reset-agents", Checkbox).value
        except Exception:
            self._log(f"[{self.cc['error']}]{G.ERR} could not read reset checkboxes[/]")
            self._close_panel()
            return

        # Save state so re-opening the panel restores the same choices.
        self._reset_state = {"db": db, "pages": pages, "secrets": secrets,
                             "users": users, "env": env, "agents": agents}

        if not any([db, pages, secrets, users, env, agents]):
            self._log(f"[{self.cc['tool']}]{G.WARN} nothing selected — no reset performed[/]")
            self._close_panel()
            return

        self._close_panel()
        self._log(f"[{self.cc['tool']}]{G.BULLET} resetting — stopping the server, backing up, and wiping data…[/]")
        msg = await reset.reset_app(
            self._admin_ctx(), backup=True,
            clear_secrets=secrets, clear_users=users,
            delete_env=env, delete_agents=agents,
        )
        self._log_block(msg)
        self._server_state = await server_health() if self.project_root else "n/a"
        self._refresh_status()

    # ── theme & animation ('Scene' panel) helpers ─────────────────────────
    def _build_palette(self):
        """The animation palette for the current choice: a named preset, or
        (default 'theme') one derived from the active theme's colours."""
        name = self.cfg.anim_palette
        if name and name != "theme":
            for p in PRESETS:
                if p.name == name:
                    try:
                        return p.builder()
                    except Exception:
                        break
        return palette_from_theme(self)

    def _retint_anim(self) -> None:
        if self._anim is not None:
            self._anim.set_palette(self._build_palette())

    def apply_setting(self, key: str, value) -> None:
        """Apply one picker change live + persist it (no-op if unchanged)."""
        cfg = self.cfg
        if key == "set-banner":
            on = bool(value)
            if on != self._anim_on:
                self._anim_on = on
                cfg.anim_enabled = on
                if self._anim is not None:
                    self._anim.display = on
                    self._anim.set_idle(not on)
                cfg.save()
            return
        if key == "set-theme":
            self._apply_theme(value)
            return
        changed = True
        if key == "set-anim" and value != cfg.anim_style:
            cfg.anim_style = value
            if self._anim is not None:
                self._anim.set_style(value)
        elif key == "set-pal" and value != cfg.anim_palette:
            cfg.anim_palette = value
            self._retint_anim()
        elif key == "set-speed" and float(value) != cfg.anim_speed:
            cfg.anim_speed = float(value)
            if self._anim is not None:
                self._anim.set_speed(float(value))
        elif key == "set-int" and float(value) != cfg.anim_intensity:
            cfg.anim_intensity = float(value)
            if self._anim is not None:
                self._anim.set_intensity(float(value))
        elif key == "set-fps" and int(value) != cfg.anim_fps:
            cfg.anim_fps = int(value)
            if self._anim is not None:
                self._anim.set_fps(int(value))
        else:
            changed = False
        if changed:
            cfg.save()

    def _apply_theme(self, name: str) -> None:
        if name not in THEME_ORDER or name == self.theme:
            return
        self.theme = name
        self.cc = chrome_colors(self)
        self._retint_anim()
        self.cfg.theme_name = name
        self.cfg.save()
        self._refresh_status()
        self._refresh_title()
        self._update_hud()
        self._log(f"[{self.cc['accent']}]theme: {THEME_LABELS.get(name, name)}[/]")

    def on_app_blur(self) -> None:
        if self._anim is not None:
            self._anim.set_idle(True)          # pause animation when unfocused (~0% CPU)

    def on_app_focus(self) -> None:
        if self._anim is not None:
            self._anim.set_idle(not self._anim_on)

    def action_cycle_theme(self) -> None:
        idx = THEME_ORDER.index(self.theme) if self.theme in THEME_ORDER else -1
        self._apply_theme(THEME_ORDER[(idx + 1) % len(THEME_ORDER)])

    @on(PromptInput.Submitted, "#prompt")
    def _submit(self, event: PromptInput.Submitted) -> None:
        self._do_submit(event.value.strip())

    @on(Click, "#send-btn")
    def _send_click(self) -> None:
        try:
            inp = self.query_one("#prompt", PromptInput)
            text = inp.text.strip()
        except Exception:
            return
        self._do_submit(text)

    def _do_submit(self, text: str) -> None:
        if not text and not self._attachments:
            return
        # Clear the input so the same text isn't resent on accident.
        try:
            self.query_one("#prompt", PromptInput).text = ""
        except Exception:
            pass
        # ── /new: start a fresh session (context reset) ──
        # Keeps the transcript visible but inserts a notice marking the reset,
        # and creates a brand-new session so the agent starts with empty context.
        # The notice includes a clickable [Clear] pill to wipe the old transcript.
        if text.strip() == "/new":
            old_id = self.session_id[:12]
            self.session_id = self.store.create_session(
                str(self.project_root) if self.project_root else "(onboarding)"
            )
            self._end_tool_group()
            clear_btn = Static(
                Text(" [Clear]", style=f"bold {self.cc['accent']}"),
                classes="session-clear-btn act-btn", markup=False,
            )
            clear_btn._btn_action = "clear_transcript"  # type: ignore[attr-defined]
            self._mount(Static(
                Text(
                    f" {G.NEW} ── Session reset (new context) ── \n"
                    f"  Previous session: {old_id} — transcript preserved above.\n",
                    style=f"bold {self.cc['fg']}",
                ),
                classes="msg-session-new",
                markup=False,
            ))
            self._mount(clear_btn)
            return

        # ── /resume: open the session-list panel ──
        if text.strip() == "/resume":
            self._open_panel("sessions")
            return
        # ── Direct-to-web-app mode: Manager muted, WebAgent live → my input goes
        # straight to the app agent (using the web app normally, from the TUI).
        if not self._webagent_muted and self._manager_muted:
            if not self._webapp_target:
                self._log(f"[{self.cc['tool']}]{G.WARN} No web-app session connected.[/] "
                          "Open Connect, pick an agent + session first.")
                return
            self._send_to_webapp(text)
            return
        # ── Manager modes (default, or bridged): the Manager handles the turn. ──
        if not self.provider.configured:
            self._log(f"[{self.cc['tool']}]{G.WARN} No AI key configured.[/] "
                      "Set LLM_API_KEY (the app key), or link a repo that has one.")
            return
        # Consume any pending image attachments for this turn, then clear the row.
        images = self._attachments
        self._attachments = []
        self._refresh_attach_row()
        self._log_user(text, images)
        if self._busy:
            # Mid-turn steer: don't start a second turn. Queue it; the running
            # turn folds it in at its next decision point and pivots.
            self._steer_queue.append(text)
            self._log(f"[{self.cc['dim']}]{G.BULLET} steering the current task...[/]")
        else:
            self._run_turn(text, images)

    # ── activity spinner + Stop / Continue ─────────────────────────────────
    def _set_busy(self, busy: bool) -> None:
        """Reflect agent activity: spin the spinner and flip which of Stop/Continue
        is enabled (Stop while busy, Continue while idle)."""
        self._busy = busy
        if self._spinner is not None:
            self._spinner.set_active(busy)
        try:
            self._stop_btn.set_class(not busy, "disabled")
            self._cont_btn.set_class(busy, "disabled")
        except Exception:
            pass

    def _dismiss_banner(self) -> None:
        """Collapse the logo banner once the session has activity, so it doesn't sit
        pinned above the transcript — the chat then uses the full height."""
        if self._anim is not None and self._anim.display:
            self._anim.display = False
            self._anim.set_idle(True)

    def action_stop(self) -> None:
        if not self._busy:
            return
        self.workers.cancel_group(self, "agent")
        self._log(f"[{self.cc['dim']}]{G.BULLET} stopped[/]")
        self._set_busy(False)

    def action_continue(self) -> None:
        if self._busy:
            return
        if not self.provider.configured:
            self._log(f"[{self.cc['tool']}]{G.WARN} No AI key configured.[/] "
                      "Open the App panel and set a provider + key.")
            return
        self._log_user("Continue.")
        self._run_turn("Please continue from where you left off.")

    # -- subagent orchestration (mk2 event-driven model) --
    def _drain_steer(self):
        """Hand the agent loop the next queued steering message (or None)."""
        return self._steer_queue.pop(0) if self._steer_queue else None

    def _after_turn_settle(self) -> None:
        """Called as a turn ends. If a subagent result surfaced or a steer queued
        right at the boundary, kick off one more (synthetic) turn so nothing is
        stranded. Idle-only -- a deliberate Stop is respected because Stop cancels
        the worker before this runs."""
        if self._busy or self.agent is None:
            return
        if self._subagents.has_surfaced_pending() or self._steer_queue:
            self._run_turn("", synthetic=True)

    def _log_subagent(self, text: str) -> None:
        """Transcript line for subagent lifecycle (start / done), styled like the
        watchdog notifications so it stands apart from the main conversation."""
        try:
            self._mount(Static(Text(text, style=self.cc["accent"]),
                               classes="msg-notify", markup=False))
        except Exception:
            pass

    async def _spawn_subagent(self, task: str, tools: list[str], mode: str,
                              group_id: str) -> str:
        """Launch a subagent worker and return immediately with its task id.

        The subagent runs in its OWN Store session (so the human can open it from
        /resume and watch its progress) with only the caller-scoped tools.
        Returns the human-readable line the main agent sees as the tool result."""
        assert self.agent is not None
        task_id = uuid.uuid4().hex[:8]
        title = f"{G.BULLET} sub {task_id}: {task[:36]}"
        sub_session = self.store.create_session(
            str(self.project_root) if self.project_root else "(subagent)", title=title)
        self._subagents.register(task_id=task_id, session_id=sub_session, description=task,
                                 tools=tools, mode=mode, group_id=group_id)
        grp = f", group '{group_id}'" if group_id else ""
        self._log_subagent(f"{G.BULLET} subagent {task_id} started ({mode}{grp}): {task[:80]}")
        self.run_worker(self._run_subagent(task_id, sub_session, task, tools),
                        group="subagents", exclusive=False)
        gnote = f" group='{group_id}'" if group_id else ""
        return (f"Task started: {task_id} (mode={mode}{gnote}). It runs in the background "
                "in its own session; keep working -- you'll get its result on a later turn "
                f"(or use check_task('{task_id}').")

    async def _run_subagent(self, task_id: str, session_id: str, task: str,
                            tools: list[str]) -> None:
        """Worker body: run the subagent to completion, record the result, and --
        depending on its delivery mode -- surface it back to the main agent."""
        situation = (
            "You are a focused SUBAGENT spawned by the main manager to handle one "
            "delegated task. Use ONLY your granted tools. When done, reply with a "
            "concise, factual summary of what you found or did -- that summary is your "
            "entire report back to the main agent, so make it self-contained and brief."
        )
        try:
            status, summary = await self.agent.run_subagent(
                session_id, task, tools, situation=situation)
        except Exception as e:
            status, summary = "error", f"{type(e).__name__}: {e}"
        dec = self._subagents.complete(task_id, status=status, summary=summary)
        first = (summary or "").strip().splitlines()[0] if summary else ""
        icon = G.OK if status == "completed" else G.ERR
        self._log_subagent(f"{icon} subagent {task_id} {status}: {first[:100]}")
        if dec.auto_trigger and not self._busy:
            self._run_turn("", synthetic=True)

    def _log_user(self, text: str, images: Optional[list[dict]] = None) -> None:
        """Render a user message as a bordered bubble that matches the input pill —
        the main background with a bright outline — so it reads as 'yours'. Any
        attached images are listed by name on a dim trailing line."""
        self._end_tool_group()
        body = Text(text, style=self.cc["fg"])
        if images:
            names = ", ".join(i.get("name", "image") for i in images)
            if text:
                body.append("\n")
            body.append(f"attached: {names}", style=self.cc["dim"])
        self._mount(Static(body, classes="msg-user", markup=False))

    # ── image attachments (pasted / dragged-in) ────────────────────────────
    def _attach_image_bytes(self, data: bytes, mime: str) -> bool:
        """Save clipboard image bytes as a pending attachment. Returns True on
        success (so the caller skips inserting text)."""
        att = attach.save_bytes(data, mime, self.session_id)
        if att is None:
            self._log(f"[{self.cc['tool']}]{G.WARN} couldn't read that image "
                      f"(empty or over {attach.MAX_BYTES // (1024 * 1024)} MB).[/]")
            return False
        self._attachments.append(att)
        self._refresh_attach_row()
        return True

    def _maybe_attach_paths(self, text: str) -> bool:
        """If ``text`` is one or more whitespace-separated image file paths (a
        drag-drop / file-manager copy), attach them and return True. Mixed or
        non-image text returns False so it pastes normally."""
        raw = (text or "").strip()
        if not raw:
            return False
        # Split on newlines first (multi-file drops), then fall back to the whole
        # line as a single (possibly space-containing, quoted) path.
        candidates = [c.strip().strip('"').strip("'") for c in raw.splitlines() if c.strip()]
        if not candidates:
            return False
        # Strip a file:// scheme some terminals prepend on drop.
        norm = [c[7:] if c.startswith("file://") else c for c in candidates]
        if not all(attach.is_image_path(c) for c in norm):
            return False
        added = 0
        for c in norm:
            att = attach.save_path(c, self.session_id)
            if att is not None:
                self._attachments.append(att)
                added += 1
        if added:
            self._refresh_attach_row()
        return added > 0

    def _refresh_attach_row(self) -> None:
        """Rebuild the chip row from ``self._attachments``; hide it when empty."""
        try:
            row = self.query_one("#attach-row", Horizontal)
        except Exception:
            return
        row.remove_children()
        if not self._attachments:
            row.display = False
            return
        row.display = True
        for att in self._attachments:
            label = Static(Text(att.get("name", "image"), style=self.cc["fg"]),
                           classes="attach-name", markup=False)
            x = Static("[x]", classes="attach-x", markup=False)
            x._att_id = att["id"]  # type: ignore[attr-defined]
            # Pass children at construction so they compose when the chip mounts
            # (Textual forbids mounting into a not-yet-mounted container).
            row.mount(Horizontal(label, x, classes="attach-chip"))

    @on(Click, ".attach-x")
    def _attach_remove(self, event: Click) -> None:
        aid = getattr(event.widget, "_att_id", None)
        if aid is None:
            return
        self._attachments = [a for a in self._attachments if a["id"] != aid]
        self._refresh_attach_row()

    def _clear_attachments(self) -> None:
        self._attachments = []
        self._refresh_attach_row()

    # ── expandable tool-call blocks (nested collapsibles, like the launcher) ──
    @staticmethod
    def _fmt_args(args) -> str:
        try:
            if isinstance(args, (dict, list)):
                return json.dumps(args, indent=2, ensure_ascii=False)
        except Exception:
            pass
        return str(args)

    def _preview(self, args) -> str:
        """A short one-line argument summary for the collapsed call title."""
        try:
            if isinstance(args, dict):
                parts = []
                for k, v in list(args.items())[:2]:
                    sv = str(v).replace("\n", " ")
                    parts.append(f"{k}={sv[:24] + '…' if len(sv) > 24 else sv}")
                s = ", ".join(parts)
            else:
                s = str(args).replace("\n", " ")
        except Exception:
            s = ""
        return s[:48] + "…" if len(s) > 48 else s

    def _args_code(self, args) -> str:
        """The call's arguments as a code block — if a call carries a command or a
        code/content field, show THAT prominently with any other args summarised."""
        if isinstance(args, dict):
            for k in ("command", "code", "content", "patch", "text", "script"):
                v = args.get(k)
                if isinstance(v, str) and v.strip():
                    extra = {kk: vv for kk, vv in args.items() if kk != k}
                    head = (json.dumps(extra, ensure_ascii=False) + "\n\n") if extra else ""
                    return head + v
        return self._fmt_args(args)

    def _build_inner(self, tool: str, args) -> tuple[Collapsible, Static]:
        """One call's collapsible: arguments code block + a result code block that
        gets filled in place when the tool returns. Returns (collapsible, result)."""
        c = self.cc
        call_code = Static(Text(self._args_code(args), style=c["fg"]),
                           classes="code-block", markup=False)
        result = Static(Text("running…", style=c["dim"]), classes="code-block", markup=False)
        inner = Collapsible(call_code, result,
                            title=f"{G.TOOL} {tool}  {self._preview(args)}",
                            collapsed=True, classes="tool-block")
        return inner, result

    def _add_tool_call(self, tool: str, args) -> None:
        """Add a call to the current group (creating the group on the first call).
        The group title tracks the count: '1 tool call' → 'N tool calls'."""
        inner, result = self._build_inner(tool, args)
        if self._tool_group is None:
            self._tool_n = 1
            self._tool_group = Collapsible(inner, title="1 tool call",
                                           collapsed=True, classes="tool-group")
            self._mount(self._tool_group)
        else:
            self._tool_n += 1
            self._tool_group.title = f"{self._tool_n} tool calls"
            try:
                self._tool_group.query_one(Collapsible.Contents).mount(inner)
            except Exception:
                # Group not fully mounted yet (rare) — queue into its contents list.
                self._tool_group._contents_list.append(inner)
            self._scroll_end()
        self._tool_pending.append({"tool": tool, "inner": inner, "result": result, "done": False})

    def _fill_tool_result(self, tool: str, text: str) -> None:
        """Fill the first still-open call of this name with its result + an ok/err mark."""
        entry = next((e for e in self._tool_pending
                      if e["tool"] == tool and not e["done"]), None)
        if entry is None:
            return
        entry["done"] = True
        out = text or ""
        lines = out.splitlines()
        head = lines[0] if lines else ""
        ok = not head.startswith(("Error", "Refused", "[exit 1"))
        shown = out[:4000] + ("\n… (truncated)" if len(out) > 4000 else "")
        try:
            entry["result"].update(Text(shown if shown.strip() else "(no output)",
                                        style=self.cc["fg"]))
        except Exception:
            pass
        mark = G.OK if ok else G.ERR
        extra = f"  (+{len(lines) - 1} lines)" if len(lines) > 1 else ""
        try:
            entry["inner"].title = f"{G.TOOL} {tool}  {mark}{extra}"
        except Exception:
            pass

    def _end_tool_group(self) -> None:
        """Close the current tool group so the next call starts a fresh one (called
        when an assistant reply, a user message, or an error interrupts the calls)."""
        self._tool_group = None
        self._tool_n = 0
        self._tool_pending = [e for e in self._tool_pending if not e["done"]]

    @work(exclusive=True, group="agent")
    async def _run_turn(self, text: str, images: Optional[list[dict]] = None,
                         synthetic: bool = False) -> None:
        assert self.agent is not None
        c = self.cc
        self._dismiss_banner()
        self._set_busy(True)

        async def on_event(ev: AgentEvent) -> None:
            if ev.kind == "assistant" and ev.text:
                self._log_assistant(ev.text)
            elif ev.kind == "tool_call":
                self._add_tool_call(ev.tool, ev.args or {})
            elif ev.kind == "tool_result":
                self._fill_tool_result(ev.tool, ev.text)
            elif ev.kind == "usage":
                u = ev.args or {}
                pin, pout = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
                if pin:
                    self._s_in += pin
                    self._ctx_tokens = pin       # latest prompt = current context size
                if pout:
                    self._s_out += pout
                self._update_hud()
            elif ev.kind == "error":
                self._end_tool_group()
                self._log(f"[{c['error']}]{G.ERR} {ev.text}[/]")
            elif ev.kind == "status":
                self._log(f"[{c['dim']}]{ev.text}[/]")

        try:
            situation = await self._build_situation()
            await self.agent.run_turn(self.session_id, text, on_event, situation=situation,
                                      images=images, synthetic=synthetic)
        except Exception as e:  # surface, never crash the UI
            self._log(f"[{c['error']}]{G.ERR} agent error: {type(e).__name__}: {e}[/]")
        finally:
            self._set_busy(False)
            self._after_turn_settle()

    # ── open-time PID / stale-instance manager ─────────────────────────────
    async def _scan_pids_on_open(self) -> None:
        """List every running webAgent server process and flag stale/zombie ones,
        offering (with permission) to remove them. Runs once on open."""
        import asyncio as _asyncio
        from .tools import server as srv
        procs = await _asyncio.to_thread(scan_webagent_processes)
        info = srv._read_pidinfo()
        tracked = int(info["pid"]) if info and info.get("pid") else None
        if not procs:
            self._log(f"[{self.cc['dim']}]{G.BULLET} no running webAgent server processes found.[/]")
            return
        health = await server_health()
        lines = ["webAgent server processes:"]
        stale: list[dict] = []
        for p in procs:
            pid, on, cmd = p["pid"], p["on_8080"], p["cmdline"]
            who = "tracked" if pid == tracked else ("port 8080" if on else "run.py")
            # Zombie: holds 8080 but doesn't serve /health. Orphan run.py: a leftover
            # launcher process — but ONLY when nothing is serving (a healthy server's
            # uvicorn-reload supervisor also runs run.py without holding the port).
            zombie = on and health != "running"
            orphan = (not on) and pid != tracked and health != "running"
            if zombie or orphan:
                stale.append(p)
                label = " — STALE (zombie on 8080)" if zombie else " — STALE (leftover run.py)"
            elif on and health == "running":
                label = " — serving"
            else:
                label = ""
            lines.append(f"  pid {pid}  [{who}]{label}  {cmd[:56]}")
        self._log_block("\n".join(lines))
        if stale:
            pids = [p["pid"] for p in stale]
            body = ("These look stale — holding port 8080 without serving /health, or a "
                    "leftover run.py from a crashed launch:\n\n"
                    + "\n".join(f"  • pid {p['pid']}  {p['cmdline'][:64]}" for p in stale)
                    + "\n\nTerminate them? (The healthy/serving instance is never touched.)")
            self._open_sidebar_confirm("Remove stale webAgent processes", body,
                                       "Terminate", lambda: self._terminate_pids(pids))

    def _terminate_pids(self, pids: list[int]) -> None:
        from .tools import server as srv
        ok, bad = [], []
        for pid in pids:
            (ok if srv._terminate(pid) else bad).append(pid)
        if ok:
            self._log(f"[{self.cc['secondary']}]{G.OK} terminated: {', '.join(map(str, ok))}[/]")
        if bad:
            self._log(f"[{self.cc['error']}]{G.ERR} could not terminate: {', '.join(map(str, bad))}[/]")

    # ── live web-app link: target, mutes, send, stream rendering ───────────
    def _sync_agent_target(self) -> None:
        """Push the connected target onto the agent so webapp_* tools can reach it."""
        t = self._webapp_target or {}
        self.agent.webapp_session_id = t.get("session_id", "") if not self._webagent_muted else ""
        self.agent.webapp_agent_id = t.get("agent_id", "")
        self.agent.webapp_agent_name = t.get("agent_name", "")

    def set_webapp_target(self, agent_id: str, agent_name: str,
                          session_id: str, session_title: str = "") -> None:
        """Set the active web-app target (agent + session). Does not connect by
        itself — unmute the WebAgent to go live."""
        self._webapp_target = {"agent_id": agent_id, "agent_name": agent_name,
                               "session_id": session_id, "session_title": session_title}
        self._sync_agent_target()
        self._log(f"[{self.cc['secondary']}]{G.OK} target set:[/] "
                  f"[{self.cc['dim']}]{agent_name} · session {session_id[:12]}[/]")

    def _ensure_webapp_stream(self) -> None:
        if not self._webapp_stream_on:
            self._webapp_stream_on = True
            self.run_worker(self._webapp.run_stream(self._on_webapp_event),
                            group="webappws", exclusive=True)

    def set_webagent_muted(self, muted: bool) -> None:
        """Toggle the WebAgent link. Muted (default) = Manager only, no app link.
        Unmuted = bridge the Manager to the target session (it can send/receive)."""
        if muted == self._webagent_muted:
            return
        self._webagent_muted = muted
        if muted:
            self._manager_muted = False
            self._sync_agent_target()
            self.workers.cancel_group(self, "webappws")
            self._webapp_stream_on = False
            self.run_worker(self._webapp.stop(), group="webappstop")
            self._log(f"[{self.cc['dim']}]{G.BULLET} WebAgent muted — back to the Manager only.[/]")
            self._refresh_status()
            return
        # Unmuting — need a target.
        if not self._webapp_target:
            self._webagent_muted = True
            self._log(f"[{self.cc['tool']}]{G.WARN} Pick an agent + session in Connect first.[/]")
            return
        self._sync_agent_target()
        self._ensure_webapp_stream()
        t = self._webapp_target
        self._log(f"[{self.cc['secondary']}]{G.OK} WebAgent live[/] "
                  f"[{self.cc['dim']}]— bridged to {t['agent_name']} (session {t['session_id'][:12]}).[/]")
        self._refresh_status()
        # Hand the Manager a readiness prompt so it acknowledges and can take over.
        readiness = (f"[system] You are now connected to the web app agent "
                     f"'{t['agent_name']}' (session {t['session_id']}). Use webapp_send to "
                     "talk to it on my behalf; its replies arrive in this transcript. "
                     "Acknowledge briefly that you're ready.")
        if self.provider.configured:
            self._run_turn(readiness)

    def set_manager_muted(self, muted: bool) -> None:
        """Toggle the Manager. Muted = my input goes straight to the app agent
        (normal web-app use); only meaningful while the WebAgent is live."""
        self._manager_muted = muted
        word = "muted — you're talking to the app agent directly" if muted else "unmuted"
        self._log(f"[{self.cc['dim']}]{G.BULLET} Manager {word}.[/]")
        self._refresh_status()

    @work(exclusive=True, group="agent")
    async def _send_to_webapp(self, text: str) -> None:
        """Manager-muted path: send my message straight to the connected app agent.
        The echo + reply render via the live stream."""
        self._dismiss_banner()
        t = self._webapp_target or {}
        try:
            await self._webapp.send(t.get("session_id", ""), text, agent_id=t.get("agent_id", ""))
        except WebAppError as e:
            self._log(f"[{self.cc['error']}]{G.ERR} {e}[/]")

    async def _on_webapp_event(self, ev: dict) -> None:
        """Render live web-app stream events for the connected target session into
        the shared transcript (sender-labelled)."""
        et = ev.get("type")
        if et == "subscribed":
            return
        if et == "_ws_status":
            if not ev.get("ok"):
                self._log(f"[{self.cc['dim']}]{G.BULLET} web-app stream dropped; reconnecting…[/]")
            return
        t = self._webapp_target
        if not t or ev.get("session_id") != t.get("session_id"):
            return  # the per-user WS delivers all sessions; only show the connected one
        if et == "user_message":
            self._flush_ws_bubble()
            self._mount(Static(Text("↦ " + (ev.get("content") or ""), style=self.cc["secondary"]),
                               classes="msg-webuser", markup=False))
        elif et == "stream":
            self._ws_append(ev.get("content") or "")
        elif et == "response":
            self._ws_finalize(ev.get("content") or self._ws_bubble_text)
        elif et == "tool_call":
            self._flush_ws_bubble()
            self._log(f"[{self.cc['dim']}]  {G.TOOL} web: {ev.get('tool', '?')}[/]")
        elif et == "error":
            self._flush_ws_bubble()
            self._log(f"[{self.cc['error']}]{G.ERR} web: {ev.get('message', 'error')}[/]")
        elif et == "interrupted":
            self._flush_ws_bubble()
            self._log(f"[{self.cc['dim']}]  {G.BULLET} web: turn interrupted/replaced[/]")

    def _ws_append(self, delta: str) -> None:
        if not delta:
            return
        self._dismiss_banner()
        name = (self._webapp_target or {}).get("agent_name", "app agent")
        if self._ws_bubble is None:
            self._ws_bubble_text = ""
            self._ws_bubble = Static(Text("", style=self.cc["fg"]), classes="msg-webagent", markup=False)
            self._mount(self._ws_bubble)
        self._ws_bubble_text += delta
        try:
            self._ws_bubble.update(Text(f"{name}: {self._ws_bubble_text}", style=self.cc["fg"]))
            self._scroll_end()
        except Exception:
            pass

    def _ws_finalize(self, full: str) -> None:
        name = (self._webapp_target or {}).get("agent_name", "app agent")
        text = full or self._ws_bubble_text
        if self._ws_bubble is not None:
            try:
                self._ws_bubble.update(Text(f"{name}: {text}", style=self.cc["fg"]))
            except Exception:
                pass
        elif text:
            self._mount(Static(Text(f"{name}: {text}", style=self.cc["fg"]),
                               classes="msg-webagent", markup=False))
        self._ws_bubble = None
        self._ws_bubble_text = ""
        self._scroll_end()

    def _flush_ws_bubble(self) -> None:
        """Finalize any in-progress streaming bubble before a new event type."""
        if self._ws_bubble is not None:
            self._ws_finalize(self._ws_bubble_text)

    async def on_unmount(self) -> None:
        try:
            await self._webapp.stop()
        except Exception:
            pass
        await self.llm.aclose()
        self.store.close()

    def action_clear_transcript(self) -> None:
        """Clear the transcript / log pane of all widgets (preserving the session)."""
        try:
            log = self.query_one("#log", VerticalScroll)
            log.remove_children()
        except Exception:
            pass


def run() -> int:
    ServerManagerApp().run()
    return 0

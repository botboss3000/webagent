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
import webbrowser
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Collapsible, Input, Select, Static

from .agent import AgentEvent, ServerManagerAgent
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
from .db import Store
from .env_probe import probe_machine, server_health
from .glyphs import EMOJI, G
from .llm import LLMClient
from .model_windows import MODEL_CONTEXT_BY_ID, MODEL_CONTEXT_WINDOWS
from .selfinfo import check_self_update, gather
from .notify import Notifier
from .watchdog import Watchdog, set_active_watchdog
from .palette import PRESETS, palette_from_theme
from .stage import AnimatedStage
from .theme_colors import chrome_colors
from .themes import CUSTOM_VAR_DEFAULTS, DEFAULT_THEME, THEME_LABELS, THEME_ORDER, build_themes


class PromptInput(Input):
    """Single-line prompt. Textual's Input maps Ctrl+A to 'cursor home'; we rebind
    it to select-all so Ctrl+A highlights everything in the field. Ctrl+C copy /
    Ctrl+V paste / Ctrl+X cut are inherited from Input."""

    BINDINGS = [Binding("ctrl+a", "select_all", "Select all", show=False)]


class SpinnerBar(Widget):
    """A one-row activity spinner (``-`` ``/`` ``|`` ``\\``) shown whenever the agent
    is busy, so the user can see it isn't frozen. Blank at rest, and the timer is
    paused when idle so it costs ~0% CPU. Colour comes from the live theme."""

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
        self.refresh()

    def render(self) -> Text:
        if not self._active:
            return Text("")
        cc = getattr(self.app, "cc", {})
        t = Text(no_wrap=True, overflow="crop")
        t.append(self.FRAMES[self._frame % len(self.FRAMES)],
                 style=f"bold {cc.get('accent', '#46d4ff')}")
        t.append(" working…", style=cc.get("dim", "#56657a"))
        return t


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


def _scene_rows(app) -> list[tuple[str, "Select"]]:
    """The theme/animation ('Scene') controls — one labelled Select per setting.
    Shared shape so every change routes through app.apply_setting (live + persisted)."""
    cfg = app.cfg

    def safe(v, allowed, default):
        return v if v in allowed else default

    pal_names = {"theme", *[p.name for p in PRESETS]}
    return [
        ("Banner", Select([("On", True), ("Off", False)],
                          value=bool(app._anim_on), allow_blank=False, id="set-banner")),
        ("Theme", Select([(THEME_LABELS.get(t, t), t) for t in THEME_ORDER],
                         value=safe(app.theme, set(THEME_ORDER), DEFAULT_THEME),
                         allow_blank=False, id="set-theme")),
        ("Animation", Select([(ANIM_LABELS[s], s) for s in ANIM_STYLES],
                             value=safe(cfg.anim_style, set(ANIM_STYLES), "plasma"),
                             allow_blank=False, id="set-anim")),
        ("Palette", Select([("Match theme", "theme")] + [(p.name, p.name) for p in PRESETS],
                           value=safe(cfg.anim_palette, pal_names, "theme"),
                           allow_blank=False, id="set-pal")),
        ("Speed", Select([("Slow", 0.5), ("Normal", 1.0), ("Fast", 2.0)],
                         value=safe(cfg.anim_speed, {0.5, 1.0, 2.0}, 1.0),
                         allow_blank=False, id="set-speed")),
        ("Intensity", Select([("Low", 0.6), ("Normal", 1.0), ("High", 1.5)],
                             value=safe(cfg.anim_intensity, {0.6, 1.0, 1.5}, 1.0),
                             allow_blank=False, id="set-int")),
        ("FPS", Select([("12", 12), ("20", 20), ("30", 30)],
                       value=safe(cfg.anim_fps, {12, 20, 30}, 20),
                       allow_blank=False, id="set-fps")),
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

    def compose(self) -> ComposeResult:
        # Custom chrome modelled on the launcher's chat screen: a header toolbar of
        # clickable CATEGORY buttons + a footer legend, both spanning the FULL width
        # above and below a middle body. The body splits into the chat column (left,
        # always visible) and a thin side panel (right) that appears only when a
        # category is opened — so opening a menu never hides the conversation.
        yield Horizontal(id="status")      # header: clickable category toolbar
        with Horizontal(id="body"):
            with Vertical(id="main"):      # the chat column (stays visible)
                self._anim = AnimatedStage(palette=self._build_palette(), style=self.cfg.anim_style,
                                           fps=self.cfg.anim_fps, speed=self.cfg.anim_speed,
                                           intensity=self.cfg.anim_intensity, show_logo=True)
                self._anim.id = "anim"
                self._anim.display = self._anim_on
                self._anim.set_idle(not self._anim_on)
                yield self._anim                   # animated logo banner
                yield VerticalScroll(id="log")     # transcript (mounted widgets)
                yield Static("", id="hud")         # session HUD (tokens / context gauge)
                # Action bar: activity spinner (left) + [Stop] / [Continue] text (far right).
                self._spinner = SpinnerBar(id="spinner")
                self._stop_btn = Static("[Stop]", classes="act-btn disabled", markup=False)
                self._stop_btn._btn_action = "stop"          # type: ignore[attr-defined]
                self._cont_btn = Static("[Continue]", classes="act-btn", markup=False)
                self._cont_btn._btn_action = "continue"      # type: ignore[attr-defined]
                yield Horizontal(self._spinner, self._stop_btn, self._cont_btn, id="actionbar")
                cta = Static(self._cta_label(), id="cta", markup=False)
                cta.display = self.project_root is None   # onboarding-only call-to-action
                yield cta
                yield PromptInput(placeholder="Ask the Server Manager…", id="prompt")
            panel = Vertical(id="side-panel")  # thin right menu; hidden until a category opens
            panel.display = False
            yield panel
        with Horizontal(id="footer"):
            yield Static("", id="hints")               # left: minimal legend
            yield Static(self._kbd_label(), id="kbd", markup=False)  # right: open-keyboard shortcut

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
        self._refresh_hints()
        self._refresh_kbd()
        self._refresh_status()
        self._update_hud()
        self.query_one("#prompt", Input).focus()
        # Keep the server dot live in managed mode (cheap localhost /health poll).
        self.set_interval(3.0, self._poll_server)
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
        return "\n".join([
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
        ])

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
        """A single transcript line carrying Rich console markup (theme colours)."""
        self._mount(Static(markup, classes="msg-line"))

    def _log_block(self, text: str) -> None:
        """Mount raw multi-line text (server logs / diagnostics) WITHOUT markup
        parsing — they contain brackets and tracebacks that aren't Rich markup."""
        self._mount(Static(Text(text, style=self.cc["dim"]), classes="msg-block", markup=False))

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
        CATEGORY button per group — Admin · Scene · App — each of which opens a thin
        right-side panel of buttons. The last item is the live SERVER STATUS itself
        (live / stopped / checking); clicking it opens the server panel (Start /
        Restart / Kill). No title or model text."""
        c = self.cc
        try:
            bar = self.query_one("#status", Horizontal)
        except Exception:
            return
        bar.remove_children()
        self._dot = None

        def cat(label: str, kind: str, action: str) -> None:
            # The pill of the open category gets the .hdr-on highlight class.
            w = self._add_hdr(bar, Text(label, style=f"bold {c['accent']}"), action)
            if self._panel_kind == kind:
                w.add_class("hdr-on")

        # Far-left = the write-gate as a ONE-WORD toggle (read → write → auto on click).
        # Coloured text shows the current mode; no inverted fill.
        mode = "auto" if self.cfg.autonomous else "write" if self.cfg.writes_enabled else "read"
        mcol = {"read": c["dim"], "write": c["secondary"], "auto": c["tool"]}[mode]
        self._add_hdr(bar, Text(mode, style=f"bold {mcol}"), "mode_cycle")

        cat("Admin", "admin", "panel_admin")
        cat("Scene", "scene", "panel_scene")
        cat("App", "app", "panel_app")
        # Last header item = the live server STATUS (spins while loading); click → server panel.
        if self.project_root:
            self._dot = ServerStatusWidget()
            if self._panel_kind == "server":
                self._dot.add_class("hdr-on")
            bar.mount(self._dot)
            self._dot.set_state(self._server_state)
        else:
            self._add_hdr(bar, Text("onboarding", style=c["secondary"]), None)

    def _refresh_hints(self) -> None:
        """Footer-left legend. Esc opens/closes the side menu (the Ctrl+ editing hints
        were removed); the open-keyboard shortcut lives on the footer-RIGHT (#kbd)."""
        c = self.cc
        t = Text(no_wrap=True, overflow="crop")
        t.append("Esc ", style=c["accent"])
        t.append("menu", style=c["dim"])
        try:
            self.query_one("#hints", Static).update(t)
        except Exception:
            pass

    def _refresh_kbd(self) -> None:
        try:
            self.query_one("#kbd", Static).update(self._kbd_label())
        except Exception:
            pass

    def _kbd_label(self):
        c = self.cc
        t = Text(no_wrap=True, overflow="crop")
        t.append(("⌨ " if EMOJI else ""), style=c["accent"])
        t.append("Keyboard", style=f"bold {c['accent']}")
        return t

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
        t.append(f"{self._fmt_tokens(self._s_in)} in / {self._fmt_tokens(self._s_out)} out",
                 style=f"bold {c['success']}")
        t.append(" | ", style=c["dim"])
        t.append_text(self._ctx_gauge())
        try:
            self.query_one("#hud", Static).update(t)
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

    @on(Click, "#kbd")
    def _on_kbd_click(self, event: Click) -> None:
        self.action_open_keyboard()

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
        """Transcript sink for the watchdog/notifier. Plain text (markup off) — the
        messages carry tracebacks/brackets that aren't Rich markup."""
        try:
            self._mount(Static(Text(text, style=self.cc["tool"]), classes="msg-line", markup=False))
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
            self._open_panel("app")

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
        if kind is None:
            panel.display = False
            return
        await panel.mount(*self._panel_widgets(kind))
        panel.display = True

    def _panel_btn(self, label: str, action: str, active: bool = False) -> Static:
        b = Static(label, classes="panel-btn panel-btn-active" if active else "panel-btn",
                   markup=False)
        b._btn_action = action  # type: ignore[attr-defined]
        return b

    def _panel_widgets(self, kind: str) -> list[Widget]:
        """Build the widgets for one category panel: a TITLE then its buttons (Admin /
        Server), the theme/animation Selects (Scene), or the App controls (AI key field,
        Read/Write/Auto, Open Browser)."""
        c = self.cc
        title = {"admin": "ADMIN", "scene": "SCENE", "app": "APP",
                 "server": "SERVER"}.get(kind, kind.upper())
        out: list[Widget] = [Static(Text(title, style=f"bold {c['accent']}"), id="panel-title")]
        if kind == "scene":
            for label, sel in _scene_rows(self):
                out.append(Horizontal(Static(label, classes="set-label"), sel, classes="set-row"))
            return out
        if kind == "app":
            # ── AI provider / key block ──────────────────────────────────
            prov_name = self.cfg.provider or provider_name_for_base(self.provider.base_url)
            out.append(Static(Text("Provider", style=c["dim"]), classes="panel-sub"))
            out.append(Select([(n, n) for n, _, _ in PROVIDER_PRESETS],
                              value=prov_name if prov_name in {n for n, _, _ in PROVIDER_PRESETS} else "Custom",
                              allow_blank=False, id="provider-select"))
            out.append(Static(Text("AI key", style=c["dim"]), classes="panel-sub"))
            out.append(Input(value="", password=True, id="key-input",
                             placeholder="update key…" if self.provider.configured else "paste API key…"))
            out.append(Static(Text("Base URL", style=c["dim"]), classes="panel-sub"))
            out.append(Input(value=self.provider.base_url, id="base-input",
                             placeholder="https://…/v1"))
            out.append(Static(Text("Model", style=c["dim"]), classes="panel-sub"))
            out.append(Input(value=self.provider.model, id="model-input", placeholder="model id"))
            out.append(Horizontal(self._panel_btn("[Save]", "key_save"),
                                  self._panel_btn("[Clear]", "key_clear"),
                                  classes="panel-row"))
            keynote = (f"✓ {self.provider.model}" if self.provider.configured else "not configured")
            kcol = c["secondary"] if self.provider.configured else c["tool"]
            out.append(Static(Text(keynote, style=kcol), id="key-status", classes="panel-sub"))
            # ── write-gate ───────────────────────────────────────────────
            cur = "auto" if self.cfg.autonomous else "write" if self.cfg.writes_enabled else "read"
            out.append(Static(Text("Mode", style=c["dim"]), classes="panel-sub"))
            out.append(self._panel_btn("[Read-only]", "mode_read", cur == "read"))
            out.append(self._panel_btn("[Write]", "mode_write", cur == "write"))
            out.append(self._panel_btn("[Autonomous]", "mode_auto", cur == "auto"))
            out.append(self._panel_btn("[Open Browser]", "open_browser"))
            return out
        specs = {
            "admin": [("[Commands]", "help"), ("[Update]", "admin_update"),
                      ("[Install]", "install"), ("[Uninstall]", "admin_uninstall"),
                      ("[Diagnostics]", "diagnostics"), ("[Logs]", "server_logs")],
            "server": [("[Start]", "server_start"), ("[Restart]", "server_restart"),
                       ("[Kill]", "server_stop")],
        }.get(kind, [])
        for label, action in specs:
            out.append(self._panel_btn(label, action))
        return out

    def action_panel_admin(self) -> None:
        self._open_panel("admin")

    def action_panel_scene(self) -> None:
        self._open_panel("scene")

    def action_panel_app(self) -> None:
        self._open_panel("app")

    def action_panel_server(self) -> None:
        if self.project_root is None:
            return
        self._open_panel("server")

    # ── panel interactions: button clicks, click-outside, settings, AI key ──
    # Buttons whose action should NOT close the panel (they edit it in place).
    _KEEP_OPEN = {"key_save", "key_clear"}

    @on(Click, ".panel-btn")
    def _on_panel_btn(self, event: Click) -> None:
        action = getattr(event.widget, "_btn_action", None)
        if action in self._KEEP_OPEN:
            getattr(self, f"action_{action}")()
            return
        self._close_panel()
        if action:
            fn = getattr(self, f"action_{action}", None)
            if fn is not None:
                fn()

    def on_click(self, event: Click) -> None:
        # Click anywhere OUTSIDE the open panel (and not on a header/panel button) closes it.
        if not self._panel_open():
            return
        node = event.widget
        while node is not None:
            if getattr(node, "id", None) == "side-panel":
                return
            cls = getattr(node, "classes", ())
            if "hdr-btn" in cls or "panel-btn" in cls:
                return
            node = node.parent
        self._close_panel()

    @on(Select.Changed)
    def _on_setting_change(self, event: Select.Changed) -> None:
        if event.value is Select.BLANK:
            return
        if event.select.id == "provider-select":
            # Fill the Base URL + Model fields with the chosen provider's defaults so
            # the URL matches the key (Custom leaves them for manual entry). Save persists.
            self._apply_provider_preset(str(event.value))
            return
        # Scene controls apply live; the panel stays open so several can be tweaked.
        self.apply_setting(event.select.id, event.value)

    def _apply_provider_preset(self, name: str) -> None:
        preset = next((p for p in PROVIDER_PRESETS if p[0] == name), None)
        if preset is None or name == "Custom":
            return
        _, base, model = preset
        try:
            self.query_one("#base-input", Input).value = base
            self.query_one("#model-input", Input).value = model
        except Exception:
            pass

    @on(Input.Submitted, "#key-input")
    def _key_enter(self, event: Input.Submitted) -> None:
        self.action_key_save()   # Enter in the key field is a shortcut for [Save]

    def action_key_save(self) -> None:
        """Persist the App-panel provider/key/model as an explicit UI override (wins
        over the repo's provider.json/.env) and rebuild the LLM client."""
        try:
            panel = self.query_one("#side-panel", Vertical)
            key = panel.query_one("#key-input", Input).value.strip()
            base = panel.query_one("#base-input", Input).value.strip()
            model = panel.query_one("#model-input", Input).value.strip()
            prov = panel.query_one("#provider-select", Select).value
        except Exception:
            return
        if key:
            self.cfg.api_key = key
        self.cfg.base_url = base
        self.cfg.model = model
        self.cfg.provider = str(prov) if prov is not Select.BLANK else ""
        self.cfg.provider_override = True
        self.cfg.save()
        self._apply_provider()
        self._refresh_status()
        if self.provider.configured:
            self._log(f"[{self.cc['secondary']}]{G.OK} AI settings saved[/] "
                      f"[{self.cc['dim']}]— {self.cfg.provider or 'custom'} · {self.provider.model}[/]")
        else:
            self._log(f"[{self.cc['tool']}]{G.WARN} saved, but no key is set yet — "
                      f"paste a key and Save again.[/]")
        self._rebuild_panel()

    def action_key_clear(self) -> None:
        """Forget the UI-set key/provider/override so resolution falls back to the
        repo/env (or built-in defaults) again."""
        self.cfg.api_key = ""
        self.cfg.base_url = ""
        self.cfg.model = ""
        self.cfg.provider = ""
        self.cfg.provider_override = False
        self.cfg.save()
        self._apply_provider()
        self._refresh_status()
        self._log(f"[{self.cc['dim']}]{G.BULLET} AI key cleared "
                  f"(falling back to the repo/env key, if any).[/]")
        self._rebuild_panel()

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
        self.push_screen(ConfirmModal("Install webAgent", body, "Install now"),
                         self._after_install_confirm)

    def _after_install_confirm(self, ok: bool | None) -> None:
        if ok:
            self.action_get_started()

    # ── footer-right: open the soft keyboard ──────────────────────────────
    def action_open_keyboard(self) -> None:
        """Focus the prompt input — the standard trigger that raises the soft keyboard
        on desktop and most platforms. On Android/Termux the OS does NOT let a terminal
        program force the soft keyboard up (only the user can, via the Termux drawer's
        'KEYBOARD' toggle or Vol-Up+K), so there we focus the input and flash the hint."""
        try:
            self.query_one("#prompt", Input).focus()
        except Exception:
            pass
        if self.facts.is_termux:
            self._log(f"[{self.cc['dim']}]tip: if the keyboard didn't appear, open it from the "
                      f"Termux left-edge drawer ▸ KEYBOARD (Android blocks apps from raising it).[/]")

    def action_open_browser(self) -> None:
        url = "http://localhost:8080/index.html"
        try:
            webbrowser.open(url)
            self._log(f"[{self.cc['dim']}]opened {url} in your browser[/]")
        except Exception as e:
            self._log(f"[{self.cc['error']}]{G.ERR} couldn't open a browser: {e}[/]")

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
        self.run_worker(self._show_logs(), group="diag", exclusive=True)

    async def _show_logs(self) -> None:
        from .tools import server as srv
        msg = await srv.server_logs(self._server_ctx(), lines=60)
        self._log_block(msg)

    def action_diagnostics(self) -> None:
        if self.project_root is None:
            self._log(f"[{self.cc['dim']}]no diagnostics in onboarding mode (link a checkout first)[/]")
            return
        self.run_worker(self._show_diagnostics(), group="diag", exclusive=True)

    async def _show_diagnostics(self) -> None:
        from .tools import diagnostics as diag
        msg = await diag.read_diagnostics(self._server_ctx(), limit=20)
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
        line("Admin", "Commands · Update · Install · Uninstall · Diagnostics · Logs")
        line("Scene", "theme, animation, palette, speed, banner on/off")
        line("App", "AI provider + key (Save/Clear), Read/Write/Auto, Open Browser")
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
            "rm -rf ~/webagent ~/.local/share/webagent-tui",
            "pip uninstall -y webagent-tui",
        ]
        desktop = [
            "# Desktop: run from source / the packaged app:",
            "run.bat            (Windows, from the webagent_tui folder)",
            "webagent-tui.exe   (frozen build)",
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
        self.push_screen(ConfirmModal("Update webAgent", self._update_info(), "Update now"),
                         self._after_update_confirm)

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
            "  • the Python package (pip uninstall webagent-tui)",
            "",
            "This cannot be undone. The manager closes immediately afterward.",
        ])

    def action_admin_uninstall(self) -> None:
        self.push_screen(ConfirmModal("Uninstall webAgent", self._uninstall_info(),
                                      "Remove everything"),
                         self._after_uninstall_confirm)

    def _after_uninstall_confirm(self, ok: bool | None) -> None:
        if ok:
            self.run_worker(self._do_uninstall(), group="admin", exclusive=True)

    async def _do_uninstall(self) -> None:
        from .tools import manage
        msg = await manage.uninstall(self._admin_ctx())
        self._log_block(msg)
        if self.facts.is_termux:
            self.set_timer(2.0, self.exit)

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
        self._refresh_hints()
        self._refresh_kbd()
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

    @on(Input.Submitted, "#prompt")
    def _submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        if not self.provider.configured:
            self._log(f"[{self.cc['tool']}]{G.WARN} No AI key configured.[/] "
                      "Set LLM_API_KEY (the app key), or link a repo that has one.")
            return
        self._log_user(text)
        self._run_turn(text)

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

    def _log_user(self, text: str) -> None:
        """Render a user message as a bordered bubble that matches the input pill —
        the main background with a bright outline — so it reads as 'yours'."""
        self._end_tool_group()
        self._mount(Static(Text(text, style=self.cc["fg"]), classes="msg-user", markup=False))

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
    async def _run_turn(self, text: str) -> None:
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
            await self.agent.run_turn(self.session_id, text, on_event, situation=situation)
        except Exception as e:  # surface, never crash the UI
            self._log(f"[{c['error']}]{G.ERR} agent error: {type(e).__name__}: {e}[/]")
        finally:
            self._set_busy(False)

    async def on_unmount(self) -> None:
        await self.llm.aclose()
        self.store.close()


def run() -> int:
    ServerManagerApp().run()
    return 0

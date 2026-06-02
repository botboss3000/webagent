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
from textual.containers import Horizontal, Vertical
from textual.events import Click
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, RichLog, Select, Static

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
from .palette import PRESETS, palette_from_theme
from .stage import AnimatedStage
from .theme_colors import chrome_colors
from .themes import CUSTOM_VAR_DEFAULTS, DEFAULT_THEME, THEME_LABELS, THEME_ORDER, build_themes


class PromptInput(Input):
    """Single-line prompt. Textual's Input maps Ctrl+A to 'cursor home'; we rebind
    it to select-all so Ctrl+A highlights everything in the field. Ctrl+C copy /
    Ctrl+V paste / Ctrl+X cut are inherited from Input."""

    BINDINGS = [Binding("ctrl+a", "select_all", "Select all", show=False)]


class WalkerBar(Widget):
    """A one-row strip where a tiny ascii guy reacts to the agent loop:
    idle (blank) · walk (thinking) · work (tool running) · cheer (reply) ·
    trip (error). Animates only while active, so it costs nothing at rest.
    Colours come from the app's live theme (``app.cc``)."""

    DEFAULT_CSS = "WalkerBar { height: 1; width: 100%; padding: 0 2; }"

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)
        self._state = "idle"
        self._pos = 0
        self._frame = 0
        self._timer = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(0.12, self._tick, pause=True)  # ~8 fps, paused at rest

    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        self._frame = 0
        if self._timer is not None:
            self._timer.pause() if state == "idle" else self._timer.resume()
        self.refresh()

    def _tick(self) -> None:
        self._frame += 1
        if self._state == "walk":
            self._pos += 1
        self.refresh()

    def render(self) -> Text:
        w = max(6, self.size.width)
        if self._state == "idle":
            return Text(" " * w)
        cc = getattr(self.app, "cc", {})
        green = cc.get("success", "#7be06a")
        amber = cc.get("tool", "#ff9d2f")
        red = cc.get("error", "#ff5f56")
        if self._state == "walk":
            sprite = "🚶" if EMOJI else ("o/" if self._frame % 2 == 0 else "o\\")
            color = green
        elif self._state == "work":
            sprite = "🔧" if EMOJI else "o" + "|/-\\"[self._frame % 4]
            color = amber
        elif self._state == "cheer":
            sprite = "🙌" if EMOJI else "\\o/"
            color = green
        else:  # trip
            sprite = "💥" if EMOJI else "x_"
            color = red
        pos = self._pos % max(1, w - 3)
        line = Text(no_wrap=True, overflow="crop")
        if pos:
            line.append(" " * pos)
        line.append(sprite, style=f"bold {color}")
        tail = w - pos - len(sprite)
        if tail > 0:
            line.append(" " * tail)
        return line


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
        self._dot = None             # the server-status widget, updated in place by the poll
        self._anim = None            # the animated logo banner
        self._anim_on = self.cfg.anim_enabled
        self._walker = None          # the loop-reactive ascii walker (above the input)
        self._s_in = 0               # session token accumulators (HUD)
        self._s_out = 0
        self._ctx_tokens = 0         # latest prompt size = current context usage
        self._panel_kind = None      # which side-panel category is open (None = closed)

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
                yield RichLog(id="log", wrap=True, markup=True, highlight=False)
                yield Static("", id="hud")         # session HUD (tokens / context gauge)
                self._walker = WalkerBar(id="walker")
                yield self._walker                 # loop-reactive ascii walker
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
        log = self.query_one("#log", RichLog)
        if self.project_root:
            log.write(f"[b {c['primary']}]{G.ADMIN} webAgent Server Manager[/] "
                      f"[{c['dim']}]— managing your checkout[/]\n")
            log.write(f"[{c['dim']}]project:[/] {self.project_root}")
            log.write(self._host_line())
            srv = (f"[{c['secondary']}]running[/] at http://localhost:8080"
                   if server_status == "running" else f"{server_status}")
            log.write(f"[{c['dim']}]server:[/] {srv}")
            if self.provider.configured:
                log.write(f"[{c['dim']}]model:[/] {self.provider.model}")
            else:
                log.write(f"[{c['tool']}]{G.WARN} No AI key.[/] Set one to enable the agent.")
            log.write(f"\n[{c['dim']}]Ask me to check status, diagnose an issue, change code, "
                      f"run it, or manage git.[/]")
        else:
            log.write(f"[b {c['primary']}]{G.ADMIN} webAgent Server Manager[/] "
                      f"[{c['dim']}]— let's get you set up[/]\n")
            log.write(self._host_line())
            if self.provider.configured:
                log.write(f"[{c['dim']}]model:[/] {self.provider.model}")
            else:
                log.write(f"[{c['tool']}]{G.WARN} No AI key configured yet.[/] "
                          "Set the app key (LLM_API_KEY) to power onboarding.")
            log.write(f"[{c['dim']}]No webAgent repo is linked yet. I can:[/]")
            log.write(f"  {G.BULLET} install webAgent for you (recommended: {self._recommended_install_path()})")
            log.write(f"  {G.BULLET} link an existing copy — tell me its folder and I'll manage it")
            log.write(f"  {G.BULLET} tell you about webAgent, or help with general questions")
            log.write(f"\n[{c['accent']}]{G.BULLET} New here? Tap [b]Click here to get started[/] "
                      f"below and I'll install and set everything up.[/]")
        log.write(self._tip_line())

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

    # ── theme-aware Rich coloring ─────────────────────────────────────────
    def _log(self, markup: str) -> None:
        self.query_one("#log", RichLog).write(markup)

    def _log_block(self, text: str) -> None:
        """Write raw multi-line text (server logs / diagnostics) WITHOUT markup
        parsing — they contain brackets and tracebacks that aren't Rich markup."""
        try:
            self.query_one("#log", RichLog).write(Text(text, style=self.cc["dim"]))
        except Exception:
            pass

    # ── custom chrome: status bar (header) + hint bar (footer) ────────────
    def _server_dot(self) -> tuple[str, str]:
        c = self.cc
        st = self._server_state
        if st == "running":
            return f"{G.DOT_LIVE} live", c["success"]
        if st == "stopped":
            return f"{G.DOT_DEAD} stopped", c["error"]
        if st == "n/a":
            return "", c["dim"]
        return f"{G.DOT_WARN} checking", c["tool"]

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

        cat("Admin", "admin", "panel_admin")
        cat("Scene", "scene", "panel_scene")
        cat("App", "app", "panel_app")
        # Last header item = the server STATUS (not the word "Server"); click → server panel.
        if self.project_root:
            dot, col = self._server_dot()
            self._dot = self._add_hdr(bar, Text(dot or "checking", style=f"bold {col}"),
                                      "panel_server")
            if self._panel_kind == "server":
                self._dot.add_class("hdr-on")
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
        cells = 12
        filled = int(round(frac * cells))
        col = c["error"] if frac >= 0.85 else c["tool"] if frac >= 0.6 else c["success"]
        g.append("ctx [", style=c["dim"])
        g.append("#" * filled, style=col)
        g.append("." * (cells - filled), style=c["dim"])
        g.append("] ", style=c["dim"])
        g.append(f"{self._fmt_tokens(used)}/{self._fmt_tokens(mx)} ", style=c["dim"])
        g.append(f"{int(frac * 100)}%", style=col)
        return g

    def _update_hud(self) -> None:
        c = self.cc
        t = Text(no_wrap=True, overflow="crop")
        t.append("session ", style=c["dim"])
        t.append(f"{self._fmt_tokens(self._s_in)} in / {self._fmt_tokens(self._s_out)} out",
                 style=f"bold {c['success']}")
        t.append("   |   ", style=c["dim"])
        t.append_text(self._ctx_gauge())
        try:
            self.query_one("#hud", Static).update(t)
        except Exception:
            pass

    async def _poll_server(self) -> None:
        """Poll server health. When running/stopped flips, rebuild the toolbar so
        the Start↔Stop button switches; otherwise just refresh the dot in place."""
        new = await server_health() if self.project_root else "n/a"
        changed = new != self._server_state
        self._server_state = new
        if changed:
            self._refresh_status()
        elif self._dot is not None:
            dot, col = self._server_dot()
            try:
                self._dot.update(Text(dot or "checking", style=f"bold {col}"))
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
        self._log(f"\n[b {self.cc['secondary']}]{G.USER} ›[/] Click here to get started")
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
        msg = await fn(self._server_ctx())
        self._log(f"[{self.cc['dim']}]{msg}[/]")
        self._server_state = await server_health() if self.project_root else "n/a"
        self._refresh_status()

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
            "admin": [("[Update]", "admin_update"), ("[Install]", "install"),
                      ("[Uninstall]", "admin_uninstall"), ("[Diagnostics]", "diagnostics"),
                      ("[Logs]", "server_logs")],
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
        self._log(f"\n[b {self.cc['secondary']}]{G.USER} ›[/] {text}")
        self._run_turn(text)

    def _walk(self, state: str) -> None:
        if self._walker is not None:
            self._walker.set_state(state)

    def _rest_walker_soon(self) -> None:
        """Let the final pose (cheer/trip) linger briefly, then go idle."""
        def _rest() -> None:
            if self._walker is not None:
                self._walker.set_state("idle")
        try:
            self.set_timer(0.9, _rest)
        except Exception:
            _rest()

    @work(exclusive=True, group="agent")
    async def _run_turn(self, text: str) -> None:
        assert self.agent is not None
        c = self.cc
        self._walk("walk")

        async def on_event(ev: AgentEvent) -> None:
            if ev.kind == "assistant" and ev.text:
                self._log(f"[{c['fg']}]{G.BOT} {ev.text}[/]")
                self._walk("walk")
            elif ev.kind == "tool_call":
                args = json.dumps(ev.args or {}, ensure_ascii=False)
                self._log(f"[{c['tool']}]{G.TOOL} {ev.tool}[/] [{c['dim']}]{args[:160]}[/]")
                self._walk("work")
            elif ev.kind == "tool_result":
                snippet = ev.text.strip().splitlines()
                head = snippet[0] if snippet else ""
                ok = not head.startswith(("Error", "Refused", "[exit 1"))
                extra = f" (+{len(snippet) - 1} lines)" if len(snippet) > 1 else ""
                mark = G.OK if ok else G.WARN
                self._log(f"[{c['dim']}]{mark} {head[:200]}{extra}[/]")
                self._walk("walk")
            elif ev.kind == "final":
                self._walk("cheer")
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
                self._log(f"[{c['error']}]{G.ERR} {ev.text}[/]")
                self._walk("trip")
            elif ev.kind == "status":
                self._log(f"[{c['dim']}]{ev.text}[/]")

        try:
            situation = await self._build_situation()
            await self.agent.run_turn(self.session_id, text, on_event, situation=situation)
        except Exception as e:  # surface, never crash the UI
            self._log(f"[{c['error']}]{G.ERR} agent error: {type(e).__name__}: {e}[/]")
            self._walk("trip")
        finally:
            self._rest_walker_soon()

    async def on_unmount(self) -> None:
        await self.llm.aclose()
        self.store.close()


def run() -> int:
    ServerManagerApp().run()
    return 0

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
from .config import ProviderConfig, TuiConfig, _looks_like_project, db_path, resolve_provider
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


class SettingsModal(ModalScreen):
    """Theme & animation picker. Each change applies live and persists. Esc closes."""

    BINDINGS = [Binding("escape", "close", "Close")]

    def compose(self) -> ComposeResult:
        app = self.app
        cfg = app.cfg

        def safe(v, allowed, default):
            return v if v in allowed else default

        pal_names = {"theme", *[p.name for p in PRESETS]}
        rows = [
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
        with Vertical(id="settings-panel"):
            yield Static("Theme & Animation  —  applies live · Esc to close", id="settings-title")
            for label, sel in rows:
                with Horizontal(classes="set-row"):
                    yield Static(label, classes="set-label")
                    yield sel

    @on(Select.Changed)
    def _on_change(self, event: Select.Changed) -> None:
        if event.value is Select.BLANK:
            return
        self.app.apply_setting(event.select.id, event.value)

    def action_close(self) -> None:
        self.dismiss()


class ServerManagerApp(App):
    CSS_PATH = "styles.tcss"
    TITLE = "webAgent Server Manager"

    # Esc exits; the editing keys (Ctrl+A/C/V) are handled by the focused input.
    # Theme stays on Ctrl+T (not advertised). priority=True so Esc/theme fire even
    # while the input is focused.
    BINDINGS = [
        Binding("escape", "exit", "Exit", priority=True),
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
        # Custom chrome modelled on the launcher's chat screen: a Rich-drawn
        # status bar (server dot + mode + writes + model) instead of the stock
        # Header, and a clickable hint-pill bar instead of the stock Footer.
        yield Horizontal(id="status")      # clickable control toolbar
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
        yield Static("", id="hints")       # editing-shortcut legend

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

    def _mode_label(self) -> Text:
        """The write-gate button — shows the CURRENT mode only; clicking cycles
        Read → Write → Auto → Read."""
        c = self.cc
        if self.cfg.autonomous:
            return Text("[Auto]", style=f"bold {c['tool']}")
        if self.cfg.writes_enabled:
            return Text("[Write]", style=f"bold {c['secondary']}")
        return Text("[Read]", style=f"bold {c['dim']}")

    def _add_hdr(self, bar: Horizontal, content, action: str | None) -> Static:
        btn = Static(content, classes="hdr-btn" if action else "hdr-note", markup=False)
        if action:
            btn._btn_action = action  # type: ignore[attr-defined]
        bar.mount(btn)
        return btn

    def _refresh_status(self) -> None:
        """(Re)build the header toolbar (replaces the stock Header): a write-gate
        button that cycles Read/Write/Auto and, in managed mode, clickable
        Browser / Restart / Stop plus the live server dot. No title or model text."""
        c = self.cc
        try:
            bar = self.query_one("#status", Horizontal)
        except Exception:
            return
        bar.remove_children()
        self._dot = None
        self._add_hdr(bar, self._mode_label(), "cycle_mode")
        self._add_hdr(bar, "[Theme]", "open_settings")
        if self.project_root:
            self._add_hdr(bar, "[Browser]", "open_browser")
            # State-aware: a running server can be restarted/stopped; a stopped one started.
            if self._server_state == "running":
                self._add_hdr(bar, "[Restart]", "server_restart")
                self._add_hdr(bar, "[Stop]", "server_stop")
            else:
                self._add_hdr(bar, "[Start]", "server_start")
            self._add_hdr(bar, "[Logs]", "server_logs")
            self._add_hdr(bar, "[Diagnostics]", "diagnostics")
            dot, col = self._server_dot()
            self._dot = self._add_hdr(bar, Text(dot or "checking", style=col), None)
        else:
            self._add_hdr(bar, Text("onboarding", style=c["secondary"]), None)

    def _refresh_hints(self) -> None:
        """Footer legend (replaces the stock Footer): the editing / exit shortcuts."""
        c = self.cc
        t = Text(no_wrap=True, overflow="crop")
        for i, (key, what) in enumerate((("Esc", "exit"), ("Ctrl+A", "select all"),
                                         ("Ctrl+C", "copy"), ("Ctrl+V", "paste"))):
            if i:
                t.append(f"   {G.SEP}   ", style=c["dim"])
            t.append(key + " ", style=c["accent"])
            t.append(what, style=c["dim"])
        if self.facts.is_termux:
            t.append(f"   {G.SEP}   ", style=c["dim"])
            t.append("Vol+ then K ", style=c["accent"])
            t.append("toggles keyboard", style=c["dim"])
        try:
            self.query_one("#hints", Static).update(t)
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
                self._dot.update(Text(dot or "checking", style=col))
            except Exception:
                pass

    @on(Click, ".hdr-btn")
    def _on_hdr_click(self, event: Click) -> None:
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
        self.exit()

    def action_cycle_mode(self) -> None:
        """Cycle the agent's write gate: read-only → writes → autonomous → …"""
        if self.cfg.autonomous:
            self.cfg.autonomous = False
            self.cfg.writes_enabled = False
        elif self.cfg.writes_enabled:
            self.cfg.autonomous = True
        else:
            self.cfg.writes_enabled = True
        self.cfg.save()
        self._refresh_status()
        mode = ("autonomous" if self.cfg.autonomous else
                "writes" if self.cfg.writes_enabled else "read-only")
        self._log(f"[{self.cc['secondary']}]{G.BULLET} mode: {mode}[/]")

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

    # ── theme & animation picker (settings modal) ─────────────────────────
    def action_open_settings(self) -> None:
        self.push_screen(SettingsModal())

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

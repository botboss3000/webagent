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
from textual.containers import Horizontal
from textual.events import Click
from textual.widgets import Input, RichLog, Static

from .agent import AgentEvent, ServerManagerAgent
from .ascii_anim import PLASMA
from .config import ProviderConfig, TuiConfig, _looks_like_project, db_path, resolve_provider
from .db import Store
from .env_probe import probe_machine, server_health
from .glyphs import G
from .llm import LLMClient
from .selfinfo import check_self_update, gather
from .palette import palette_from_theme
from .stage import AnimatedStage
from .theme_colors import chrome_colors
from .themes import CUSTOM_VAR_DEFAULTS, DEFAULT_THEME, THEME_LABELS, THEME_ORDER, build_themes


class PromptInput(Input):
    """Single-line prompt. Textual's Input maps Ctrl+A to 'cursor home'; we rebind
    it to select-all so Ctrl+A highlights everything in the field. Ctrl+C copy /
    Ctrl+V paste / Ctrl+X cut are inherited from Input."""

    BINDINGS = [Binding("ctrl+a", "select_all", "Select all", show=False)]


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
        self._anim = AnimatedStage(palette=palette_from_theme(self), style=PLASMA,
                                   fps=20, show_logo=True)
        self._anim.id = "anim"
        self._anim.display = self._anim_on
        self._anim.set_idle(not self._anim_on)
        yield self._anim                   # animated logo banner
        yield RichLog(id="log", wrap=True, markup=True, highlight=False)
        yield PromptInput(placeholder="Ask the Server Manager…", id="prompt")
        yield Static("", id="hints")       # editing-shortcut legend

    async def on_mount(self) -> None:
        # Register the 23 shared themes and activate the saved one.
        for theme in build_themes():
            self.register_theme(theme)
        self.theme = self.cfg.theme_name if self.cfg.theme_name in THEME_ORDER else DEFAULT_THEME
        self.cc = chrome_colors(self)
        if self._anim is not None:
            self._anim.set_palette(palette_from_theme(self))
            self._anim.set_idle(not self._anim_on)
        self._server_state = await server_health() if self.project_root else "n/a"
        self._render_welcome(self._server_state)
        self._refresh_hints()
        self._refresh_status()
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
            if not self.provider.configured:
                log.write(f"[{c['tool']}]{G.WARN} No AI key configured yet.[/] "
                          "Set the app key (LLM_API_KEY) to power onboarding.")
            log.write(f"[{c['dim']}]No webAgent repo is linked yet. I can:[/]")
            log.write(f"  {G.BULLET} install webAgent for you (recommended: {self._recommended_install_path()})")
            log.write(f"  {G.BULLET} link an existing copy — tell me its folder and I'll manage it")
            log.write(f"  {G.BULLET} tell you about webAgent, or help with general questions")
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
        self._add_hdr(bar, "[Anim]", "toggle_anim")
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
        try:
            self.query_one("#hints", Static).update(t)
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

    def action_toggle_anim(self) -> None:
        """Show/hide the animated logo banner (stops the frame timer when hidden)."""
        self._anim_on = not self._anim_on
        self.cfg.anim_enabled = self._anim_on
        self.cfg.save()
        if self._anim is not None:
            self._anim.display = self._anim_on
            self._anim.set_idle(not self._anim_on)

    def on_app_blur(self) -> None:
        if self._anim is not None:
            self._anim.set_idle(True)          # pause animation when unfocused (~0% CPU)

    def on_app_focus(self) -> None:
        if self._anim is not None:
            self._anim.set_idle(not self._anim_on)

    def action_cycle_theme(self) -> None:
        idx = THEME_ORDER.index(self.theme) if self.theme in THEME_ORDER else -1
        nxt = THEME_ORDER[(idx + 1) % len(THEME_ORDER)]
        self.theme = nxt
        self.cc = chrome_colors(self)
        if self._anim is not None:
            self._anim.set_palette(palette_from_theme(self))
        self.cfg.theme_name = nxt
        self.cfg.save()
        self._refresh_status()
        self._refresh_hints()
        self._log(f"[{self.cc['accent']}]theme: {THEME_LABELS.get(nxt, nxt)}[/]")

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

    @work(exclusive=True, group="agent")
    async def _run_turn(self, text: str) -> None:
        assert self.agent is not None
        c = self.cc

        async def on_event(ev: AgentEvent) -> None:
            if ev.kind == "assistant" and ev.text:
                self._log(f"[{c['fg']}]{G.BOT} {ev.text}[/]")
            elif ev.kind == "tool_call":
                args = json.dumps(ev.args or {}, ensure_ascii=False)
                self._log(f"[{c['tool']}]{G.TOOL} {ev.tool}[/] [{c['dim']}]{args[:160]}[/]")
            elif ev.kind == "tool_result":
                snippet = ev.text.strip().splitlines()
                head = snippet[0] if snippet else ""
                ok = not head.startswith(("Error", "Refused", "[exit 1"))
                extra = f" (+{len(snippet) - 1} lines)" if len(snippet) > 1 else ""
                mark = G.OK if ok else G.WARN
                self._log(f"[{c['dim']}]{mark} {head[:200]}{extra}[/]")
            elif ev.kind == "error":
                self._log(f"[{c['error']}]{G.ERR} {ev.text}[/]")
            elif ev.kind == "status":
                self._log(f"[{c['dim']}]{ev.text}[/]")

        try:
            situation = await self._build_situation()
            await self.agent.run_turn(self.session_id, text, on_event, situation=situation)
        except Exception as e:  # surface, never crash the UI
            self._log(f"[{c['error']}]{G.ERR} agent error: {type(e).__name__}: {e}[/]")

    async def on_unmount(self) -> None:
        await self.llm.aclose()
        self.store.close()


def run() -> int:
    ServerManagerApp().run()
    return 0

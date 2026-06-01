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
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, RichLog

from .agent import AgentEvent, ServerManagerAgent
from .config import ProviderConfig, TuiConfig, db_path, resolve_provider
from .db import Store
from .glyphs import G
from .llm import LLMClient
from .theme_colors import chrome_colors
from .themes import CUSTOM_VAR_DEFAULTS, DEFAULT_THEME, THEME_LABELS, THEME_ORDER, build_themes


class ServerManagerApp(App):
    CSS_PATH = "styles.tcss"
    TITLE = "webAgent Server Manager"

    # priority=True so these global toggles fire even while the Input is focused
    # (otherwise the Input would swallow keys like Ctrl+W).
    BINDINGS = [
        Binding("ctrl+w", "toggle_writes", "Allow writes", priority=True),
        Binding("ctrl+a", "toggle_autonomous", "Autonomous", priority=True),
        Binding("ctrl+t", "cycle_theme", "Theme", priority=True),
        # Copy the transcript selection. priority=True so it wins while the Input
        # is focused (it always is), instead of Textual's default ctrl+c handling.
        Binding("ctrl+c", "copy_selection", "Copy", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg = TuiConfig.load()
        self.project_root: Path | None = self.cfg.project_dir()
        self.provider: ProviderConfig = resolve_provider(self.project_root, self.cfg)
        self.store = Store(db_path())
        self.llm = LLMClient(self.provider)
        self.agent: ServerManagerAgent | None = None
        self.session_id: str = ""
        self.cc: dict[str, str] = chrome_colors(self)  # concrete theme colors for Rich text
        if self.project_root:
            self.agent = ServerManagerAgent(self.cfg, self.project_root, self.llm, self.store)
            self.session_id = self.store.create_session(str(self.project_root))

    # Declared so the stylesheet's custom tokens ($dim, $tool, $bar-bg, …) parse
    # even before one of our themes is active (Textual requirement).
    def get_theme_variable_defaults(self) -> dict[str, str]:
        return CUSTOM_VAR_DEFAULTS

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield RichLog(id="log", wrap=True, markup=True, highlight=False)
            yield Input(
                placeholder=f"Ask the Server Manager…  ({G.SEP} Ctrl+W writes {G.SEP} "
                            f"Ctrl+A autonomous {G.SEP} Ctrl+T theme {G.SEP} Ctrl+Q quit)",
                id="prompt",
            )
        yield Footer()

    def on_mount(self) -> None:
        # Register the 23 shared themes and activate the saved one.
        for theme in build_themes():
            self.register_theme(theme)
        self.theme = self.cfg.theme_name if self.cfg.theme_name in THEME_ORDER else DEFAULT_THEME
        self.cc = chrome_colors(self)

        log = self.query_one("#log", RichLog)
        log.write(f"[b {self.cc['primary']}]{G.ADMIN} webAgent Server Manager[/] "
                  f"[{self.cc['dim']}]— server-independent admin agent[/]\n")
        if not self.project_root:
            log.write(f"[{self.cc['error']}]{G.ERR} No target project found.[/] Set "
                      "WEBAGENT_TUI_PROJECT to a webAgent checkout (run.py + app/) and restart.")
        else:
            log.write(f"[{self.cc['dim']}]project:[/] {self.project_root}")
        if not self.provider.configured:
            log.write(f"[{self.cc['tool']}]{G.WARN} No API key.[/] Set LLM_API_KEY (or the project's .env).")
        else:
            log.write(f"[{self.cc['dim']}]model:[/] {self.provider.model}")
        log.write(f"[{self.cc['dim']}]tip: drag to select {G.SEP} Ctrl+C copies {G.SEP} "
                  f"Shift+drag for your terminal's native select/copy[/]")
        self._refresh_subtitle()
        self.query_one("#prompt", Input).focus()

    # ── theme-aware Rich coloring ─────────────────────────────────────────
    def _log(self, markup: str) -> None:
        self.query_one("#log", RichLog).write(markup)

    def _refresh_subtitle(self) -> None:
        if self.cfg.autonomous:
            mode = f"{G.DOT_WARN} AUTONOMOUS"
        elif self.cfg.writes_enabled:
            mode = f"{G.DOT_LIVE} writes ON"
        else:
            mode = f"{G.DOT_DEAD} read-only"
        self.sub_title = f"{mode}  {G.SEP}  {THEME_LABELS.get(self.theme, self.theme)}"

    # ── actions ──────────────────────────────────────────────────────────
    def action_toggle_writes(self) -> None:
        self.cfg.writes_enabled = not self.cfg.writes_enabled
        self.cfg.save()
        self._refresh_subtitle()
        on = self.cfg.writes_enabled
        self._log(f"[{self.cc['secondary']}]{G.OK if on else G.BULLET} "
                  f"writes {'enabled' if on else 'disabled'}[/]")

    def action_toggle_autonomous(self) -> None:
        self.cfg.autonomous = not self.cfg.autonomous
        self.cfg.save()
        self._refresh_subtitle()
        on = self.cfg.autonomous
        self._log(f"[{self.cc['primary']}]{G.DOT_WARN if on else G.BULLET} autonomous mode "
                  f"{'ON — mutating tools run without gating' if on else 'off'}[/]")

    def action_cycle_theme(self) -> None:
        idx = THEME_ORDER.index(self.theme) if self.theme in THEME_ORDER else -1
        nxt = THEME_ORDER[(idx + 1) % len(THEME_ORDER)]
        self.theme = nxt
        self.cc = chrome_colors(self)
        self.cfg.theme_name = nxt
        self.cfg.save()
        self._refresh_subtitle()
        self._log(f"[{self.cc['accent']}]theme: {THEME_LABELS.get(nxt, nxt)}[/]")

    def action_copy_selection(self) -> None:
        """Copy the highlighted transcript text to the system clipboard.

        A TUI captures the mouse, so a normal click-drag is handled by the app
        (Textual highlights it) rather than the terminal. This sends that
        highlight to the OS clipboard via OSC-52. With nothing selected we keep
        the reflexive-Ctrl+C-quit hint so the key still does something useful.
        """
        selection = self.screen.get_selected_text()
        if selection:
            self.copy_to_clipboard(selection)
            n = len(selection)
            self.notify(f"Copied {n} character{'' if n == 1 else 's'} to the clipboard.",
                        title="Copy", timeout=2)
        else:
            self.notify(
                "Nothing selected. Drag over text to highlight it, then Ctrl+C to copy — "
                "or hold Shift while dragging to use your terminal's own select/copy. "
                "Ctrl+Q quits.",
                title="Copy", timeout=6,
            )

    @on(Input.Submitted, "#prompt")
    def _submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        if not self.agent:
            self._log(f"[{self.cc['error']}]{G.ERR} No project loaded — cannot run.[/]")
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
            await self.agent.run_turn(self.session_id, text, on_event)
        except Exception as e:  # surface, never crash the UI
            self._log(f"[{c['error']}]{G.ERR} agent error: {type(e).__name__}: {e}[/]")

    async def on_unmount(self) -> None:
        await self.llm.aclose()
        self.store.close()


def run() -> int:
    ServerManagerApp().run()
    return 0

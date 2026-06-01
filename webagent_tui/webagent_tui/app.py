"""Textual TUI for the webAgent Operator.

A single chat screen: a transcript pane that streams the agent's text, tool
calls, and tool results, plus an input. Mutating tools are gated behind an
"Allow writes" toggle (Ctrl+W) unless Autonomous mode (Ctrl+A) is on.
"""

from __future__ import annotations

import json
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, RichLog

from .agent import AgentEvent, OperatorAgent
from .config import ProviderConfig, TuiConfig, db_path, resolve_provider
from .db import Store
from .llm import LLMClient


class OperatorApp(App):
    CSS_PATH = "styles.tcss"
    TITLE = "webAgent Operator"

    BINDINGS = [
        Binding("ctrl+w", "toggle_writes", "Allow writes"),
        Binding("ctrl+a", "toggle_autonomous", "Autonomous"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.cfg = TuiConfig.load()
        self.project_root: Path | None = self.cfg.project_dir()
        self.provider: ProviderConfig = resolve_provider(self.project_root, self.cfg)
        self.store = Store(db_path())
        self.llm = LLMClient(self.provider)
        self.agent: OperatorAgent | None = None
        self.session_id: str = ""
        if self.project_root:
            self.agent = OperatorAgent(self.cfg, self.project_root, self.llm, self.store)
            self.session_id = self.store.create_session(str(self.project_root))

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield RichLog(id="log", wrap=True, markup=True, highlight=False)
            yield Input(placeholder="Ask the operator…  (Ctrl+W writes · Ctrl+A autonomous · Ctrl+Q quit)",
                        id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        log = self.query_one("#log", RichLog)
        log.write("[b]webAgent Operator[/b] — server-independent admin agent\n")
        if not self.project_root:
            log.write("[red]No target project found.[/red] Set WEBAGENT_TUI_PROJECT to a "
                      "webAgent checkout (a folder with run.py + app/) and restart.")
        else:
            log.write(f"[dim]project:[/dim] {self.project_root}")
        if not self.provider.configured:
            log.write("[yellow]No API key.[/yellow] Set LLM_API_KEY (or the project's .env).")
        else:
            log.write(f"[dim]model:[/dim] {self.provider.model}")
        self._refresh_subtitle()
        self.query_one("#prompt", Input).focus()

    def _refresh_subtitle(self) -> None:
        mode = "AUTONOMOUS" if self.cfg.autonomous else ("writes ON" if self.cfg.writes_enabled else "read-only")
        self.sub_title = f"{mode}"

    # ── actions ──────────────────────────────────────────────────────────
    def action_toggle_writes(self) -> None:
        self.cfg.writes_enabled = not self.cfg.writes_enabled
        self.cfg.save()
        self._refresh_subtitle()
        self.query_one("#log", RichLog).write(
            f"[cyan]writes {'enabled' if self.cfg.writes_enabled else 'disabled'}[/cyan]"
        )

    def action_toggle_autonomous(self) -> None:
        self.cfg.autonomous = not self.cfg.autonomous
        self.cfg.save()
        self._refresh_subtitle()
        self.query_one("#log", RichLog).write(
            f"[magenta]autonomous mode {'ON — mutating tools run without gating' if self.cfg.autonomous else 'off'}[/magenta]"
        )

    @on(Input.Submitted, "#prompt")
    def _submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        if not self.agent:
            self.query_one("#log", RichLog).write("[red]No project loaded — cannot run.[/red]")
            return
        self.query_one("#log", RichLog).write(f"\n[b green]›[/b green] {text}")
        self._run_turn(text)

    @work(exclusive=True, group="agent")
    async def _run_turn(self, text: str) -> None:
        log = self.query_one("#log", RichLog)
        assert self.agent is not None

        async def on_event(ev: AgentEvent) -> None:
            if ev.kind == "assistant" and ev.text:
                log.write(ev.text)
            elif ev.kind == "tool_call":
                args = json.dumps(ev.args or {}, ensure_ascii=False)
                log.write(f"[yellow]⚙ {ev.tool}[/yellow] [dim]{args[:160]}[/dim]")
            elif ev.kind == "tool_result":
                snippet = ev.text.strip().splitlines()
                head = snippet[0] if snippet else ""
                extra = f" [dim](+{len(snippet) - 1} lines)[/dim]" if len(snippet) > 1 else ""
                log.write(f"[dim]→ {head[:200]}{extra}[/dim]")
            elif ev.kind == "error":
                log.write(f"[red]{ev.text}[/red]")
            elif ev.kind == "status":
                log.write(f"[dim]{ev.text}[/dim]")

        try:
            await self.agent.run_turn(self.session_id, text, on_event)
        except Exception as e:  # surface, never crash the UI
            log.write(f"[red]agent error: {type(e).__name__}: {e}[/red]")

    async def on_unmount(self) -> None:
        await self.llm.aclose()
        self.store.close()


def run() -> int:
    OperatorApp().run()
    return 0

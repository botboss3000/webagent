"""Headless smoke test for the in-TUI chat.

Verifies (without a running server):
  * all launcher modules import,
  * `_parse_agent_ref` resolves the ref forms,
  * `ChatScreen` composes its widgets,
  * the SSE event router mounts user/agent/tool/pipeline widgets.

Run:  cd launcher && uv run python scripts/smoke_chat.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Collapsible, Static

from webagent_launcher.chat_screen import ChatScreen, _parse_agent_ref
from webagent_launcher.config import LauncherConfig

_STYLES = Path(__file__).resolve().parents[1] / "webagent_launcher" / "styles.tcss"


class _Harness(App):
    CSS_PATH = str(_STYLES)

    def compose(self) -> ComposeResult:
        return iter(())  # empty base screen; we push ChatScreen on top


def _check_parse() -> None:
    assert _parse_agent_ref("admin-agent")[0] == "template"
    assert _parse_agent_ref("admin-agent")[1] == "admin-agent"
    k, v, _ = _parse_agent_ref("agent:abc123")
    assert (k, v) == ("agent", "abc123")
    k, v, _ = _parse_agent_ref("template:default")
    assert (k, v) == ("template", "default")
    print("parse_agent_ref OK")


async def main() -> int:
    _check_parse()

    cfg = LauncherConfig()
    cfg.auto_start_server = False
    cfg.chat_default_view = False

    app = _Harness()
    async with app.run_test(size=(120, 40)) as pilot:
        # autostart=False → no connect worker, so driving events is deterministic.
        screen = ChatScreen(cfg, None, autostart=False)
        app.push_screen(screen)
        await pilot.pause(0.2)

        for sel in ("#chat-status", "#chat-log", "#chat-input", "#chat-hints"):
            assert screen.query(sel), f"missing {sel}"
        print("compose OK")

        # Drive the event router as if a turn is streaming (no server needed).
        screen.ready = True
        screen._clear_log()
        screen._add_user("run git status please")
        for ev in (
            {"type": "pipeline", "step": "memory_search_end", "results_count": 2},
            {"type": "pipeline", "step": "turn_start", "turn": 1, "max_turns": 10},
            {"type": "pipeline", "step": "llm_call_start", "model": "test-model"},
            {"type": "stream", "content": "Let me check. "},
            {"type": "tool_call", "tool": "run_command", "args": {"command": "git status"}},
            {"type": "tool_result", "tool": "run_command", "result": "On branch main", "duration_ms": 9},
            {"type": "stream", "content": "Done."},
            {"type": "response", "content": "On branch main — clean."},
        ):
            screen._handle_event(ev)
        await pilot.pause(0.3)

        log = screen.query_one("#chat-log")
        kids = [type(c).__name__ for c in log.children]
        print("chat-log children:", kids)

        tool_blocks = screen.query(Collapsible)
        assert len(tool_blocks) >= 1, "tool block not rendered"
        statics = screen.query(Static)
        assert len(statics) >= 4, "messages not rendered"
        print(f"event router OK ({len(tool_blocks)} tool block(s))")

    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

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

from webagent_launcher.chat_screen import (
    ChatInput,
    ChatScreen,
    _extract_image_paths,
    _fmt_tokens,
    _parse_agent_ref,
)
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


def _check_helpers() -> None:
    assert _fmt_tokens(950) == "950"
    assert _fmt_tokens(1234) == "1.2k"
    assert _fmt_tokens(None) == "0"
    # non-existent paths must not be treated as images
    assert _extract_image_paths("just some text") == []
    assert _extract_image_paths(r"C:\nope\missing.png") == []
    print("helpers OK")


async def main() -> int:
    _check_parse()
    _check_helpers()

    cfg = LauncherConfig()
    cfg.auto_start_server = False
    cfg.chat_default_view = False

    app = _Harness()
    async with app.run_test(size=(120, 40)) as pilot:
        # autostart=False → no connect worker, so driving events is deterministic.
        screen = ChatScreen(cfg, None, autostart=False)
        app.push_screen(screen)
        await pilot.pause(0.2)

        for sel in ("#chat-status", "#chat-body", "#chat-log", "#chat-input", "#chat-hints"):
            assert screen.query(sel), f"missing {sel}"
        assert isinstance(screen.query_one("#chat-input"), ChatInput), "input not ChatInput"
        # Welcome animation present on an empty session.
        assert screen.query("#chat-welcome"), "welcome animation missing on empty session"
        print("compose OK (welcome present)")

        # multi-line autosize: 3 newlines → height caps at 3 lines + border
        ta = screen.query_one("#chat-input", ChatInput)
        ta.text = "a\nb\nc\nd"
        screen._autosize_input()
        await pilot.pause(0.05)
        assert int(ta.styles.height.value) == 5, f"autosize height {ta.styles.height}"
        ta.text = ""
        print("input autosize OK")

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

        assert not screen.query("#chat-welcome"), "welcome should vanish once chatting"
        log = screen.query_one("#chat-log")
        kids = [type(c).__name__ for c in log.children]
        print("chat-log children:", kids)
        assert screen.query(".msg-stats"), "stats line not rendered after response"

        tool_blocks = screen.query(Collapsible)
        assert len(tool_blocks) >= 1, "tool block not rendered"
        statics = screen.query(Static)
        assert len(statics) >= 4, "messages not rendered"
        print(f"event router OK ({len(tool_blocks)} tool block(s))")

    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

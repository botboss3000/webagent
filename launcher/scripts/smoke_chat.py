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
from textual.binding import Binding
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
    # Mirror LauncherApp's app-level Ctrl+Q so the keybinding test is faithful.
    BINDINGS = [Binding("ctrl+q", "go_back_last", "Back", priority=True)]

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

        for sel in ("#chat-status", "#chat-body", "#chat-log", "#chat-hud",
                    "#chat-walker", "#chat-input", "#chat-hints"):
            assert screen.query(sel), f"missing {sel}"
        assert isinstance(screen.query_one("#chat-input"), ChatInput), "input not ChatInput"
        # Static banner pinned at the top of the transcript (no animation).
        assert screen.query(".chat-banner"), "static banner missing on empty session"
        print("compose OK (banner present)")

        # multi-line autosize: caps at 5 content lines + 2 border = 7
        ta = screen.query_one("#chat-input", ChatInput)
        ta.text = "a\nb\nc\nd"          # 4 lines → 4 + 2 border
        screen._autosize_input()
        await pilot.pause(0.05)
        assert int(ta.styles.height.value) == 6, f"autosize height {ta.styles.height}"
        ta.text = "\n".join(str(i) for i in range(8))  # 8 lines → capped at 5 + 2
        screen._autosize_input()
        await pilot.pause(0.05)
        assert int(ta.styles.height.value) == 7, f"autosize cap {ta.styles.height}"
        ta.text = ""
        print("input autosize OK (grows to 5 lines)")

        # Drive the event router as if a turn is streaming (no server needed).
        screen.ready = True
        screen._clear_log()
        screen._add_user("run git status please")
        for ev in (
            {"type": "pipeline", "step": "memory_search_end", "results_count": 2},
            {"type": "pipeline", "step": "turn_start", "turn": 1, "max_turns": 10},
            {"type": "pipeline", "step": "llm_call_start", "model": "claude-3-test"},
            {"type": "stream", "content": "Let me check. "},
            {"type": "tool_call", "tool": "run_command", "args": {"command": "git status"}},
            {"type": "tool_result", "tool": "run_command", "result": "On branch main", "duration_ms": 9},
            {"type": "billing", "input_tokens": 7500, "output_tokens": 116, "cost": 0.012},
            {"type": "stream", "content": "Done."},
            {"type": "response", "content": "On branch main — clean."},
        ):
            screen._handle_event(ev)
        await pilot.pause(0.3)

        # Banner stays pinned at the top even while chatting.
        assert screen.query(".chat-banner"), "banner should remain pinned at top"
        log = screen.query_one("#chat-log")
        kids = [type(c).__name__ for c in log.children]
        print("chat-log children:", kids)

        # Session HUD reflects accumulated tokens + context size.
        assert screen._s_out == 116, f"session out tokens {screen._s_out}"
        assert screen._ctx_tokens == 7500, f"context size {screen._ctx_tokens}"
        assert screen._ctx_max == 200000, f"ctx max for claude {screen._ctx_max}"
        print(f"session HUD OK (in={screen._s_in} out={screen._s_out} ctx={screen._ctx_tokens})")

        tool_blocks = screen.query(Collapsible)
        assert len(tool_blocks) >= 1, "tool block not rendered"
        statics = screen.query(Static)
        assert len(statics) >= 4, "messages not rendered"
        print(f"event router OK ({len(tool_blocks)} tool block(s))")

        # Interaction filter: hide tool blocks, confirm they collapse out of view.
        screen._apply_filter(set())  # uncheck everything
        await pilot.pause(0.05)
        assert all(not w.display for w in screen._cat_widgets["tools"]), "tool filter failed"
        screen._apply_filter({"tools", "loop", "memory"})
        await pilot.pause(0.05)
        assert all(w.display for w in screen._cat_widgets["tools"]), "tool unfilter failed"
        print("filter OK")

        # Keybindings: with the editor focused (the normal state), every command
        # key must still fire its action — both the shifted symbol AND the plain
        # ctrl+digit/backtick fallback — and must NOT leak into the message text.
        calls: list[str] = []
        screen.action_pick_agent = lambda: calls.append("agent")          # type: ignore
        screen.action_pick_session = lambda: calls.append("session")      # type: ignore
        screen.action_new_session = lambda: calls.append("new_session")   # type: ignore
        screen.action_new_agent = lambda: calls.append("new_agent")       # type: ignore
        screen.action_go_home = lambda: calls.append("home")              # type: ignore
        screen.action_filter = lambda: calls.append("filter")             # type: ignore
        app.action_go_back_last = lambda: calls.append("home")            # type: ignore  Ctrl+Q (app-level)
        ta = screen.query_one("#chat-input", ChatInput)
        ta.text = ""
        ta.focus()
        await pilot.pause(0.05)
        key_expect = [
            ("ctrl+exclamation_mark", "agent"), ("ctrl+1", "agent"),
            ("ctrl+at", "session"), ("ctrl+2", "session"),
            ("ctrl+number_sign", "new_session"), ("ctrl+3", "new_session"),
            ("ctrl+dollar_sign", "new_agent"), ("ctrl+4", "new_agent"),
            ("ctrl+tilde", "home"), ("ctrl+grave_accent", "home"), ("ctrl+q", "home"),
            ("ctrl+f", "filter"),
        ]
        for key, expect in key_expect:
            calls.clear()
            await pilot.press(key)
            await pilot.pause(0.02)
            assert expect in calls, f"{key} did not fire {expect} (got {calls})"
        assert ta.text == "", f"command keys leaked into the message box: {ta.text!r}"
        print(f"keybindings OK ({len(key_expect)} keys fire, none leak into input)")

        # Ctrl+C must NOT quit (it is the editor's copy). If it quit, the editor
        # would be gone and the following type would not land.
        ta.text = "hello"
        ta.focus()
        ta.move_cursor(ta.document.end)
        await pilot.pause(0.02)
        await pilot.press("ctrl+c")          # copy — must not quit
        await pilot.press("x")               # editor still alive?
        await pilot.pause(0.02)
        after = screen.query_one("#chat-input", ChatInput).text
        assert after == "hellox", \
            f"Ctrl+C disrupted the editor (quit instead of copy?): {after!r}"
        print("Ctrl+C copy (no quit) OK")

    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

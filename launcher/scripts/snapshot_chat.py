"""Render the chat screen (with fake content) to SVG for visual inspection.

Run:  uv run python scripts/snapshot_chat.py  [walk|work|cheer|trip|idle]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from textual.app import App, ComposeResult

from webagent_launcher.chat_screen import ChatScreen
from webagent_launcher.config import LauncherConfig

_STYLES = Path(__file__).resolve().parents[1] / "webagent_launcher" / "styles.tcss"


class _Harness(App):
    CSS_PATH = str(_STYLES)

    def compose(self) -> ComposeResult:
        return iter(())


async def main() -> int:
    state = sys.argv[1] if len(sys.argv) > 1 else "work"
    cfg = LauncherConfig()
    cfg.auto_start_server = False
    cfg.chat_default_view = False

    app = _Harness()
    async with app.run_test(size=(120, 34)) as pilot:
        screen = ChatScreen(cfg, None, autostart=False)
        app.push_screen(screen)
        await pilot.pause(0.2)
        screen.ready = True
        screen.session_title = "git status check"
        screen._refresh_status()
        screen._clear_log()
        screen._add_user("run git status and summarise the branch state")
        for ev in (
            {"type": "pipeline", "step": "turn_start", "turn": 2, "max_turns": 50},
            {"type": "pipeline", "step": "llm_call_start", "model": "claude-sonnet-4-6"},
            {"type": "stream", "content": "Let me check the working tree.\n\n"},
            {"type": "tool_call", "tool": "run_command", "args": {"command": "git status"}},
            {"type": "tool_result", "tool": "run_command", "result": "On branch main\nnothing to commit", "duration_ms": 12},
            {"type": "billing", "input_tokens": 128000, "output_tokens": 412, "cost": 0.0241},
            {"type": "stream", "content": "You're on **main**, working tree clean."},
            {"type": "response", "content": "You're on **main**, working tree clean."},
        ):
            screen._handle_event(ev)
        # show the loop detail too, so the snapshot exercises the dim lines
        screen._apply_filter({"tools", "loop", "memory"})
        screen.is_processing = True
        screen._activity = "running run_command..."
        screen._update_hud()
        if screen._walker:
            screen._walker.set_state(state)
        await pilot.pause(0.25)
        out = Path(f"chat_snapshot_{state}.svg")
        out.write_text(app.export_screenshot(title=f"chat ({state})"), encoding="utf-8")
        print(f"snapshot written: {out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

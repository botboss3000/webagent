"""Verify the Launch button's label transitions correctly with server state.

Doesn't actually start the webagent server — we just push synthetic
ServerState values into the app's update path and read the button label back.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from textual.widgets import Button

from webagent_launcher.app import LauncherApp
from webagent_launcher.config import LauncherConfig
from webagent_launcher.server import ServerState


async def main() -> int:
    cfg = LauncherConfig.load()
    cfg.project_path = str(Path(__file__).resolve().parents[2])
    cfg.save()

    app = LauncherApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        btn = app.query_one("#btn-launch", Button)

        # 1) Initial state — server is stopped, label should be "Launch"
        label_stopped = str(btn.label)
        assert "Launch" in label_stopped and "Browser" not in label_stopped, \
            f"expected stopped label without 'Browser', got: {label_stopped!r}"
        print(f"stopped label OK: {label_stopped!r}")

        # 2) Simulate transition to running
        running = ServerState(status="running", pid=1234, started_at=0.0)
        app._on_state_change(running)
        await pilot.pause(0.1)
        label_running = str(btn.label)
        assert "Browser" in label_running, \
            f"expected running label to mention 'Browser', got: {label_running!r}"
        print(f"running label OK: {label_running!r}")

        # 3) Simulate transition back to stopped
        app._on_state_change(ServerState(status="stopped"))
        await pilot.pause(0.1)
        label_back = str(btn.label)
        assert "Launch" in label_back and "Browser" not in label_back, \
            f"expected stopped label, got: {label_back!r}"
        print(f"back to stopped OK: {label_back!r}")

    print("launch button transitions: OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

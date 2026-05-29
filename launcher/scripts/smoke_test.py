"""Headless smoke test for the launcher.

Runs the app in test mode (Textual's Pilot) and confirms it composes,
mounts, and shuts down without exceptions. Used by CI/local dev.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from webagent_launcher.app import LauncherApp
from webagent_launcher.config import LauncherConfig


async def main() -> int:
    # Seed config so the setup screen doesn't gate us
    cfg = LauncherConfig.load()
    cfg.project_path = str(Path(__file__).resolve().parents[2])
    cfg.save()

    app = LauncherApp()
    async with app.run_test(size=(140, 40)) as pilot:
        # Let mount + initial animation frames happen
        await pilot.pause(0.5)
        # Open settings screen, cycle a theme, close
        await pilot.press("t")
        await pilot.pause(0.2)
        await pilot.press("right")
        await pilot.pause(0.1)
        await pilot.press("escape")
        await pilot.pause(0.2)
        # Cycle animation
        await pilot.press("space")
        await pilot.pause(0.2)
        # Quit (no server running so no confirm)
        await pilot.press("q")
        await pilot.pause(0.2)
    print("smoke test: OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

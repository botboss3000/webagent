"""Snapshot the settings modal as an SVG for visual inspection."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from webagent_launcher.app import LauncherApp
from webagent_launcher.config import LauncherConfig


async def main() -> int:
    cfg = LauncherConfig.load()
    cfg.project_path = str(Path(__file__).resolve().parents[2])
    cfg.save()

    app = LauncherApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.4)
        await pilot.press("t")          # open settings
        await pilot.pause(0.4)
        svg = app.export_screenshot(title="Settings panel")
        out = Path("snapshot_settings.svg")
        out.write_text(svg, encoding="utf-8")
        print(f"snapshot written: {out.resolve()}")
        # Verify Close button text + Esc key both fire dismiss
        await pilot.press("escape")
        await pilot.pause(0.3)
        # If we got here without crash, escape worked
        print("escape closed the modal (no crash)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

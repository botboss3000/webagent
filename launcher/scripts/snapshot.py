"""Render one screen of the launcher to plain text for visual inspection.

Run:  uv run python scripts/snapshot.py
"""

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
        await pilot.pause(1.2)  # let mount + first animation frame happen
        svg = app.export_screenshot(title="webagent launcher")
        out = Path("snapshot.svg")
        out.write_text(svg, encoding="utf-8")
        print(f"snapshot written: {out.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

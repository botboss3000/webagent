"""Print the Textual widget tree + sizes — diagnose why the stage isn't painting."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from webagent_launcher.app import LauncherApp
from webagent_launcher.config import LauncherConfig


def _dump(widget, depth: int = 0) -> None:
    cls = type(widget).__name__
    wid = getattr(widget, "id", None)
    region = getattr(widget, "region", None)
    layer = ""
    try:
        styles = getattr(widget, "styles", None)
        if styles is not None:
            layer = f"layer={styles.layer or '-'}"
    except Exception:
        pass
    print("  " * depth + f"- {cls} id={wid} region={region} {layer}")
    for child in getattr(widget, "children", ()):
        _dump(child, depth + 1)


async def main() -> int:
    cfg = LauncherConfig.load()
    cfg.project_path = str(Path(__file__).resolve().parents[2])
    cfg.save()
    app = LauncherApp()
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.5)
        print("=== widget tree ===")
        _dump(app.screen)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

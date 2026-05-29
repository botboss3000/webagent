"""Entry point: `python -m webagent_launcher` and the bundled .exe."""

from __future__ import annotations

import sys


def main() -> int:
    try:
        # Absolute import so this works both as `python -m webagent_launcher`
        # AND when PyInstaller runs the script directly as __main__.
        from webagent_launcher.app import LauncherApp
    except ImportError as e:
        sys.stderr.write(f"[webagent launcher] Failed to import app: {e}\n")
        sys.stderr.write("Install deps with: uv sync (inside launcher/) or pip install -e .\n")
        return 1

    app = LauncherApp()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

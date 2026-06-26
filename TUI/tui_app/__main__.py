"""Entry point: ``python -m tui_app`` and the bundled exe."""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from tui_app.app import run
    except ImportError as e:
        sys.stderr.write(f"[tui_app] failed to import app: {e}\n")
        sys.stderr.write("Install deps with: pip install -e .  (inside tui_app/)\n")
        return 1
    return run()


if __name__ == "__main__":
    raise SystemExit(main())

"""Entry point: ``python -m webagent`` and the bundled exe."""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from webagent.app import run
    except ImportError as e:
        sys.stderr.write(f"[webagent] failed to import app: {e}\n")
        sys.stderr.write("Install deps with: pip install -e .  (inside webagent/)\n")
        return 1
    return run()


if __name__ == "__main__":
    raise SystemExit(main())

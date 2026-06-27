"""Entry point: ``python -m tui_app`` and the bundled exe.

There is NO importable ``webagent`` module — ``pyproject.toml`` only exposes
``webagent`` as a *console script* pointing here. Launch the manager with
``python -m tui_app`` (or the ``webagent`` / ``tui-app`` command), never
``python -m webagent`` (that fails with "No module named webagent").
"""

from __future__ import annotations

import sys

_USAGE = (
    "webAgent Server Manager (TUI)\n"
    "\n"
    "Usage: python -m tui_app\n"
    "\n"
    "Launches the interactive Server-Manager TUI (requires a terminal). The\n"
    "package also installs `webagent` / `tui-app` console scripts that do the\n"
    "same thing. Note: there is no importable `webagent` module - always use\n"
    "`-m tui_app`.\n"
)


def main() -> int:
    argv = sys.argv[1:]
    # Cheap, terminal-free short-circuits: let the entry point be smoke-tested
    # (and answer `--help`/`--version`) without spinning up the full Textual app.
    if argv and argv[0] in ("-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0
    if argv and argv[0] in ("-V", "--version"):
        from . import __version__
        sys.stdout.write(f"tui_app {__version__}\n")
        return 0
    try:
        from tui_app.app import run
    except ImportError as e:
        sys.stderr.write(f"[tui_app] failed to import app: {e}\n")
        sys.stderr.write("Install deps with: pip install -e .  (inside tui_app/)\n")
        return 1
    return run()


if __name__ == "__main__":
    raise SystemExit(main())

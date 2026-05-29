"""Entry point: `python -m webagent_launcher` and the bundled .exe."""

from __future__ import annotations

import sys


def _force_utf8_io() -> None:
    """Make stdout/stderr UTF-8 so emoji never raise UnicodeEncodeError.

    A frozen Windows exe can inherit a legacy code page (cp1252); writing an
    emoji then crashes with UnicodeEncodeError. Reconfigure to UTF-8 with
    replacement so output is always safe.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def main() -> int:
    # Standalone console window (no Windows-Terminal relaunch). Emoji are used
    # only when the host terminal supports them — see webagent_launcher.glyphs;
    # a plain double-clicked window falls back to clean ASCII glyphs.
    _force_utf8_io()
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

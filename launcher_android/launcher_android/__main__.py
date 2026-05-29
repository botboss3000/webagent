"""Entry point: `python -m launcher_android`."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .app import LauncherApp


def _resolve_project_dir() -> Path:
    """Locate the webagent repo root.

    Priority:
      1. WEBAGENT_PROJECT_DIR env var
      2. parent of this package (assumes layout: <repo>/launcher_android/launcher_android/)
      3. cwd if it contains run.py
    """
    env = os.environ.get("WEBAGENT_PROJECT_DIR")
    if env:
        p = Path(env).expanduser().resolve()
        if (p / "run.py").exists():
            return p

    here = Path(__file__).resolve()
    candidate = here.parent.parent.parent
    if (candidate / "run.py").exists():
        return candidate

    cwd = Path.cwd()
    if (cwd / "run.py").exists():
        return cwd

    return candidate


def main() -> int:
    project_dir = _resolve_project_dir()
    if not (project_dir / "run.py").exists():
        print(
            f"ERROR: could not find webagent run.py.\n"
            f"  tried project_dir = {project_dir}\n"
            f"  set WEBAGENT_PROJECT_DIR to the webagent repo path and retry.",
            file=sys.stderr,
        )
        return 2

    app = LauncherApp(project_dir=project_dir)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

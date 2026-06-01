"""Build a single-file ``webagent-tui`` executable via PyInstaller.

    cd webagent_tui
    uv run --extra build python scripts/build_exe.py
    # → webagent-tui(.exe) at the webagent_tui/ root

The TUI's own data (external DB + config) lives in the per-user data dir (see
config.py), NOT next to the exe, so the binary stays a pure, relocatable
controller.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent       # webagent_tui/
PKG = ROOT / "webagent_tui"
NAME = "webagent-tui"


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.stderr.write("PyInstaller missing — install the 'build' extra:\n")
        sys.stderr.write("  uv sync --extra build   (or: pip install pyinstaller)\n")
        return 1

    # Textual ships a CSS file we must bundle; '.tcss' next to the package.
    sep = ";" if os.name == "nt" else ":"
    add_data = f"{PKG / 'styles.tcss'}{sep}webagent_tui"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--name", NAME,
        "--collect-all", "textual",      # Textual loads resources at runtime
        "--add-data", add_data,
        "--console",
        str(PKG / "__main__.py"),
    ]
    print("$", " ".join(cmd))
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        return rc

    exe = ROOT / "dist" / (f"{NAME}.exe" if os.name == "nt" else NAME)
    if exe.exists():
        dest = ROOT / exe.name
        shutil.move(str(exe), str(dest))
        print(f"✓ built {dest}")
    for junk in ("build", "dist", f"{NAME}.spec"):
        p = ROOT / junk
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

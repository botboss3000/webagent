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
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent       # webagent_tui/
PKG = ROOT / "webagent_tui"
NAME = "webagent-tui"
BUILD_STAMP = PKG / "_build.py"                      # generated; bundled then removed


def _read_version() -> str:
    try:
        ns: dict = {}
        for line in (PKG / "__init__.py").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("__version__"):
                exec(line, ns)  # noqa: S102 — trusted local source
                return str(ns.get("__version__", "0.0.0"))
    except OSError:
        pass
    return "0.0.0"


def _write_build_stamp() -> None:
    """Stamp the git commit + time into the package so the frozen exe knows which
    build it is (the self-update check reads this to tell if it's behind)."""
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT),
                                capture_output=True, text=True, timeout=10, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = ""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    BUILD_STAMP.write_text(
        f'BUILD_COMMIT = "{commit}"\nBUILD_TIME = "{now}"\nBUILD_VERSION = "{_read_version()}"\n',
        encoding="utf-8",
    )
    print(f"[build] stamped commit={commit or '(unknown)'} time={now}")


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.stderr.write("PyInstaller missing — install the 'build' extra:\n")
        sys.stderr.write("  uv sync --extra build   (or: pip install pyinstaller)\n")
        return 1

    _write_build_stamp()

    # Textual ships a CSS file we must bundle; '.tcss' next to the package.
    sep = ";" if os.name == "nt" else ":"
    add_data = f"{PKG / 'styles.tcss'}{sep}webagent_tui"
    # The manager's human-readable config (system prompt, tool list, monitor
    # defaults) lives under manager/ and is loaded at runtime (resources.py), so
    # the whole folder must ride along in the frozen bundle, landing at
    # webagent_tui/manager/ where Path(__file__).parent/'manager' resolves it.
    manager_data = f"{PKG / 'manager'}{sep}webagent_tui/manager"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--name", NAME,
        "--collect-all", "textual",      # Textual loads resources at runtime
        "--collect-all", "websockets",   # lazy submodule loading (asyncio client) — collect all
        "--add-data", add_data,
        "--add-data", manager_data,
        "--console",
        str(PKG / "__main__.py"),
    ]
    print("$", " ".join(cmd))
    rc = subprocess.call(cmd, cwd=str(ROOT))
    # The stamp has served its purpose (it's now inside the bundle); keep the
    # source tree clean so it's never accidentally committed.
    BUILD_STAMP.unlink(missing_ok=True)
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

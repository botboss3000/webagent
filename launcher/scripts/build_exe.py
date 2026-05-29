"""Build webagent.exe via PyInstaller.

Usage (from launcher/ directory):
    uv sync --extra build
    uv run python scripts/build_exe.py

Output:
    webagent.exe   (single portable file at the PROJECT ROOT, i.e. the
    repository's top folder — the parent of launcher/)

The script also cleans up PyInstaller's build/ and dist/ scratch folders
(under launcher/) so the only artifact left is webagent.exe at the root.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


EXE_NAME = "webagent.exe" if sys.platform == "win32" else "webagent"

# parents[1] = launcher/  ;  parents[2] = project root (repo top folder)
LAUNCHER_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ROOT = LAUNCHER_DIR  # build inputs (spec, icon, scratch dirs) live under launcher/
SPEC = ROOT / "webagent.spec"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
FINAL = PROJECT_ROOT / EXE_NAME  # final exe lands at the project root
ICON = ROOT / "assets" / "webagent.ico"
ICON_SCRIPT = ROOT / "scripts" / "generate_icon.py"


def _remove_path(p: Path) -> None:
    if not p.exists():
        return
    try:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink()
    except OSError as e:
        print(f"[build] warn: could not remove {p}: {e}", file=sys.stderr)


def _is_locked(p: Path) -> bool:
    """True if the file exists but can't be opened for writing (in use)."""
    if not p.exists():
        return False
    try:
        with open(p, "ab"):
            return False
    except OSError:
        return True


def _is_locked(p: Path) -> bool:
    """True if the file exists but can't be opened for writing (in use)."""
    if not p.exists():
        return False
    try:
        with open(p, "ab"):
            return False
    except OSError:
        return True


def main() -> int:
    if not SPEC.exists():
        print(f"[build] spec not found: {SPEC}", file=sys.stderr)
        return 1

    # Fail fast & clearly if a running instance holds webagent.exe open — the
    # final move would otherwise fail only AFTER a full PyInstaller build.
    if _is_locked(FINAL):
        print(
            f"[build] ERROR: {FINAL.name} is in use (a launcher instance is "
            f"running).\n[build] Close all running webagent.exe windows, then "
            f"re-run this build.",
            file=sys.stderr,
        )
        return 3

    # Generate model_windows.py from @earendil-works/pi-ai (npm package)
    # so the TUI ships with up-to-date context-window sizes for every model.
    build_models_script = ROOT / "scripts" / "build_model_windows.mjs"
    if build_models_script.exists():
        print(f"[build] generating model_windows.py from pi-ai...")
        rc = subprocess.call(["node", str(build_models_script)], cwd=str(ROOT))
        if rc != 0:
            print(f"[build] model generation failed ({rc}); continuing",
                  file=sys.stderr)
    else:
        print(f"[build] model generator not found: {build_models_script}; "
              f"using existing model_windows.py (if any)", file=sys.stderr)

    # Regenerate the icon — guarantees a fresh clone produces a complete .exe.
    if ICON_SCRIPT.exists():
        print(f"[build] regenerating icon -> {ICON.name}")
        rc = subprocess.call([sys.executable, str(ICON_SCRIPT)], cwd=str(ROOT))
        if rc != 0:
            print(f"[build] icon generation failed ({rc}); continuing without icon",
                  file=sys.stderr)

    # Clean previous artifacts (including any stale final exe)
    for p in (DIST, BUILD, FINAL):
        if p.exists():
            print(f"[build] removing {p.name}")
            _remove_path(p)

    cmd = [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", str(SPEC)]
    print(f"[build] $ {' '.join(cmd)}")
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        print(f"[build] PyInstaller exited {rc}", file=sys.stderr)
        return rc

    built = DIST / EXE_NAME
    if not built.exists():
        print(f"[build] expected output not found: {built}", file=sys.stderr)
        return 2

    # Move the .exe to the launcher root, then nuke the scratch folders.
    # Remove any stale final exe first so move() never hits "file exists".
    _remove_path(FINAL)
    try:
        shutil.move(str(built), str(FINAL))
    except (OSError, shutil.Error) as e:
        print(
            f"[build] ERROR: could not place {FINAL.name}: {e}\n"
            f"[build] The freshly built exe is at: {built}\n"
            f"[build] (Close any running webagent.exe and re-run, or copy it manually.)",
            file=sys.stderr,
        )
        return 4
    _remove_path(DIST)
    _remove_path(BUILD)

    size_mb = FINAL.stat().st_size / (1024 * 1024)
    print(f"[build] OK -> {FINAL}  ({size_mb:.1f} MB)")
    print(f"[build] (launcher/build and launcher/dist removed; exe is at the project root)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

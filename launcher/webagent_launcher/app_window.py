"""Open the webagent web app in a chromeless, AI-controllable Chromium window.

The launcher's **App Window** button calls :func:`open_window`, which launches
Chromium in *app mode* (``--app=<url>`` — no tabs, no address bar, looks like a
native desktop app) pointed at the local webagent server, with a **remote
debugging port** open.

That debugging port is the bridge to the agent: ``app/tools/browser.py``
notices the port is live and *attaches* to this very window over CDP, so the
agent's ``browser_action`` tool drives the same window the user is watching —
instead of its usual invisible (headless) browser. Close the window and the
agent silently falls back to headless. See ``app/tools/browser.py``.

Windows-only for now (the launcher's target platform). The browser is, in
preference order: the Playwright Chromium the installer downloaded (guaranteed
to match what the agent attaches with), then a system Chrome, then Edge.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from .config import tools_dir

# The agent (app/tools/browser.py) reads the SAME env var with the SAME default,
# and inherits this process's environment when the launcher spawns the server,
# so the two always agree on which port to meet on.
CDP_PORT = int(os.environ.get("WEBAGENT_BROWSER_CDP_PORT", "9222") or "9222")

# The launched browser process, if any. Module-level so the single app window
# is tracked across button presses and torn down on launcher exit.
_proc: Optional[subprocess.Popen] = None


# ── locating a Chromium-family browser ──────────────────────────────────────
def _playwright_chromium() -> Optional[Path]:
    """Highest-versioned Playwright Chromium under %LOCALAPPDATA%\\ms-playwright.

    Preferred because it's the exact build the agent's Playwright attaches with,
    and the installer guarantees it's present.
    """
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if not base.is_dir():
        return None
    best: tuple[int, Optional[Path]] = (-1, None)
    # Newer Playwright lays it down as chrome-win64; older as chrome-win.
    for exe in base.glob("chromium-*/chrome-win*/chrome.exe"):
        try:
            build = int(exe.parent.parent.name.split("-", 1)[1])
        except (IndexError, ValueError):
            build = 0
        if build >= best[0]:
            best = (build, exe)
    return best[1]


def _system_browser() -> Optional[Path]:
    """Fallback: a system-installed Chrome, then Edge (always on Win10/11)."""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    for cand in (
        Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(pf86) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(pf86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ):
        if cand.is_file():
            return cand
    return None


def find_browser() -> Optional[Path]:
    """The Chromium-family executable to drive, or None if none is installed."""
    return _playwright_chromium() or _system_browser()


def _profile_dir() -> Path:
    """A dedicated browser profile for the app window.

    Kept separate from the user's real Chrome profile so (a) the remote
    debugging port is honoured — Chrome refuses it on the default profile — and
    (b) we never disturb their browsing. Persisting it means the app window
    stays logged in to webagent between launches.
    """
    d = tools_dir().parent / "browser-profile"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── lifecycle ────────────────────────────────────────────────────────────────
def is_open() -> bool:
    """True while our app-window browser process is still alive."""
    return _proc is not None and _proc.poll() is None


def open_window(url: str, log: Callable[[str], None] = lambda _l: None) -> bool:
    """Open (or focus) the chromeless app-mode window pointed at ``url``.

    Returns True if a window is now open. Idempotent: a second press while the
    window is alive is a no-op (one app window, one debugging port).
    """
    global _proc
    if sys.platform != "win32":
        log("[launcher] app window is currently Windows-only")
        return False
    if is_open():
        log("[launcher] app window already open")
        return True

    browser = find_browser()
    if browser is None:
        log("[launcher] no Chromium found for the app window - run Update to download it")
        return False

    profile = _profile_dir()
    args = [
        str(browser),
        f"--app={url}",                          # chromeless single-site window
        f"--remote-debugging-port={CDP_PORT}",   # the agent attaches here
        f"--user-data-dir={profile}",            # dedicated profile (port + isolation)
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--window-size=1200,800",
    ]
    try:
        _proc = subprocess.Popen(args)
    except OSError as e:
        log(f"[launcher] could not open app window: {e}")
        _proc = None
        return False

    log(f"[launcher] app window -> {url}  (agent can control it | CDP :{CDP_PORT})")
    return True


def close_window() -> None:
    """Tear down the app window if we opened one (best-effort, called on exit)."""
    global _proc
    if _proc is None:
        return
    try:
        if _proc.poll() is None:
            _proc.terminate()
            try:
                _proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                _proc.kill()
    except OSError:
        pass
    _proc = None

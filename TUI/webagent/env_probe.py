"""Environment snapshot — the live state the agent is told about each turn.

The single biggest reliability lever for a lightweight model is to *not let it
guess* the situation. We probe the host once (cheap, static facts) and the local
server's health each turn (it changes), then hand the agent a compact summary so
its guidance and tool choices stay grounded instead of hallucinated.

Machine facts are gathered with the user's own privileges (no sandbox) and are
read-only. Nothing here mutates anything.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# webAgent's supported Python range (matches the app's runtime expectations).
_MIN_PY = (3, 11)
_MAX_PY = (3, 12)  # inclusive — 3.13+ hits dependency-build friction


def _is_termux() -> bool:
    if os.environ.get("TERMUX_VERSION"):
        return True
    prefix = os.environ.get("PREFIX", "")
    return "com.termux" in prefix or Path("/data/data/com.termux").exists()


def _os_label(is_termux: bool) -> str:
    if is_termux:
        return "Android (Termux)"
    sysname = platform.system()
    return {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}.get(sysname, sysname or "unknown")


def _find_system_python() -> tuple[Optional[str], Optional[str]]:
    """Locate a *system* Python on PATH (not this bundled interpreter) and its
    version. Returns (path, version_str) or (None, None)."""
    for name in ("python3", "python"):
        exe = shutil.which(name)
        if not exe:
            continue
        # Skip if it's literally our own frozen interpreter.
        try:
            if Path(exe).resolve() == Path(sys.executable).resolve():
                # Still report the bundled one's version if nothing else turns up.
                pass
        except OSError:
            pass
        try:
            out = subprocess.run(
                [exe, "--version"], capture_output=True, text=True, timeout=5, check=False
            )
            ver = (out.stdout or out.stderr or "").strip().replace("Python ", "")
            if ver:
                return exe, ver
        except (OSError, subprocess.SubprocessError):
            continue
    return None, None


def _py_supported(version_str: Optional[str]) -> Optional[bool]:
    if not version_str:
        return None
    try:
        parts = version_str.split(".")
        major, minor = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return None
    return _MIN_PY <= (major, minor) <= _MAX_PY


@dataclass
class MachineFacts:
    os_label: str
    arch: str
    is_termux: bool
    browser_capable: bool          # headless Chromium can run here? (no on Android)
    runtime_python: str            # this manager's own interpreter
    system_python: Optional[str]   # a Python found on PATH for running the server
    system_python_supported: Optional[bool]
    git_present: bool


def probe_machine() -> MachineFacts:
    """Gather static host facts. Cheap-ish (one subprocess for the Python probe);
    call once per session and cache — these don't change while running."""
    is_termux = _is_termux()
    sys_py_path, sys_py_ver = _find_system_python()
    return MachineFacts(
        os_label=_os_label(is_termux),
        arch=platform.machine() or "unknown",
        is_termux=is_termux,
        browser_capable=not is_termux,
        runtime_python="%d.%d.%d" % sys.version_info[:3],
        system_python=sys_py_ver,
        system_python_supported=_py_supported(sys_py_ver),
        git_present=shutil.which("git") is not None,
    )


async def server_health(port: int = 8080, timeout: float = 2.5) -> str:
    """Probe the local webAgent server. Returns 'running' | 'stopped' | 'unknown'.

    Uses the app's ``/health`` endpoint. A refused connection means stopped; any
    HTTP reply means something is listening (treated as running).

    The timeout is deliberately generous. A *refused* connection (nothing on the
    port) raises ``ConnectError`` immediately, so 'stopped' is always detected
    fast regardless of this value — the timeout only bounds how long we wait on a
    server that DID accept the connection. The first probe of a freshly started
    webAgent (cold httpx + a warming app) can take ~1s+, so a tight timeout (we
    used 0.6s) misreported a healthy server as 'unknown', leaving the status dot
    stuck on "checking" and the auto-start health gate falsely failing. 2.5s
    absorbs a cold/loaded probe while staying under the 3s status-poll cadence;
    the launcher uses a comparable 5s tolerance."""
    try:
        import httpx
    except ImportError:
        return "unknown"
    # Short connect (a down server fails fast via ConnectError anyway), generous
    # read so a slow-but-healthy reply isn't misclassified as a timeout.
    limits = httpx.Timeout(timeout, connect=min(timeout, 2.0))
    try:
        async with httpx.AsyncClient(timeout=limits) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/health")
        return "running" if resp.status_code < 500 else "unknown"
    except httpx.ConnectError:
        return "stopped"
    except httpx.HTTPError:
        return "unknown"

"""
Environment snapshot — the live state the agent is told about each turn.

The single biggest reliability lever for a lightweight model is to *not let it
guess* the situation. We probe the host once (cheap, static facts) and the local
server's health each turn (it changes), then hand the agent a compact summary so
its guidance and tool choices stay grounded instead of hallucinated.

Machine facts are gathered with the user's own privileges (no sandbox) and are
read-only. Nothing here mutates anything.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# WebAgent's supported Python range (matches the app's runtime expectations).
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
            exe_real = os.path.realpath(exe)
        except OSError:
            exe_real = exe
        me = os.path.normcase(os.path.realpath(sys.executable))
        if os.path.normcase(exe_real) == me:
            continue
        try:
            r = subprocess.run([exe, "--version"],
                               capture_output=True, text=True, timeout=5)
            ver = r.stdout.strip() or r.stderr.strip()
            if "Python" in ver:
                return exe, ver
        except Exception:
            continue
    return None, None


def _py_supported(ver_str: Optional[str]) -> bool:
    if not ver_str:
        return False
    try:
        parts = ver_str.replace("Python ", "").split(".")[:2]
        major, minor = int(parts[0]), int(parts[1])
        return (major, minor) >= _MIN_PY and (major, minor) <= _MAX_PY
    except (ValueError, IndexError):
        return False


@dataclass
class MachineProbe:
    os_label: str
    arch: str
    is_termux: bool
    browser_capable: bool
    runtime_python: str
    system_python: Optional[str]
    system_python_supported: bool
    git_present: bool


def probe_machine() -> MachineProbe:
    """One-shot (synchronous) snapshot of host facts — called once on start."""
    is_t = _is_termux()
    sys_py, sys_ver = _find_system_python()
    return MachineProbe(
        os_label=_os_label(is_t),
        arch=platform.machine(),
        is_termux=is_t,
        browser_capable=not is_t,
        runtime_python="%d.%d.%d" % sys.version_info[:3],
        system_python=sys_py if sys_ver else None,
        system_python_supported=_py_supported(sys_ver),
        git_present=shutil.which("git") is not None,
    )


def _probe_health_sync(port: int, timeout: float) -> str:
    """Blocking ``/health`` probe on the **standard library** (urllib + socket),
    returning 'running' | 'stopped' | 'unknown'.

    Deliberately stdlib-only: this machine's Python 3.14 has a broken httpx/SSL
    environment where every httpx call fails even against a healthy server, which
    used to force the watchdog to shell out to a subprocess on every tick. The
    stdlib path has no such problem, so one robust probe now serves the status
    dot, the launch gate, and the watchdog alike.

    Classified by **TCP reachability first**, which is the reliable signal on
    localhost:

    * We open a raw TCP connection. A listening socket accepts the handshake in
      the kernel *instantly* — even if the app is pegged at 100% CPU — so a
      successful connect always means a server is really there. A DOWN server
      fails to connect: it either refuses (RST) or, on hosts whose firewall
      silently DROPS packets to dead ports (this machine does), the connect times
      out. Both mean "no usable server", so **any connect failure → 'stopped'**,
      which is what lets the watchdog actually recover a down server here (the old
      httpx probe mis-read a dropped-packet timeout as indeterminate and never
      recovered).
    * Once connected, we ask ``/health``. A clean reply classifies it: <500 →
      'running', >=500 → 'unknown'. If the port accepts but the HTTP read then
      fails/hangs, the server is listening but not answering properly → 'unknown'
      (present-but-wedged; we don't kill on that signal alone)."""
    host = "127.0.0.1"
    connect_timeout = min(timeout, 2.0)
    try:                                     # 1) is anything actually listening?
        socket.create_connection((host, port), timeout=connect_timeout).close()
    except OSError:
        return "stopped"                     # refused OR dropped/timeout → no server
    try:                                     # 2) it accepts — does /health answer?
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=timeout) as resp:
            return "running" if resp.status < 500 else "unknown"
    except urllib.error.HTTPError as e:      # answered with an HTTP error code
        return "running" if e.code < 500 else "unknown"
    except (urllib.error.URLError, OSError):
        return "unknown"                     # listening but not answering cleanly → wedged


def _probe_ready_sync(port: int, timeout: float) -> str:
    """Blocking **readiness** probe on ``/health/ready`` (stdlib), returning
    'ready' | 'not_ready' | 'unknown'.

    Readiness is deeper than the liveness ``/health`` that :func:`server_health`
    checks: the app answers ``/health/ready`` with 503 when it's up but can't do
    real work (a wedged DB round-trip). We map only a clean 200 to 'ready' and only
    an explicit 503 to 'not_ready'; anything else — nothing listening, the endpoint
    missing on an OLDER server build (404), or an ambiguous error — is 'unknown', so
    the caller takes NO readiness action on a signal it can't trust. Liveness owns
    the "is it even up?" question; this only refines a server already known live."""
    host = "127.0.0.1"
    connect_timeout = min(timeout, 2.0)
    try:                                     # nothing listening → liveness' job, not ours
        socket.create_connection((host, port), timeout=connect_timeout).close()
    except OSError:
        return "unknown"
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health/ready", timeout=timeout) as resp:
            return "ready" if resp.status < 400 else "unknown"
    except urllib.error.HTTPError as e:      # 503 = explicitly not ready; 404 (old build) = unknown
        return "not_ready" if e.code == 503 else "unknown"
    except (urllib.error.URLError, OSError):
        return "unknown"


async def server_ready(port: int = 8080, timeout: float = 2.5) -> str:
    """Probe the local server's READINESS (``/health/ready``). Returns
    'ready' | 'not_ready' | 'unknown'. Async wrapper over the blocking stdlib probe
    so the Textual loop never stalls. Only meaningful for a server already known
    live via :func:`server_health` — 'unknown' means "don't act on this"."""
    return await asyncio.to_thread(_probe_ready_sync, port, timeout)


async def server_health(port: int = 8080, timeout: float = 2.5) -> str:
    """Probe the local WebAgent server. Returns 'running' | 'stopped' | 'unknown'.

    Async wrapper that runs the blocking stdlib probe in a worker thread so the
    Textual event loop never stalls on a slow reply.

    The timeout is deliberately generous. A *refused* connection (nothing on the
    port) fails immediately, so 'stopped' is always detected fast regardless of
    this value — the timeout only bounds how long we wait on a server that DID
    accept the connection. The first probe of a freshly started WebAgent (a
    warming app) can take ~1s+, so a tight timeout (we used 0.6s) misreported a
    healthy server as 'unknown', leaving the status dot stuck on "checking" and
    the auto-start health gate falsely failing. 2.5s absorbs a cold/loaded probe
    while staying under the 3s status-poll cadence; the launcher uses a
    comparable 5s tolerance."""
    return await asyncio.to_thread(_probe_health_sync, port, timeout)
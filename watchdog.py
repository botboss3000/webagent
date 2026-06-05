"""webAgent watchdog — keep the server alive independently of the TUI.

The Server-Manager TUI normally launches the webAgent server (``run.py`` on
port 8080), but if the TUI crashes or the server hangs, nothing brings it back.
This script is a small, dependency-free supervisor you can leave running in its
own window: it watches the server and restarts it whenever it dies or stops
answering ``/health``.

What it does, in a loop:
  * If no server is up, launch ``run.py`` from the checkout's own ``.venv``.
  * If the server process we launched exits, relaunch it.
  * If the server is alive but ``/health`` fails N times in a row (hung),
    kill it and relaunch.
  * Before each (re)launch, clear any zombie still holding port 8080 so the
    fresh server can bind.
  * Crash-loop backoff: if it keeps dying fast, wait longer between tries so
    we don't spin the CPU hammering a broken build.

It coexists with the TUI: both check ``/health`` before starting, so whoever
is up first owns the server and the other just monitors it. Stop the watchdog
with Ctrl+C — it shuts the server it launched down cleanly on the way out.

Run it:  python watchdog.py        (or double-click watchdog.bat on Windows)

Standard library only, so it runs under any Python 3.8+ — it does not need the
project venv itself (it only *uses* the venv to launch the server).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ── configuration ─────────────────────────────────────────────────────────────
HOST = "127.0.0.1"
PORT = 8080
HEALTH_URL = f"http://{HOST}:{PORT}/health"

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"
SERVER_LOG = LOG_DIR / "watchdog-server.log"   # captured stdout/stderr of run.py
WATCHDOG_LOG = LOG_DIR / "watchdog.log"         # the watchdog's own activity

POLL_SECONDS = 5.0          # how often we check health while the server is up
HEALTH_TIMEOUT = 4.0        # per-probe HTTP timeout
UNHEALTHY_STRIKES = 3       # consecutive failed probes before we call it "hung"
START_GRACE_SECONDS = 60.0  # cold boot can be slow; wait this long for first /health

# Crash-loop backoff: if the server dies within FAST_FAIL_SECONDS of starting,
# we count it as a fast failure and grow the wait (capped) so we stop hammering.
FAST_FAIL_SECONDS = 30.0
BACKOFF_BASE = 3.0
BACKOFF_MAX = 60.0

_IS_WIN = sys.platform == "win32"
_NO_WINDOW = 0x08000000          # CREATE_NO_WINDOW
_NEW_GROUP = 0x00000200          # CREATE_NEW_PROCESS_GROUP


# ── small helpers ─────────────────────────────────────────────────────────────
def log(msg: str) -> None:
    """Print to the console and append to the watchdog's own log file."""
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def venv_python() -> Path | None:
    """The checkout's own venv interpreter, or None if the env isn't built yet."""
    for venv_dir in (".venv", "venv"):
        cand = (PROJECT_ROOT / venv_dir
                / ("Scripts" if _IS_WIN else "bin")
                / ("python.exe" if _IS_WIN else "python"))
        if cand.exists():
            return cand
    return None


def is_healthy() -> bool:
    """True if /health answers with any non-5xx HTTP status."""
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=HEALTH_TIMEOUT) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500          # an HTTP error still means something is listening
    except (urllib.error.URLError, OSError):
        return False                 # refused / timed out → nothing healthy there


def pids_on_port(port: int = PORT) -> set[int]:
    """PIDs of processes LISTENING on the port (best-effort, no psutil)."""
    pids: set[int] = set()
    try:
        if _IS_WIN:
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                                 capture_output=True, text=True,
                                 creationflags=_NO_WINDOW).stdout or ""
            needle = f":{port} "
            for ln in out.splitlines():
                if needle in ln and "LISTENING" in ln:
                    parts = ln.split()
                    if parts and parts[-1].isdigit():
                        pids.add(int(parts[-1]))
        else:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True).stdout or ""
            for tok in out.split():
                if tok.isdigit():
                    pids.add(int(tok))
    except (OSError, subprocess.SubprocessError):
        pass
    return pids


def kill_pid(pid: int) -> None:
    try:
        if _IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True, text=True, creationflags=_NO_WINDOW)
        else:
            os.kill(pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass


def clear_port() -> None:
    """Kill any zombie still holding the port so a fresh server can bind."""
    for pid in pids_on_port():
        log(f"clearing stale PID {pid} holding port {PORT}")
        kill_pid(pid)
    if pids_on_port():
        time.sleep(1.5)              # give the OS a moment to release the socket


def launch_server(py: Path) -> subprocess.Popen | None:
    """Spawn run.py from the venv, capturing its output to SERVER_LOG."""
    clear_port()
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logf = open(SERVER_LOG, "ab")
    except OSError as e:
        log(f"could not open server log: {e}")
        return None
    kwargs: dict = dict(cwd=str(PROJECT_ROOT), stdout=logf,
                        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    if _IS_WIN:
        # CREATE_NO_WINDOW: headless console (no stray window). NEW_PROCESS_GROUP:
        # our Ctrl+C doesn't propagate straight into the child — we stop it
        # deliberately via terminate() so shutdown is orderly.
        kwargs["creationflags"] = _NO_WINDOW | _NEW_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen([str(py), "run.py"], **kwargs)
    except OSError as e:
        log(f"failed to launch server: {e}")
        return None
    finally:
        logf.close()
    log(f"launched server (pid {proc.pid}); waiting for /health…")
    return proc


def wait_until_healthy(proc: subprocess.Popen) -> bool:
    """Poll /health until the freshly started server answers, or it dies / times out."""
    deadline = time.monotonic() + START_GRACE_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log(f"server exited during startup (code {proc.returncode}); see {SERVER_LOG.name}")
            return False
        if is_healthy():
            log(f"server healthy at http://localhost:{PORT}  (UI: /index.html)")
            return True
        time.sleep(1.0)
    log("server did not become healthy within the start grace period")
    return False


def stop_server(proc: subprocess.Popen | None) -> None:
    """Terminate the server we launched (and any leftover on the port)."""
    if proc is not None and proc.poll() is None:
        log(f"stopping server (pid {proc.pid})")
        kill_pid(proc.pid)
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
    clear_port()


# ── main supervision loop ─────────────────────────────────────────────────────
def main() -> int:
    log(f"watchdog starting — supervising webAgent on port {PORT}")
    log(f"project: {PROJECT_ROOT}")

    py = venv_python()
    if py is None:
        log("ERROR: no .venv found in this checkout. Build it first "
            "(run webAgent.bat or `uv sync`), then start the watchdog.")
        return 1
    log(f"using interpreter: {py}")

    proc: subprocess.Popen | None = None
    fast_failures = 0

    # Adopt an already-running server (e.g. one the TUI started) instead of
    # starting a duplicate. We can't get a process handle for it, so we monitor
    # by health alone until it goes down, then take ownership.
    if is_healthy():
        log("a server is already running — monitoring it (not launching a duplicate)")

    try:
        while True:
            owned = proc is not None and proc.poll() is None

            # 1. Nothing of ours is alive — make sure SOMETHING healthy is up.
            if not owned:
                if proc is not None:        # our child just died
                    ran_for = time.monotonic() - started_at
                    log(f"server process exited (code {proc.returncode}) "
                        f"after {ran_for:.0f}s")
                    fast_failures = fast_failures + 1 if ran_for < FAST_FAIL_SECONDS else 0
                    proc = None

                if is_healthy():
                    # Someone else's server is healthy — just keep watching it.
                    time.sleep(POLL_SECONDS)
                    continue

                if fast_failures:
                    wait = min(BACKOFF_BASE * (2 ** (fast_failures - 1)), BACKOFF_MAX)
                    log(f"crash-loop backoff: waiting {wait:.0f}s "
                        f"(fast failure #{fast_failures})")
                    time.sleep(wait)

                started_at = time.monotonic()
                proc = launch_server(py)
                if proc is None:
                    time.sleep(BACKOFF_MAX)
                    continue
                if not wait_until_healthy(proc):
                    stop_server(proc)
                    ran_for = time.monotonic() - started_at
                    fast_failures = fast_failures + 1 if ran_for < FAST_FAIL_SECONDS else fast_failures
                    proc = None
                    continue
                fast_failures = 0           # healthy start resets the backoff
                time.sleep(POLL_SECONDS)
                continue

            # 2. Our server is alive — is it actually responding?
            strikes = 0
            while strikes < UNHEALTHY_STRIKES:
                if proc.poll() is not None:     # it died between probes
                    break
                if is_healthy():
                    strikes = 0
                    time.sleep(POLL_SECONDS)
                    continue
                strikes += 1
                log(f"/health failed ({strikes}/{UNHEALTHY_STRIKES})")
                time.sleep(2.0)

            if proc.poll() is None and strikes >= UNHEALTHY_STRIKES:
                log("server is hung (unresponsive) — restarting it")
                ran_for = time.monotonic() - started_at
                fast_failures = fast_failures + 1 if ran_for < FAST_FAIL_SECONDS else 0
                stop_server(proc)
                proc = None
            # loop back around to relaunch / re-evaluate
    except KeyboardInterrupt:
        log("Ctrl+C received — shutting down")
    finally:
        stop_server(proc)
        log("watchdog stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Supervisor-cooperative server self-restart.

Lets the running WebAgent server restart **itself** from the UI without depending
on an external supervisor being present — so a settings change that needs a fresh
process (e.g. switching the secrets vault for a clean cutover) can be applied with
one click instead of asking the user to restart manually.

Two roles, one file:

* **Detached relauncher** (run as a standalone script — see ``trigger_restart``):
  it waits for the old server to stop answering ``/health``, then gives any
  external supervisor (the Server-Manager guardian, a ``WebAgent.bat`` loop,
  systemd/launchd…) a short head-start to revive the server, and ONLY if nothing
  does within that window does it relaunch ``run.py`` itself. So:
    - on a supervised host the supervisor wins the race → no duplicate server;
    - on an unsupervised host the relauncher brings it back → never stranded.
  If a duplicate ever does slip through, the guardian's reaper collapses it to the
  one process actually serving :8080.

* **Helpers** imported by the admin API: ``auto_restart_available()`` (can we
  self-revive on this host?) and ``trigger_restart()`` (spawn the relauncher, then
  exit this process so the port frees).

Intentionally STDLIB-ONLY and spawned by FILE PATH (not ``-m app.relauncher``), so
the tiny relauncher process never imports the heavy ``app`` package.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

# Default port for self-restart. Reads WEBAGENT_PORT so a SIBLING instance (one a
# different repo folder started on 8081/8082 via the Local Instances page) that
# self-restarts probes + relaunches on ITS own port, not the 8080 hub. The hub is
# started with no WEBAGENT_PORT, so it stays 8080. The relaunched `run.py` inherits
# this same env, so it re-binds the correct port automatically.
def _resolve_port() -> int:
    raw = (os.environ.get("WEBAGENT_PORT") or "").strip()
    if raw:
        try:
            p = int(raw)
            if 1 <= p <= 65535:
                return p
        except ValueError:
            pass
    return 8080


PORT = _resolve_port()

_IS_WIN = sys.platform == "win32"
_NO_WINDOW = 0x08000000      # CREATE_NO_WINDOW   — hidden console (background daemon)
_NEW_GROUP = 0x00000200      # CREATE_NEW_PROCESS_GROUP — detach from parent's Ctrl+C

# ── cadence / windows (seconds) ──────────────────────────────────────────────
_DOWN_WAIT = 30.0            # how long to wait for the OLD server to stop answering
_COOP_WAIT = 12.0            # head-start given to an external supervisor to revive it
_PORT_WAIT = 20.0            # if the port is held but unhealthy, wait this long for it
                            # to either become healthy (someone else won) or free up
_UP_WAIT = 180.0            # after WE launch, how long to wait for /health before exit
_PROBE_TIMEOUT = 3.0


def _project_root() -> str:
    """The WebAgent checkout root — the parent of this file's ``app/`` dir."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_py(root: str) -> str:
    return os.path.join(root, "run.py")


def auto_restart_available(root: str | None = None) -> tuple[bool, str]:
    """Whether this host can revive itself after a self-restart.

    True unless we can't even find the entrypoint to relaunch — in which case the
    UI should fall back to asking the user to restart manually rather than risk
    stranding them with a dead server."""
    root = root or _project_root()
    if not os.path.isfile(_run_py(root)):
        return False, "run.py not found — cannot auto-relaunch on this host."
    if not sys.executable:
        return False, "No Python interpreter resolved — cannot auto-relaunch."
    return True, ""


def _detached_kwargs(log_path: str | None) -> dict:
    try:
        out: object = open(log_path, "ab") if log_path else subprocess.DEVNULL
    except OSError:
        out = subprocess.DEVNULL
    kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=out,
                        stderr=subprocess.STDOUT, close_fds=True)
    if _IS_WIN:
        kwargs["creationflags"] = _NO_WINDOW | _NEW_GROUP
    else:
        kwargs["start_new_session"] = True
    return kwargs


def trigger_restart(port: int = PORT, delay: float = 0.6) -> dict:
    """Spawn the detached relauncher, then schedule this process to exit.

    The relauncher waits for us to go down, cooperates with any supervisor, and
    relaunches ``run.py`` only if nothing else does. Returns immediately so the
    HTTP response can flush before the ``os._exit`` fires."""
    root = _project_root()
    ok, reason = auto_restart_available(root)
    if not ok:
        return {"status": "unavailable", "auto_restart": False, "reason": reason}

    log_path = os.path.join(root, "data", "relauncher.log")
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
    except OSError:
        log_path = None

    args = [
        sys.executable, os.path.abspath(__file__),
        "--port", str(port),
        "--root", root,
        "--python", sys.executable,
        "--pid", str(os.getpid()),
    ]
    try:
        subprocess.Popen(args, cwd=root, **_detached_kwargs(log_path))
    except OSError as e:
        return {"status": "error", "auto_restart": False,
                "reason": f"Could not spawn relauncher: {e}"}

    import threading

    def _die() -> None:
        time.sleep(max(0.1, delay))
        os._exit(0)

    threading.Thread(target=_die, daemon=True).start()
    return {"status": "restarting", "auto_restart": True,
            "message": "Server is restarting — it will be back in a few seconds."}


# ── the detached relauncher loop ─────────────────────────────────────────────
def _health_up(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=_PROBE_TIMEOUT) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except (urllib.error.URLError, OSError):
        return False


def _port_in_use(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def _launch_server(python: str, root: str) -> bool:
    run_py = _run_py(root)
    if not os.path.isfile(run_py):
        return False
    log_path = os.path.join(root, "data", "relauncher.log")
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
    except OSError:
        log_path = None
    try:
        subprocess.Popen([python, "run.py"], cwd=root, **_detached_kwargs(log_path))
        return True
    except OSError:
        return False


def _relaunch(port: int, root: str, python: str) -> int:
    deadline = time.monotonic() + _DOWN_WAIT
    while time.monotonic() < deadline:               # 1. wait for the old server to go down
        if not _health_up(port):
            break
        time.sleep(0.7)

    deadline = time.monotonic() + _COOP_WAIT
    while time.monotonic() < deadline:               # 2. let a supervisor revive it first
        if _health_up(port):
            return 0                                  # someone else won → nothing to do
        time.sleep(0.7)

    if _port_in_use(port):                            # 3. port held but unhealthy → don't double-bind
        deadline = time.monotonic() + _PORT_WAIT
        while time.monotonic() < deadline:
            if _health_up(port):
                return 0                              # it finished booting → done
            if not _port_in_use(port):
                break                                 # freed up → safe to launch
            time.sleep(0.7)
        else:
            return 0                                  # still squatting → leave it; don't fight

    if not _launch_server(python, root):              # 4. nothing revived it → we do
        return 1

    deadline = time.monotonic() + _UP_WAIT
    while time.monotonic() < deadline:
        if _health_up(port):
            return 0
        time.sleep(1.0)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="WebAgent server relauncher")
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--root", default=_project_root())
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--pid", type=int, default=0)      # informational; we gate on /health
    a = p.parse_args(argv)
    return _relaunch(a.port, a.root, a.python or sys.executable)


if __name__ == "__main__":
    raise SystemExit(main())

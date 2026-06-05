"""The external **keep-alive guardian** — a supervisor that outlives the TUI.

The Server-Manager TUI (``python -m webagent``) auto-starts the webAgent web
server and watches it via the *in-process* ``watchdog.py``. But that watchdog is
a TUI worker: it dies the instant the TUI dies, which is exactly when the server
it launched is most likely to fall over too. This module is the answer — a
**separate process**, spawned by the TUI but fully detached from it, that keeps
*both* alive:

* **the web server** — if ``/health`` stops answering and a checkout is linked,
  relaunch ``run.py`` from the *project's* venv (with crash-loop backoff);
* **the TUI itself** — if the TUI process vanishes WITHOUT a clean-quit marker
  (i.e. it crashed or its window was closed), relaunch it in a fresh console.

Design points that matter:

* **Singleton.** Reopening the TUI must never stack guardians. The TUI does a
  cheap pre-check, and the guardian makes an atomic ``O_EXCL`` claim on
  ``guardian.pid`` at startup (and re-checks ownership each tick), so a duplicate
  spawn is a harmless no-op that exits immediately.
* **Two interpreters.** Relaunching the **TUI** uses *this* process's
  interpreter (the TUI spawned us with its own venv python, so ``sys.executable``
  is already the TUI venv). Relaunching the **server** uses the *project*
  checkout's venv (``_venv_python(project_root)``). Mixing them up imports the
  wrong package — keep them separate.
* **Off switch.** ``guardian.json`` (``{"enabled": bool}``) is re-read every tick;
  the ADMIN toggle flips it and terminates this process. Turning it off NEVER
  stops the server or TUI — it only stops *supervising* them.

Standard-library only and intentionally import-light (no Textual, no httpx, no
``webagent.tools`` — that package pulls the whole tool registry). It depends only
on :mod:`webagent.config` for the data dir + the linked project path.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import data_dir, TuiConfig

PORT = 8080
HEALTH_URL = f"http://127.0.0.1:{PORT}/health"

_IS_WIN = sys.platform == "win32"
_NO_WINDOW = 0x08000000      # CREATE_NO_WINDOW  — hidden console (background daemon)
_NEW_GROUP = 0x00000200      # CREATE_NEW_PROCESS_GROUP — detach from parent's Ctrl+C
_NEW_CONSOLE = 0x00000010    # CREATE_NEW_CONSOLE — a real, visible window for the TUI

# ── cadence / thresholds ──────────────────────────────────────────────────────
POLL_SECONDS = 5.0           # gap between supervision ticks
HEALTH_TIMEOUT = 4.0         # per-probe HTTP timeout
INITIAL_GRACE = 8.0          # let the TUI's own autostart begin before we act
SERVER_SETTLE = 60.0         # after launching the server, give it this long to boot
                             # before considering another launch (cold boot is slow)
TUI_RELAUNCH_GAP = 30.0      # min seconds between TUI relaunches (crash-loop guard +
                             # covers the window before the new TUI writes its pid)
MAX_SERVER_RESTARTS_PER_HOUR = 5


# ── state-file locations (all in the TUI data dir) ────────────────────────────
def _gpid_file() -> Path:
    return data_dir() / "guardian.pid"


def _gstate_file() -> Path:
    return data_dir() / "guardian.json"


def _tpid_file() -> Path:
    return data_dir() / "tui.pid"


def _clean_exit_file() -> Path:
    return data_dir() / "tui.clean_exit"


def _glog_file() -> Path:
    return data_dir() / "guardian.log"


def _server_pid_file() -> Path:
    # The SAME file tools/server.py uses, so the manager keeps tracking the
    # server even when the guardian is the one that (re)launched it.
    return data_dir() / "server.pid"


def _server_log_file() -> Path:
    return data_dir() / "server.log"


# ── tiny JSON + process primitives (no psutil, no tools.server import) ─────────
def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _write_json_atomic(path: Path, obj: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _read_pid(path: Path) -> int:
    data = _read_json(path)
    if isinstance(data, dict):
        try:
            return int(data.get("pid") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if _IS_WIN:
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, creationflags=_NO_WINDOW)
            return str(pid) in (out.stdout or "")
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _terminate(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        if _IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True, text=True, creationflags=_NO_WINDOW)
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _venv_python(project_root: Path) -> Optional[Path]:
    """The webAgent checkout's own venv interpreter (for running ``run.py``)."""
    for venv_dir in (".venv", "venv"):
        cand = (project_root / venv_dir / ("Scripts" if _IS_WIN else "bin")
                / ("python.exe" if _IS_WIN else "python"))
        if cand.exists() or cand.is_symlink():
            return cand
    return None


def _pids_on_port(port: int = PORT) -> set[int]:
    pids: set[int] = set()
    try:
        if _IS_WIN:
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                                 text=True, creationflags=_NO_WINDOW).stdout or ""
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


def _is_healthy() -> bool:
    """True if /health answers with any non-5xx status (urllib — no httpx dep)."""
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=HEALTH_TIMEOUT) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except (urllib.error.URLError, OSError):
        return False


def _log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        with open(_glog_file(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ── on/off state (shared with the ADMIN toggle in app.py) ─────────────────────
def read_enabled() -> bool:
    """Whether the guardian should run. Absent file → on by default."""
    data = _read_json(_gstate_file())
    if not isinstance(data, dict):
        return True
    return bool(data.get("enabled", True))


def write_enabled(enabled: bool) -> None:
    _write_json_atomic(_gstate_file(), {"enabled": bool(enabled)})


def guardian_pid() -> int:
    return _read_pid(_gpid_file())


def guardian_alive() -> bool:
    pid = guardian_pid()
    return bool(pid and pid != os.getpid() and _pid_alive(pid))


# ── control plane used by the TUI (app.py) ────────────────────────────────────
def write_tui_pid() -> None:
    """The TUI records its own PID so the guardian can tell when it dies."""
    _write_json_atomic(_tpid_file(), {"pid": os.getpid(), "ts": time.time()})


def clear_clean_exit() -> None:
    """Remove a stale quit marker (called at TUI startup) so an old clean quit
    never masks a future crash."""
    try:
        _clean_exit_file().unlink()
    except OSError:
        pass


def write_clean_exit() -> None:
    """Mark this shutdown as deliberate so the guardian does NOT relaunch the TUI."""
    try:
        _clean_exit_file().write_text("1", encoding="utf-8")
    except OSError:
        pass


def ensure_running() -> bool:
    """Spawn the guardian (detached) if it's enabled and not already alive.
    Returns True if a guardian is (now) running. Safe to call repeatedly."""
    if not read_enabled():
        return False
    if guardian_alive():
        return True
    return _spawn_guardian()


def stop_guardian() -> bool:
    """Terminate the running guardian and drop its pidfile (used by the OFF
    toggle). Does not touch the server or TUI."""
    pid = guardian_pid()
    ok = _terminate(pid) if pid else False
    try:
        _gpid_file().unlink()
    except OSError:
        pass
    return ok


def _spawn_guardian() -> bool:
    """Launch ``python -m webagent.guardian`` as a detached, windowless process
    that survives this (the TUI) process exiting."""
    try:
        _glog_file().parent.mkdir(parents=True, exist_ok=True)
        logf: object = open(_glog_file(), "ab")
    except OSError:
        logf = subprocess.DEVNULL
    kwargs: dict = dict(cwd=str(data_dir()), stdin=subprocess.DEVNULL,
                        stdout=logf, stderr=subprocess.STDOUT, close_fds=True)
    if _IS_WIN:
        kwargs["creationflags"] = _NO_WINDOW | _NEW_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, "-m", "webagent.guardian"], **kwargs)
        return True
    except OSError as e:
        _log(f"failed to spawn guardian: {e}")
        return False
    finally:
        if logf is not subprocess.DEVNULL:
            try:
                logf.close()  # type: ignore[union-attr]
            except OSError:
                pass


# ── the supervisor itself ─────────────────────────────────────────────────────
class Guardian:
    """One synchronous supervision loop. Runs in the detached guardian process."""

    def __init__(self) -> None:
        self._seen_tui = False
        self._srv_launched_at = 0.0
        self._srv_restart_times: list[float] = []
        self._srv_pause_logged = 0.0
        self._last_tui_relaunch = 0.0

    # ── singleton ─────────────────────────────────────────────────────────
    def _claim_singleton(self) -> bool:
        """Atomically claim the pidfile. False if a live guardian already owns it."""
        path = _gpid_file()
        for _ in range(2):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                other = _read_pid(path)
                if other and other != os.getpid() and _pid_alive(other):
                    return False                       # a live guardian owns it
                try:
                    os.unlink(str(path))               # stale → reclaim
                except OSError:
                    return False
                continue
            except OSError:
                return False
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pid": os.getpid(), "port": PORT}, f)
            return True
        return False

    def _still_owner(self) -> bool:
        """Per-tick ownership re-check — converges any rare duplicate to one."""
        owner = _read_pid(_gpid_file())
        if owner == os.getpid():
            return True
        if owner and _pid_alive(owner):
            return False                               # someone else won → step down
        _write_json_atomic(_gpid_file(), {"pid": os.getpid(), "port": PORT})  # stale → reclaim
        return True

    # ── server keep-alive ─────────────────────────────────────────────────
    def _supervise_server(self, root: Optional[Path]) -> None:
        if root is None:
            return                                     # no checkout linked → nothing to keep
        if _is_healthy():
            return
        now = time.monotonic()
        if now - self._srv_launched_at < SERVER_SETTLE:
            return                                     # still booting — don't double-launch
        self._srv_restart_times = [t for t in self._srv_restart_times if now - t < 3600]
        if len(self._srv_restart_times) >= MAX_SERVER_RESTARTS_PER_HOUR:
            if now - self._srv_pause_logged > 600:
                self._srv_pause_logged = now
                _log("server crash-loop — auto-relaunch paused for now")
            return
        py = _venv_python(root)
        if py is None:
            return                                     # env not built yet
        for pid in _pids_on_port(PORT):                # clear a zombie holding the port
            _terminate(pid)
        pid = self._spawn_server(py, root)
        if pid:
            self._srv_launched_at = time.monotonic()
            self._srv_restart_times.append(self._srv_launched_at)
            _write_json_atomic(_server_pid_file(),
                               {"pid": pid, "port": PORT, "project": str(root)})
            _log(f"relaunched the web server (pid {pid})")

    def _spawn_server(self, py: Path, root: Path) -> int:
        try:
            logf: object = open(_server_log_file(), "ab")
        except OSError:
            logf = subprocess.DEVNULL
        kwargs: dict = dict(cwd=str(root), stdout=logf, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, close_fds=True)
        if _IS_WIN:
            kwargs["creationflags"] = _NO_WINDOW | _NEW_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen([str(py), "run.py"], **kwargs)
            return proc.pid
        except OSError as e:
            _log(f"server launch failed: {e}")
            return 0
        finally:
            if logf is not subprocess.DEVNULL:
                try:
                    logf.close()  # type: ignore[union-attr]
                except OSError:
                    pass

    # ── TUI keep-alive ────────────────────────────────────────────────────
    def _supervise_tui(self) -> None:
        pid = _read_pid(_tpid_file())
        if pid and _pid_alive(pid):
            self._seen_tui = True
            return
        if not self._seen_tui and pid == 0:
            return                                     # no TUI has ever checked in — nothing to revive
        if _clean_exit_file().exists():                # deliberate quit → respect it
            clear_clean_exit()
            self._seen_tui = False
            _log("TUI quit cleanly — not relaunching")
            return
        now = time.monotonic()
        if now - self._last_tui_relaunch < TUI_RELAUNCH_GAP:
            return                                     # already relaunched recently; let it write its pid
        self._last_tui_relaunch = now
        self._relaunch_tui()

    def _relaunch_tui(self) -> None:
        """Open a fresh console running the TUI. ``sys.executable`` is the TUI's
        own venv python (we were spawned with it)."""
        args = [sys.executable, "-m", "webagent"]
        kwargs: dict = dict(cwd=str(data_dir()), close_fds=True)
        try:
            if _IS_WIN:
                kwargs["creationflags"] = _NEW_CONSOLE | _NEW_GROUP
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen(args, **kwargs)
            _log("relaunched the TUI after an unexpected exit")
        except OSError as e:
            _log(f"TUI relaunch failed: {e}")

    # ── loop ──────────────────────────────────────────────────────────────
    def run(self) -> int:
        if not self._claim_singleton():
            return 0                                    # another guardian already owns the post
        _log(f"guardian started (pid {os.getpid()})")
        time.sleep(INITIAL_GRACE)                       # let the TUI's own autostart go first
        try:
            while True:
                if not self._still_owner():
                    _log("another guardian took ownership — exiting")
                    return 0
                if not read_enabled():
                    _log("disabled via the off switch — exiting")
                    return 0
                try:
                    root = TuiConfig.load().project_dir()
                    self._supervise_server(root)
                    self._supervise_tui()
                except Exception as e:                  # one bad tick must never kill the guardian
                    _log(f"tick error: {type(e).__name__}: {e}")
                time.sleep(POLL_SECONDS)
        finally:
            if _read_pid(_gpid_file()) == os.getpid():
                try:
                    _gpid_file().unlink()
                except OSError:
                    pass
            _log("guardian stopped")


def main(argv: Optional[list[str]] = None) -> int:
    return Guardian().run()


if __name__ == "__main__":
    raise SystemExit(main())

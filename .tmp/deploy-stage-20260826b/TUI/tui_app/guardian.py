"""The external **keep-alive guardian** — a supervisor that outlives the TUI.

The Server-Manager TUI (``python -m tui_app``) auto-starts the WebAgent web
server and watches it via the *in-process* ``watchdog.py``. But that watchdog is
a TUI worker: it dies the instant the TUI dies, which is exactly when the server
it launched is most likely to fall over too. This module is the answer — a
**separate process**, spawned by the TUI but fully detached from it, that keeps
*both* alive:

* **the web server** — if ``/health`` stops answering and a checkout is linked,
  relaunch ``run.py`` from the *project's* venv (with crash-loop backoff);
* **one server only** — when the server is healthy, kill any *other* WebAgent
  ``run.py`` tree on the machine besides the one actually serving port 8080, so a
  second launcher (e.g. a stray ``WebAgent.bat``) can't leave a duplicate server
  running its own background loops against the same database. The guardian adopts
  whichever process is serving (it never kills the live one);
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
_FAIL_LIMIT = 36             # consecutive launch failures before giving up (~3 min)
HEALTH_TIMEOUT = 4.0         # per-probe HTTP timeout
INITIAL_GRACE = 8.0          # let the TUI's own autostart begin before we act
HEALTH_WAIT_SECONDS = 90.0   # after spawning, how long to wait for /health before
                             # declaring "not yet responding". MUST exceed the real
                             # cold-boot time (full-app import + plugin discovery has
                             # been measured at ~45s on Windows); too short makes the
                             # guardian cry wolf on a server that is merely still booting
                             # and risks a duplicate launch colliding with the live one.
SERVER_SETTLE = 90.0         # after launching the server, give it this long to boot
                             # before considering another launch (cold boot is slow).
                             # Kept >= HEALTH_WAIT_SECONDS so a slow-but-fine boot is
                             # never relaunched on top of itself.
TUI_RELAUNCH_GAP = 30.0      # min seconds between TUI relaunches (crash-loop guard +
                             # covers the window before the new TUI writes its pid)
MAX_SERVER_RESTARTS_PER_HOUR = 5
DORMANT_RETRY_SECONDS = 300.0  # when the server can't be launched at all (venv missing,
                             # spawn keeps failing), the guardian used to EXIT — which
                             # permanently ended keep-alive after a transient hiccup (e.g.
                             # the venv briefly gone mid-`uv sync`). Instead it now goes
                             # DORMANT: stays alive, keeps supervising the TUI, and retries
                             # the server at this slow cadence until it comes back.


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


def _gfail_file() -> Path:
    """Marker file the guardian writes when it gives up on launching the server.
    The TUI reads this to show why the guardian is dead or dormant."""
    return data_dir() / "guardian.fail"


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
    """The WebAgent checkout's own venv interpreter (for running ``run.py``)."""
    for venv_dir in (".venv", "venv", "venv313"):
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


def _proc_table() -> list[tuple[int, int, str]]:
    """Best-effort ``[(pid, ppid, cmdline)]`` for every process (windowless).
    Used to identify duplicate WebAgent server trees. Returns ``[]`` on failure —
    callers MUST treat an empty table as "don't know" and reap nothing."""
    rows: list[tuple[int, int, str]] = []
    try:
        if _IS_WIN:
            ps = ("Get-CimInstance Win32_Process | ForEach-Object { "
                  "\"$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.CommandLine)\" }")
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW,
            ).stdout or ""
            for ln in out.splitlines():
                pid_s, _, rest = ln.partition("\t")
                ppid_s, _, cmd = rest.partition("\t")
                if pid_s.strip().isdigit():
                    rows.append((int(pid_s), int(ppid_s) if ppid_s.strip().isdigit() else 0,
                                 cmd.strip()))
        else:
            proc_root = Path("/proc")
            if proc_root.is_dir():
                for d in proc_root.iterdir():
                    if not d.name.isdigit():
                        continue
                    try:
                        cmd = (d / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                            "utf-8", "replace").strip()
                        # /proc/<pid>/stat: ppid is field 4, but comm (field 2) may
                        # contain spaces/parens — split on the last ')' to be safe.
                        stat = (d / "stat").read_text()
                        after = stat.rsplit(")", 1)[1].split()
                        ppid = int(after[1]) if len(after) > 1 else 0
                    except (OSError, ValueError, IndexError):
                        continue
                    rows.append((int(d.name), ppid, cmd))
    except (OSError, subprocess.SubprocessError):
        return []
    return rows


def _is_server_proc(cmdline: str) -> bool:
    """True if a command line looks like a WebAgent **web server** (``run.py`` /
    ``app.main``) — and NOT the TUI/guardian or a scanner shell. Deliberately
    conservative: a false positive here could kill the wrong process."""
    c = (cmdline or "").lower()
    if not c:
        return False
    if "webagent.guardian" in c or "tui_app.guardian" in c or "-m webagent" in c or "-m tui_app" in c:   # the supervisor / the TUI
        return False
    if "powershell" in c or "get-ciminstance" in c or "tasklist" in c:  # scanners
        return False
    return ("run.py" in c) or ("app.main" in c)


def _server_tree(listener_pids: set[int], runpy_pids: set[int],
                 parents: dict[int, int]) -> set[int]:
    """The set of WebAgent server pids that make up the **serving** tree: start
    from whoever is LISTENING on the port and walk up to run.py ancestors and down
    to run.py children (through run.py nodes only). That tree is "our own" server —
    everything else in ``runpy_pids`` is a duplicate. Seeding from the live listener
    (not a remembered pid) means we adopt whatever is actually serving and can never
    pick the live server as the thing to kill."""
    own: set[int] = set()
    children: dict[int, set[int]] = {}
    for pid in runpy_pids:
        ppid = parents.get(pid, 0)
        if ppid in runpy_pids:
            children.setdefault(ppid, set()).add(pid)
    work = [p for p in listener_pids if p in runpy_pids]
    while work:
        pid = work.pop()
        if pid in own:
            continue
        own.add(pid)
        ppid = parents.get(pid, 0)                 # up: run.py parent
        if ppid in runpy_pids and ppid not in own:
            work.append(ppid)
        for child in children.get(pid, ()):        # down: run.py children
            if child not in own:
                work.append(child)
    return own


def _foreign_server_pids(listener_pids: set[int], runpy_pids: set[int],
                         parents: dict[int, int]) -> set[int]:
    """Server pids that are NOT part of the serving tree → duplicates to reap.
    Fail-safe: if we can't identify the serving tree (no listener among the server
    procs), return an empty set so the guardian kills nothing."""
    own = _server_tree(listener_pids, runpy_pids, parents)
    if not own:
        return set()
    return set(runpy_pids) - own


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
    # Clear the fail marker so a dead guardian doesn't leave stale warnings.
    try:
        _gfail_file().unlink()
    except OSError:
        pass
    return ok


def read_fail_marker() -> Optional[dict]:
    """Read the guardian's failure marker if present.
    Returns None if the guardian is fine or hasn't failed."""
    data = _read_json(_gfail_file())
    if isinstance(data, dict):
        return data
    return None


def _spawn_guardian() -> bool:
    """Launch ``python -m tui_app.guardian`` as a detached, windowless process
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
        subprocess.Popen([sys.executable, "-m", "tui_app.guardian"], **kwargs)
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
        self._consecutive_fails = 0
        self._last_reap = 0.0
        self._reap_clean_streak = 0                   # consecutive scans that found no duplicate
        self._last_listeners: set[int] = set()        # port-8080 owners seen at the last scan
        self._dormant_reason: Optional[str] = None   # set when the server can't be launched
        self._dormant_retry_at = 0.0                 # monotonic time of the next slow retry

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
            self._consecutive_fails = 0                # success resets the counter
            self._clear_dormant()                      # server is back → leave slow-retry mode
            return
        now = time.monotonic()
        if now - self._srv_launched_at < SERVER_SETTLE:
            return                                     # still booting — don't double-launch
        if self._dormant_reason is not None:
            # We've hit a hard launch failure (venv missing / spawn keeps failing) and are
            # in slow-retry mode. Attempt again only every DORMANT_RETRY_SECONDS instead of
            # every tick — but NEVER exit, so TUI supervision keeps running and the server
            # comes back the moment the underlying problem (e.g. a mid-update venv) resolves.
            if now < self._dormant_retry_at:
                return
            self._dormant_retry_at = now + DORMANT_RETRY_SECONDS
        self._srv_restart_times = [t for t in self._srv_restart_times if now - t < 3600]
        if len(self._srv_restart_times) >= MAX_SERVER_RESTARTS_PER_HOUR:
            if now - self._srv_pause_logged > 600:
                self._srv_pause_logged = now
                _log("server crash-loop — auto-relaunch paused for now")
            return
        py = _venv_python(root)
        if py is None:
            self._consecutive_fails += 1
            if self._consecutive_fails >= _FAIL_LIMIT:
                self._go_dormant(
                    f"venv not found (looked for .venv/bin/python, venv/bin/python in {root})")
            return
        for pid in _pids_on_port(PORT):                # clear a zombie holding the port
            _terminate(pid)
        pid = self._spawn_server(py, root)
        if pid:
            self._consecutive_fails = 0                # success resets the counter
            self._clear_dormant()                      # a spawn succeeded → leave slow-retry mode
            self._reap_clean_streak = 0                # fresh launch → re-arm fast duplicate scans
            self._srv_launched_at = time.monotonic()
            self._srv_restart_times.append(self._srv_launched_at)
            # Wait for the server to start listening, then track the REAL
            # listener PID (not the uv stub — .venv/Scripts/python.exe is a
            # 45KB shim that spawns the real Python as a child).  Tracking
            # the stub means kill/status follow a zombie while the real
            # server runs unmonitored.
            for _ in range(int(HEALTH_WAIT_SECONDS / 0.5)):  # 0.5s per probe
                time.sleep(0.5)
                if _is_healthy():
                    listeners = _pids_on_port(PORT)
                    track = next(iter(listeners)) if listeners else pid
                    _write_json_atomic(_server_pid_file(),
                                       {"pid": track, "port": PORT, "project": str(root)})
                    _log(f"relaunched the web server (tracked pid {track}, spawned {pid})")
                    return
            _write_json_atomic(_server_pid_file(),
                               {"pid": pid, "port": PORT, "project": str(root)})
            _log(f"relaunched the web server (pid {pid}) — /health not yet responding")
        else:
            self._consecutive_fails += 1
            if self._consecutive_fails >= _FAIL_LIMIT:
                self._go_dormant(
                    f"server launch failed {self._consecutive_fails} times in a row")

    # ── dormant slow-retry (never exit on a hard launch failure) ──────────
    def _go_dormant(self, reason: str) -> None:
        """Enter slow-retry mode after too many hard launch failures. Records the
        reason in the fail marker (so the TUI can show WHY the server is down) and
        schedules the next attempt — but keeps the guardian process alive so it
        goes on supervising the TUI and re-launches the server the moment the
        underlying problem clears. Replaces the old ``raise SystemExit`` which
        permanently killed keep-alive after a transient hiccup."""
        first = self._dormant_reason is None
        self._dormant_reason = reason
        self._dormant_retry_at = time.monotonic() + DORMANT_RETRY_SECONDS
        _write_json_atomic(_gfail_file(),
                           {"reason": reason, "consecutive_fails": self._consecutive_fails,
                            "dormant": True})
        if first:
            _log(f"cannot launch the server ({reason}) — entering slow-retry mode "
                 f"(every {int(DORMANT_RETRY_SECONDS)}s); guardian stays alive to keep "
                 "supervising the TUI and will relaunch when it can")

    def _clear_dormant(self) -> None:
        """Leave slow-retry mode once the server is launchable/healthy again: drop
        the fail marker and reset the failure counter so the TUI stops showing the
        stale 'guardian gave up' warning."""
        if self._dormant_reason is None:
            return
        self._dormant_reason = None
        self._consecutive_fails = 0
        try:
            _gfail_file().unlink()
        except OSError:
            pass
        _log("server is launchable again — left slow-retry mode")

    # ── sole-authority over port 8080 ─────────────────────────────────────
    REAP_INTERVAL = 30.0     # base gap between duplicate-server scans (churny state)
    REAP_BACKOFF_STEPS = 3   # widen up to REAP_INTERVAL*(1+STEPS) once proven stable (→ 120s)

    def _reap_interval(self) -> float:
        """How long to wait before the next duplicate scan. The scan itself is the
        one genuinely heavy op in the loop — a full process-table enumeration
        (PowerShell ``Get-CimInstance`` on Windows). Running it every 30s forever is
        wasteful when a single clean server has been up for hours, so once several
        scans in a row find no duplicate we widen the gap (30→60→90→120s). ANY churn
        re-arms the fast cadence by zeroing ``_reap_clean_streak``: a fresh launch
        (see _supervise_server) or the serving process changing between scans — the
        moments a duplicate/orphan can actually appear."""
        return self.REAP_INTERVAL * (1 + min(self._reap_clean_streak, self.REAP_BACKOFF_STEPS))

    def _reap_foreign_servers(self) -> None:
        """Enforce "one server, and it's the one I supervise": kill any *other*
        WebAgent ``run.py`` tree besides whichever one is actually serving 8080.

        Runs ONLY when the server is healthy and past its boot-settle window, so
        the serving tree is stable — a still-booting server (not yet listening)
        is never mistaken for a duplicate. We adopt whatever is serving (seed from
        the live listener), so this can never kill the live server, and it converges
        even if some other launcher started it. Fail-safe at every step: any missing
        signal → reap nothing.

        The gate uses an ADAPTIVE interval (see _reap_interval): the detection logic
        is unchanged, we just pay for the heavy process-table scan less often once the
        server has proven stable, and re-arm the fast cadence the moment anything
        changes."""
        now = time.monotonic()
        if now - self._last_reap < self._reap_interval():
            return
        if now - self._srv_launched_at < SERVER_SETTLE:
            return                                     # our own launch is still booting
        if not _is_healthy():
            return                                     # no stable serving tree to anchor on
        self._last_reap = now
        listeners = _pids_on_port(PORT)                # cheap netstat pre-check
        if listeners != self._last_listeners:
            self._reap_clean_streak = 0                # serving process changed → scan promptly
            self._last_listeners = set(listeners)
        if not listeners:
            return
        table = _proc_table()                          # the heavy scan — now gated by the streak
        if not table:
            return                                     # can't see processes → don't guess
        parents = {pid: ppid for pid, ppid, _ in table}
        runpy = {pid for pid, _, cmd in table if _is_server_proc(cmd)}
        runpy.discard(os.getpid())                     # never count ourselves
        foreign = _foreign_server_pids(listeners, runpy, parents)
        if not foreign:
            self._reap_clean_streak += 1               # nothing to reap → allow backing off
            return
        self._reap_clean_streak = 0                    # a duplicate appeared → stay on fast cadence
        for pid in sorted(foreign):
            if _terminate(pid):
                _log(f"reaped duplicate web-server process (pid {pid}) — not the one serving :{PORT}")
        # Adopt the serving tree as the tracked server so the TUI's status/stop
        # follow the real one: record its top-most run.py ancestor.
        own = _server_tree(listeners, runpy, parents)
        root = None
        for pid in own:
            if parents.get(pid, 0) not in own:         # the tree's run.py root
                root = pid
                break
        if root:
            cur = _read_pid(_server_pid_file())
            if cur != root:
                _write_json_atomic(_server_pid_file(),
                                   {"pid": root, "port": PORT, "adopted": True})

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
                    self._reap_foreign_servers()       # one server only — kill rival trees
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

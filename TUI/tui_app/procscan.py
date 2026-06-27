"""Find running webAgent server processes (dependency-free, cross-platform).

Used on open to show the user every webAgent server instance on the machine and
to spot **stale/zombie** ones (a process holding port 8080 that no longer serves
``/health``, or a leftover ``run.py`` python from a crashed launch) so the manager
can offer to clean them up.

Two signals are merged:
  1. PIDs **listening on port 8080** (the server port) — the live instance(s).
  2. Python processes whose command line mentions ``run.py`` / ``app.main`` — the
     webAgent launcher, which also catches one mid-startup or bound elsewhere.

Everything is best-effort: any probe that fails is skipped, never raised. Returns
a list of ``{pid, cmdline, on_8080}`` dicts. Classification (tracked vs untracked
vs zombie) and the /health check live in the app, which has the async loop and the
manager-tracked PID from ``tools/server``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_IS_WIN = sys.platform == "win32"
_NO_WINDOW = {"creationflags": 0x08000000} if _IS_WIN else {}  # CREATE_NO_WINDOW
_MARKERS = ("run.py", "app.main:app", "app.main")


def _run(args: list[str], timeout: float = 6.0) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                             **_NO_WINDOW)
        return out.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


# ── port-8080 listeners ──────────────────────────────────────────────────────
def listeners_on(port: int = 8080) -> set[int]:
    pids: set[int] = set()
    if _IS_WIN:
        for line in _run(["netstat", "-ano", "-p", "TCP"]).splitlines():
            parts = line.split()
            if len(parts) >= 5 and "LISTENING" in parts and f":{port}" in parts[1]:
                try:
                    pids.add(int(parts[-1]))
                except ValueError:
                    pass
        return pids
    # POSIX: prefer ss, fall back to lsof.
    ss = _run(["ss", "-ltnpH"])
    if ss:
        for line in ss.splitlines():
            if f":{port}" not in line:
                continue
            # ...users:(("python",pid=1234,fd=7))
            marker = "pid="
            i = line.find(marker)
            while i != -1:
                j = i + len(marker)
                num = ""
                while j < len(line) and line[j].isdigit():
                    num += line[j]
                    j += 1
                if num:
                    pids.add(int(num))
                i = line.find(marker, j)
        if pids:
            return pids
    for line in _run(["lsof", "-iTCP:%d" % port, "-sTCP:LISTEN", "-P", "-n", "-Fp"]).splitlines():
        if line.startswith("p"):
            try:
                pids.add(int(line[1:]))
            except ValueError:
                pass
    return pids


# ── webAgent python processes (by command line) ──────────────────────────────
def _runpy_procs() -> dict[int, str]:
    procs: dict[int, str] = {}
    if _IS_WIN:
        # PowerShell CIM is reliable for full command lines (wmic is deprecated).
        ps = ("Get-CimInstance Win32_Process | "
              "Where-Object { $_.CommandLine -match 'run\\.py|app\\.main' } | "
              "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }")
        out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=10.0)
        for line in out.splitlines():
            pid_str, _, cmd = line.partition("\t")
            pid_str = pid_str.strip()
            if pid_str.isdigit():
                procs[int(pid_str)] = cmd.strip()
        return procs
    # POSIX: scan /proc/<pid>/cmdline.
    proc_root = Path("/proc")
    if proc_root.is_dir():
        for d in proc_root.iterdir():
            if not d.name.isdigit():
                continue
            try:
                raw = (d / "cmdline").read_bytes()
            except OSError:
                continue
            cmd = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
            if cmd and any(m in cmd for m in _MARKERS):
                procs[int(d.name)] = cmd
        return procs
    # macOS / other: ps with full args.
    for line in _run(["ps", "-axo", "pid=,command="]).splitlines():
        line = line.strip()
        pid_str, _, cmd = line.partition(" ")
        if pid_str.isdigit() and any(m in cmd for m in _MARKERS):
            procs[int(pid_str)] = cmd.strip()
    return procs


def process_age(pid: int) -> str:
    """Human-readable age for a PID, e.g. '00:12:34' or '3d 02:15'.
    Returns '?' on any failure."""
    try:
        import subprocess
        out = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5,
                             **_NO_WINDOW).stdout.strip()
        if out:
            return out
    except Exception:
        pass
    return "?"


def process_stat(pid: int) -> str:
    """Process state letter from /proc (Linux) or '?'."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        # field 3 is the state (after pid, comm)
        return stat.split()[2] if len(stat.split()) > 2 else "?"
    except Exception:
        return "?"


def scan_webagent_processes(port: int = 8080) -> list[dict]:
    """Merge port-8080 listeners and webAgent python processes into one list:
    ``[{pid, cmdline, on_8080, age, stat, cmd_short}]`` sorted by pid.
    Never raises."""
    try:
        on_port = listeners_on(port)
    except Exception:
        on_port = set()
    try:
        by_cmd = _runpy_procs()
    except Exception:
        by_cmd = {}
    pids = set(on_port) | set(by_cmd.keys())
    out = []
    for pid in sorted(pids):
        cmd = by_cmd.get(pid, "")
        # shorten the command line for display
        cmd_short = cmd
        if "python" in cmd_short or "python3" in cmd_short:
            # try to show just the meaningful tail
            for marker in ("run.py", "-m tui_app", "-m webagent", "tui_app", "webagent"):
                idx = cmd_short.find(marker)
                if idx != -1:
                    cmd_short = cmd_short[idx:]
                    break
        if len(cmd_short) > 50:
            cmd_short = cmd_short[:47] + "..."
        out.append({
            "pid": pid,
            "cmdline": cmd,
            "cmd_short": cmd_short,
            "on_8080": pid in on_port,
            "age": process_age(pid),
            "stat": process_stat(pid),
        })
    return out

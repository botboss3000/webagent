"""Dependency-free system metrics for the watchdog: disk, memory, CPU, and a raw
port check.

The rest of the manager deliberately avoids ``psutil`` (see ``tools/server.py``),
so this module gathers everything from the standard library and the OS:

* **disk** — ``shutil.disk_usage`` (reliable everywhere).
* **memory** — Windows ``GlobalMemoryStatusEx`` via ctypes; Linux ``/proc/meminfo``.
* **host CPU** — a *delta* sample between calls: Windows ``GetSystemTimes``, Linux
  ``/proc/stat``; elsewhere the 1-min load average as a proxy. The first call has
  no baseline and returns ``None`` — the watchdog's second tick onward has a real
  number (averaged over the tick interval, which is exactly what we want).
* **process memory** — Windows ctypes ``GetProcessMemoryInfo``; Linux
  ``/proc/<pid>/status``; macOS ``ps``.
* **port_in_use** — a raw TCP connect, to tell a zombie holding port 8080 (but not
  serving ``/health``) apart from a truly free port.

Every function returns ``None`` (or a partial dict) on any failure and NEVER
raises — a metric we can't read must not break monitoring.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from typing import Any, Optional

_IS_WIN = sys.platform == "win32"
_IS_MAC = sys.platform == "darwin"
_NO_WINDOW = {"creationflags": 0x08000000} if _IS_WIN else {}


# ── disk ─────────────────────────────────────────────────────────────────────
def disk_usage(path: str) -> Optional[dict[str, Any]]:
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    pct = round(u.used / u.total * 100, 1) if u.total else None
    return {"total": u.total, "used": u.used, "free": u.free, "percent": pct}


# ── system memory ────────────────────────────────────────────────────────────
def system_memory() -> Optional[dict[str, Any]]:
    if _IS_WIN:
        return _win_system_memory()
    if sys.platform.startswith("linux"):
        return _linux_system_memory()
    return None


def _win_system_memory() -> Optional[dict[str, Any]]:
    try:
        import ctypes

        class _MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        ms = _MS()
        ms.dwLength = ctypes.sizeof(_MS)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            return None
        return {"total": ms.ullTotalPhys, "available": ms.ullAvailPhys,
                "percent": float(ms.dwMemoryLoad)}
    except Exception:
        return None


def _linux_system_memory() -> Optional[dict[str, Any]]:
    try:
        info: dict[str, int] = {}
        for line in open("/proc/meminfo", encoding="utf-8"):
            k, _, rest = line.partition(":")
            info[k.strip()] = int(rest.strip().split()[0]) * 1024  # kB → bytes
        total = info.get("MemTotal")
        avail = info.get("MemAvailable", info.get("MemFree"))
        if not total:
            return None
        pct = round((total - (avail or 0)) / total * 100, 1)
        return {"total": total, "available": avail, "percent": pct}
    except (OSError, ValueError):
        return None


# ── host CPU (delta sample between calls) ────────────────────────────────────
_cpu_prev: Optional[tuple[int, int]] = None  # (idle, total) from the previous call


def host_cpu_percent() -> Optional[float]:
    """Busy-% since the previous call (None on the first call / unsupported)."""
    global _cpu_prev
    sample = _cpu_sample()
    if sample is None:
        # Fall back to load average where we can't sample CPU times.
        try:
            return round(os.getloadavg()[0], 2)  # a load number, not a %
        except (OSError, AttributeError):
            return None
    idle, total = sample
    if _cpu_prev is None:
        _cpu_prev = sample
        return None
    didle, dtotal = idle - _cpu_prev[0], total - _cpu_prev[1]
    _cpu_prev = sample
    if dtotal <= 0:
        return None
    return round((1 - didle / dtotal) * 100, 1)


def _cpu_sample() -> Optional[tuple[int, int]]:
    if _IS_WIN:
        try:
            import ctypes

            def _ft():
                return ctypes.c_ulonglong()

            idle, kern, user = _ft(), _ft(), _ft()
            if not ctypes.windll.kernel32.GetSystemTimes(
                    ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
                return None
            # kernel time INCLUDES idle; total = kernel + user.
            return idle.value, kern.value + user.value
        except Exception:
            return None
    if sys.platform.startswith("linux"):
        try:
            parts = open("/proc/stat", encoding="utf-8").readline().split()[1:]
            vals = [int(x) for x in parts]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
            return idle, sum(vals)
        except (OSError, ValueError, IndexError):
            return None
    return None


# ── process memory ───────────────────────────────────────────────────────────
def process_memory(pid: int) -> Optional[int]:
    """Resident memory of a process in bytes, or None if unavailable."""
    if not pid:
        return None
    if _IS_WIN:
        return _win_process_rss(pid)
    if sys.platform.startswith("linux"):
        try:
            for line in open(f"/proc/{pid}/status", encoding="utf-8"):
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError):
            return None
        return None
    # macOS / other POSIX: ps RSS is in kB.
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        v = (out.stdout or "").strip()
        return int(v) * 1024 if v.isdigit() else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _win_process_rss(pid: int) -> Optional[int]:
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_INFORMATION = 0x0400
        PROCESS_VM_READ = 0x0010
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            return None

        class _PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        try:
            pmc = _PMC()
            pmc.cb = ctypes.sizeof(_PMC)
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
            return int(pmc.WorkingSetSize) if ok else None
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return None


# ── port check ───────────────────────────────────────────────────────────────
def port_in_use(port: int = 8080, host: str = "127.0.0.1",
                fallback_python: str = "") -> bool:
    """True if something is listening on the TCP port (a raw connect succeeds).

    On systems where the socket stack is broken (e.g. Python 3.14 with a
    corrupted Winsock provider), falls back to a subprocess call via
    *fallback_python* (e.g. the WebAgent's venv Python 3.12)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            return s.connect_ex((host, port)) == 0
    except OSError:
        if fallback_python:
            return _port_in_use_fallback(port, host, fallback_python)
        return False


def _port_in_use_fallback(port: int, host: str, python_exe: str) -> bool:
    """Check whether the port is in use via a subprocess call."""
    import subprocess
    try:
        code = (
            f"import socket\n"
            f"with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:\n"
            f"  s.settimeout(2.0)\n"
            f"  print('yes' if s.connect_ex(('{host}', {port})) == 0 else 'no')\n"
        )
        result = subprocess.run(
            [python_exe, "-c", code],
            capture_output=True, text=True, timeout=5.0,
        )
        return result.stdout.strip() == "yes"
    except Exception:
        return False


# ── aggregate ────────────────────────────────────────────────────────────────
def fmt_bytes(n: Optional[int]) -> str:
    if not n:
        return "n/a"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def gather(disk_path: str, pid: int = 0) -> dict[str, Any]:
    """One snapshot of host + (optional) server-process resources for reporting."""
    return {
        "disk": disk_usage(disk_path),
        "memory": system_memory(),
        "cpu_percent": host_cpu_percent(),
        "process_rss": process_memory(pid) if pid else None,
    }

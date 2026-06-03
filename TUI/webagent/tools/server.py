"""Local server lifecycle — start / stop / restart / status / logs.

The webAgent server is one launcher (``run.py``) bound to port 8080, with a
``/health`` endpoint. These tools run it from the linked checkout's own virtual
environment as a **detached** background process so it outlives a single tool
call, recording the PID + a captured log in the manager's per-user data dir
(never inside the repo). Local-only by design — no remote/VM control.

Cross-platform process handling (no psutil dependency): POSIX uses a new session
+ ``SIGTERM``; Windows uses a detached process group + ``taskkill``.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from ..config import data_dir
from ..env_probe import server_health
from .base import WRITES_DISABLED_MSG, ToolContext

PORT = 8080
_IS_WIN = sys.platform == "win32"
# Keep helper console tools (tasklist/taskkill) from flashing a window on Windows.
_NO_WINDOW = {"creationflags": 0x08000000} if _IS_WIN else {}  # CREATE_NO_WINDOW


# ── state files (per-user data dir, not the repo) ─────────────────────────────
def _pid_file() -> Path:
    return data_dir() / "server.pid"


def _log_file() -> Path:
    return data_dir() / "server.log"


def _read_pidinfo() -> Optional[dict]:
    try:
        return json.loads(_pid_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_pidinfo(info: dict) -> None:
    p = _pid_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(info), encoding="utf-8")


def _clear_pidinfo() -> None:
    try:
        _pid_file().unlink()
    except OSError:
        pass


# ── process primitives (cross-platform, testable in isolation) ────────────────
def _venv_python(project_root: Path) -> Optional[Path]:
    """The checkout's own venv interpreter, or None if the env isn't built yet."""
    for venv_dir in (".venv", "venv"):
        cand = (project_root / venv_dir / ("Scripts" if _IS_WIN else "bin")
                / ("python.exe" if _IS_WIN else "python"))
        # The symlink may be broken on the host (e.g. inside proot), but the
        # venv itself is valid — treat an existing symlink as "found" so the
        # manager can still discover the env even when python lives elsewhere.
        if cand.exists() or cand.is_symlink():
            return cand
    return None


def _is_termux() -> bool:
    """Detect whether we're running inside Termux (Android)."""
    return bool(os.environ.get("TERMUX_VERSION")) or os.path.exists("/data/data/com.termux")


def _proot_distro() -> Optional[str]:
    """Return the proot-distro name to use, or None if not available."""
    for d in ("ubuntu",):
        if os.path.exists(f"/data/data/com.termux/files/usr/var/lib/proot-distro/containers/{d}"):
            return d
    return None


def _proot_project_path(host_path: Path) -> str:
    """Translate a host path to the equivalent path inside the proot container.

    Termux binds /data/data/com.termux/files/home -> /data/data/com.termux/files/home
    (identity mount for the Termux home), so host paths inside the user's home
    are the same inside the proot.  The webAgent clone lives on the Termux
    filesystem, not under the proot's own rootfs.
    """
    return str(host_path)


def _proot_venv_activate(project_root: Path) -> Optional[str]:
    """Find a venv activation script inside the proot container."""
    for venv_dir in (".venv", "venv"):
        act = project_root / venv_dir / "bin" / "activate"
        if act.exists():
            return f"{venv_dir}/bin/activate"
    return None


def _spawn_detached_proot(project_root: Path, log_path: Path) -> int:
    """Launch the server inside a proot-distro container (Android/Termux).

    Binds the host project to /root/webagent inside the proot so that venv
    symlinks (which point to the proot's own python3.11) resolve correctly.
    """
    distro = _proot_distro()
    if distro is None:
        raise RuntimeError("No proot-distro container found (expected 'ubuntu').")
    act = _proot_venv_activate(project_root)
    if act is None:
        raise RuntimeError(
            "No venv found for proot (looked for .venv/bin/activate and venv/bin/activate)."
        )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logf = open(log_path, "ab")
    proot_bin = "/data/data/com.termux/files/usr/bin/proot-distro"
    in_proot_root = "/root/webagent"
    args = [
        proot_bin, "login", distro,
        "--bind", f"{project_root}:{in_proot_root}",
        "--", "bash", "-c",
        f"cd '{in_proot_root}' && source {act} && exec python run.py"
    ]
    try:
        proc = subprocess.Popen(
            args,
            stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    finally:
        logf.close()
    return proc.pid


def _pid_alive(pid: int) -> bool:
    if _IS_WIN:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, **_NO_WINDOW)
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _spawn_detached(args: list[str], cwd: Path, log_path: Path) -> int:
    """Launch a fully detached background process writing to ``log_path``."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logf = open(log_path, "ab")  # noqa: SIM115 — handed to the child, closed below
    kwargs: dict = dict(cwd=str(cwd), stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    if _IS_WIN:
        # CREATE_NO_WINDOW (not DETACHED_PROCESS): both keep the server alive
        # after the manager exits, but DETACHED_PROCESS still lets a console
        # subsystem app (python/uvicorn) pop its own visible window — so the
        # background server showed up as a stray second window. CREATE_NO_WINDOW
        # gives it a console that is never shown, keeping it truly headless.
        kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(args, **kwargs)
    finally:
        logf.close()
    return proc.pid


def _terminate(pid: int) -> bool:
    try:
        if _IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True, text=True, **_NO_WINDOW)
        else:
            os.kill(pid, signal.SIGTERM)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# ── tools ─────────────────────────────────────────────────────────────────────
async def server_status(ctx: ToolContext) -> str:
    health = await server_health(PORT)
    info = _read_pidinfo()
    pid_note = ""
    if info and info.get("pid"):
        alive = _pid_alive(int(info["pid"]))
        pid_note = f" (tracked pid {info['pid']}{'' if alive else ' — not running'})"
        if not alive and health == "stopped":
            _clear_pidinfo()
    if health == "running":
        return f"[server] running at http://localhost:{PORT}{pid_note}. UI: /index.html, docs: /docs."
    if health == "stopped":
        return f"[server] stopped{pid_note}."
    return f"[server] status unknown{pid_note}."


async def server_start(ctx: ToolContext) -> str:
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    if ctx.project_root is None:
        return "Error: no checkout linked — link or install one first."
    if await server_health(PORT) == "running":
        return f"[server] already running at http://localhost:{PORT}."
    runpy = ctx.project_root / "run.py"
    if not runpy.exists():
        return "Error: run.py not found in the checkout — is this a complete webAgent install?"
    try:
        if _is_termux():
            distro = _proot_distro()
            if distro is None:
                return ("Error: on Termux/Android but no proot-distro container found. "
                        "Install one: proot-distro install ubuntu")
            pid = _spawn_detached_proot(ctx.project_root, _log_file())
        else:
            py = _venv_python(ctx.project_root)
            if py is None:
                return ("Error: no virtual environment found in this checkout (.venv). "
                        "Build the environment first (setup_environment).")
            pid = _spawn_detached([str(py), "run.py"], ctx.project_root, _log_file())
    except OSError as e:
        ctx.audit("server_start", {}, False, str(e))
        return f"Error: failed to launch the server: {e}"
    _write_pidinfo({"pid": pid, "port": PORT, "project": str(ctx.project_root)})
    # Poll /health until healthy so we report a real outcome, not just "spawned".
    # A cold webAgent boot (FastAPI + uvicorn + a headless-browser import + the
    # first-run DB build) routinely takes well over 10s; the launcher waits up to
    # 60s. Wait ~30s here with a tolerant per-probe timeout so a slow-but-fine
    # startup isn't reported as a failure. (The status dot polls independently,
    # so the UI reflects "live" the moment the server answers, even before this.)
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.5)
        if await server_health(PORT, timeout=5.0) == "running":
            ctx.audit("server_start", {"pid": pid}, True, "healthy")
            return f"[server] started (pid {pid}) — healthy at http://localhost:{PORT} (UI: /index.html)."
    ctx.audit("server_start", {"pid": pid}, False, "no health within 30s")
    return (f"[server] launched (pid {pid}) but /health didn't respond within 30s. "
            "It may still be starting; check server_logs for errors.")


async def server_stop(ctx: ToolContext) -> str:
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    info = _read_pidinfo()
    if not info or not info.get("pid"):
        if await server_health(PORT) == "running":
            return (f"[server] something is on port {PORT} but the manager didn't start it, "
                    "so it has no PID to stop. Stop it where it was launched.")
        return "[server] not running (nothing to stop)."
    pid = int(info["pid"])
    ok = _terminate(pid)
    _clear_pidinfo()
    ctx.audit("server_stop", {"pid": pid}, ok, "terminated" if ok else "terminate failed")
    return f"[server] stopped (pid {pid})." if ok else f"[server] could not stop pid {pid}."


async def server_restart(ctx: ToolContext) -> str:
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    await server_stop(ctx)
    await asyncio.sleep(1.0)
    return await server_start(ctx)


async def server_logs(ctx: ToolContext, lines: int = 40) -> str:
    try:
        text = _log_file().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "[server] no captured log yet (start the server first)."
    tail = text.splitlines()[-max(1, min(lines, 400)):]
    return "[server log]\n" + ("\n".join(tail) if tail else "(empty)")

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
    cand = (project_root / ".venv" / ("Scripts" if _IS_WIN else "bin")
            / ("python.exe" if _IS_WIN else "python"))
    return cand if cand.exists() else None


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
    py = _venv_python(ctx.project_root)
    if py is None:
        return ("Error: no virtual environment found in this checkout (.venv). "
                "Build the environment first (setup_environment).")
    runpy = ctx.project_root / "run.py"
    if not runpy.exists():
        return "Error: run.py not found in the checkout — is this a complete webAgent install?"
    try:
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

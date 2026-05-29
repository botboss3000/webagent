"""Reset operations: clear DB, wipe venv, full reset.

Mirrors the destructive paths in `reset_webagent.bat`.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Iterable

from .server import ServerController


# Database-related files (mandatory wipe targets for "Clear DB")
DB_FILES = [
    "app/db/local.db",
    "app/db/local.db-journal",
    "app/db/local.db-wal",
    "app/db/local.db-shm",
    "app/db/local.db.preprompt-bak",
    "local.db",
]
DB_DIRS = [
    "visuals/users",
]


def _ts() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _exists(p: Path) -> bool:
    try:
        return p.exists() or p.is_symlink()
    except OSError:
        return False


def _backup_or_delete(src: Path, backup_dir: Path | None, log: Callable[[str], None]) -> bool:
    if not _exists(src):
        log(f"  skip (missing): {src.name}")
        return True
    if backup_dir is not None:
        rel = src.name if src.is_absolute() else str(src)
        dst = backup_dir / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            log(f"  backup: {src.name} → {dst}")
            return True
        except OSError as e:
            log(f"  FAIL backup {src.name}: {e}")
            return False
    # hard delete
    try:
        if src.is_dir() and not src.is_symlink():
            shutil.rmtree(src)
        else:
            src.unlink()
        log(f"  delete: {src.name}")
        return True
    except OSError as e:
        log(f"  FAIL delete {src.name}: {e}")
        return False


async def clear_database(
    project_dir: Path,
    controller: ServerController,
    log: Callable[[str], None],
    backup: bool = True,
) -> bool:
    """Stop server → backup-or-delete DB files → restart server."""
    log("[reset] Clearing database...")

    was_running = controller.state.status == "running"
    if controller.state.status in ("running", "starting"):
        await controller.stop()

    backup_dir = None
    if backup:
        backup_dir = project_dir / "temp" / f"reset-backup-{_ts()}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        log(f"[reset] Backup dir: {backup_dir}")

    ok = True
    for rel in DB_FILES:
        ok = _backup_or_delete(project_dir / rel, backup_dir, log) and ok
    for rel in DB_DIRS:
        ok = _backup_or_delete(project_dir / rel, backup_dir, log) and ok

    log("[reset] Database wipe complete. Restart will recreate it.")

    if was_running:
        await asyncio.sleep(0.3)
        await controller.start()

    return ok


async def reset_venv(
    project_dir: Path,
    controller: ServerController,
    log: Callable[[str], None],
) -> bool:
    """Stop server → delete .venv → run uv sync → restart server."""
    log("[reset] Wiping Python environment...")

    was_running = controller.state.status == "running"
    if controller.state.status in ("running", "starting"):
        await controller.stop()

    venv = project_dir / ".venv"
    if venv.exists():
        log(f"[reset] Removing {venv}")
        try:
            shutil.rmtree(venv)
        except OSError as e:
            log(f"[reset] FAIL to remove .venv: {e}")
            return False

    # Run uv sync
    from shutil import which
    uv = which("uv")
    if not uv:
        log("[reset] ERROR: uv not found on PATH. Install from https://docs.astral.sh/uv/")
        return False

    log(f"[reset] $ {uv} sync")
    proc = await asyncio.create_subprocess_exec(
        uv, "sync",
        cwd=str(project_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        log("  " + line.decode("utf-8", errors="replace").rstrip())

    rc = await proc.wait()
    if rc != 0:
        log(f"[reset] uv sync exited {rc}")
        return False

    log("[reset] Python environment rebuilt.")

    if was_running:
        await asyncio.sleep(0.3)
        await controller.start()

    return True


async def full_reset(
    project_dir: Path,
    controller: ServerController,
    log: Callable[[str], None],
) -> bool:
    """DB clear + venv wipe + uv sync + restart."""
    log("[reset] FULL RESET starting...")
    ok1 = await clear_database(project_dir, controller, log, backup=True)
    ok2 = await reset_venv(project_dir, controller, log)
    log(f"[reset] Full reset {'succeeded' if (ok1 and ok2) else 'completed with errors'}.")
    return ok1 and ok2

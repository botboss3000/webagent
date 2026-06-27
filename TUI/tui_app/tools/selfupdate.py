"""Self-update — the manager updating its OWN code, on request or when it finds
itself behind upstream. Two paths depending on how it's running (see selfinfo):

* **source**: back up the manager's source, then ``git pull --ff-only`` the repo
  it lives in. The new code loads on **restart**.
* **frozen exe**: fetch fresh source into the per-user data dir, rebuild the exe
  with PyInstaller, back up the current exe (timestamped), and stage the new one
  beside it. A running exe can't overwrite itself, so the swap finishes on restart
  via a tiny detached helper that waits for this process to exit, swaps the file,
  and relaunches.

Everything is gated behind the "Allow writes" toggle, every mutation is audited,
and a timestamped backup is always taken before anything is replaced. Force is
never used (``git pull`` is fast-forward only); the original is always recoverable.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from ..config import data_dir
from ..selfinfo import REPO_URL, DEFAULT_BRANCH, SelfInfo, check_self_update, gather
from . import install                       # reuse _run, _supported_python
from .base import WRITES_DISABLED_MSG, ToolContext

_IS_WIN = sys.platform == "win32"
# Heavy source trees we never copy into a source backup.
_BACKUP_IGNORE = shutil.ignore_patterns(
    ".venv", "venv", "__pycache__", ".git", "build", "dist",
    "*.egg-info", "*.exe", "*.bak.exe", ".mypy_cache", ".pytest_cache",
)


def _ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _self_dir() -> Path:
    d = data_dir() / "self-update"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _backups_dir() -> Path:
    d = data_dir() / "self-backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── status ────────────────────────────────────────────────────────────────────
async def self_status(ctx: ToolContext) -> str:
    """Report how the manager is running and whether it's behind upstream. Read-only."""
    info = gather()
    st = await check_self_update(info)
    lines = [
        f"[self] mode: {info.mode} (version {info.version}, "
        f"build {info.build_commit or 'unstamped'}"
        + (f", built {info.build_time}" if info.build_time else "") + ")",
    ]
    if info.mode == "frozen":
        lines.append(f"[self] exe: {info.exe_path}")
        lines.append("[self] code is sealed in the exe — an update rebuilds it from source and "
                     "swaps it in (needs git + Python 3.11/3.12 installed to build).")
    else:
        lines.append(f"[self] repo: {info.repo_root}")
        lines.append(f"[self] source: {info.project_dir}")
        lines.append("[self] code is on disk — an update pulls the repo; the new code loads on restart.")
    lines.append(f"[self] {st.summary}")
    if st.behind:
        lines.append("[self] Run self_update (backs up + fetches), then self_restart to apply it.")
    return "\n".join(lines)


# ── update ──────────────────────────────────────────────────────────────────--
async def self_update(ctx: ToolContext, make_backup: bool = True, restart: bool = False) -> str:
    """Update the manager's own code. Backs up first, then pulls (source) or
    rebuilds + stages a new exe (frozen). Pass restart=true to apply immediately
    (closes and relaunches the manager). Mutating."""
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    info = gather()
    if info.mode == "frozen":
        msg = await _update_frozen(ctx, info, make_backup)
    else:
        msg = await _update_source(ctx, info, make_backup)
    if restart and not msg.startswith(("Error", "Refused")):
        msg += "\n\n" + await self_restart(ctx)
    return msg


def _copy_source(src: Path, dest: Path) -> None:
    shutil.copytree(src, dest, ignore=_BACKUP_IGNORE)


async def _update_source(ctx: ToolContext, info: SelfInfo, make_backup: bool) -> str:
    if not info.repo_root:
        return "Error: can't locate the git checkout this manager runs from."
    repo = Path(info.repo_root)
    if not shutil.which("git"):
        return "Error: git isn't installed, so the manager can't pull its update. Install git and retry."
    steps: list[str] = []
    if make_backup and info.project_dir:
        dest = _backups_dir() / f"source-{_ts()}"
        try:
            await asyncio.to_thread(_copy_source, Path(info.project_dir), dest)
            steps.append(f"backed up the manager's source to {dest}")
        except OSError as e:
            ctx.audit("self_update", {"mode": "source"}, False, f"backup failed: {e}")
            return f"Error: backup failed ({e}) — aborted before changing anything."
    out, code = await install._run(["git", "pull", "--ff-only"], repo, 180)
    ctx.audit("self_update", {"mode": "source", "backup": make_backup}, code == 0, out[-300:])
    if code != 0:
        pre = ("[self-update] " + "; ".join(steps) + ".\n") if steps else ""
        return (pre + f"But the pull failed (exit {code}):\n{out[-600:]}\n"
                "Nothing else changed and your backup is safe. This is usually local edits or a "
                "diverged branch — resolve those (or restore from the backup) and retry.")
    steps.append("pulled the latest code (fast-forward)")
    return ("[self-update] " + "; ".join(steps) + ".\n"
            "The new code loads on restart — run self_restart (or close and reopen) to apply it. "
            "Note: this pulled the whole repository, so a managed webAgent checkout living in the "
            "same repo is now updated too.")


async def _update_frozen(ctx: ToolContext, info: SelfInfo, make_backup: bool) -> str:
    exe = Path(info.exe_path) if info.exe_path else Path(sys.executable).resolve()
    exe_dir = exe.parent
    # Preflight: rebuilding needs git (fetch source) + a supported Python (PyInstaller).
    if not shutil.which("git"):
        return "Error: rebuilding the exe needs git to fetch source. Install git and retry — nothing was changed."
    py = install._supported_python()
    if not py:
        return ("Error: rebuilding the exe needs Python 3.11 or 3.12 to run the build. "
                "Install one and retry — nothing was changed.")
    work = _self_dir()
    src = work / "src"
    steps: list[str] = []
    # 1) fresh source (shallow; reuse the clone across runs)
    if (src / ".git").exists():
        await install._run(["git", "fetch", "--depth", "1", "origin", DEFAULT_BRANCH], src, 300)
        out, code = await install._run(["git", "reset", "--hard", f"origin/{DEFAULT_BRANCH}"], src, 120)
    else:
        if src.exists():
            await asyncio.to_thread(shutil.rmtree, src, True)
        out, code = await install._run(["git", "clone", "--depth", "1", REPO_URL, str(src)], None, 300)
    if code != 0:
        ctx.audit("self_update", {"mode": "frozen", "step": "fetch"}, False, out[-300:])
        return f"Error fetching fresh source (exit {code}):\n{out[-500:]}"
    steps.append("fetched fresh source")
    tui = src / "webagent"
    if not (tui / "scripts" / "build_exe.py").exists():
        return f"Error: fetched source is missing the build script ({tui})."
    # 2) build environment (reuse across runs)
    buildenv = work / "buildenv"
    bpy = buildenv / ("Scripts" if _IS_WIN else "bin") / ("python.exe" if _IS_WIN else "python")
    if not bpy.exists():
        out, code = await install._run([py, "-m", "venv", str(buildenv)], None, 180)
        if code != 0:
            return f"Error creating the build environment (exit {code}):\n{out[-400:]}"
    await install._run([str(bpy), "-m", "pip", "install", "--upgrade", "pip"], tui, 300)
    out, code = await install._run([str(bpy), "-m", "pip", "install", "-e", ".[build]"], tui, 1200)
    if code != 0:
        ctx.audit("self_update", {"mode": "frozen", "step": "deps"}, False, out[-400:])
        return f"Error installing build tools (exit {code}):\n{out[-600:]}"
    steps.append("installed build tools")
    # 3) build the new exe
    out, code = await install._run([str(bpy), "scripts/build_exe.py"], tui, 1800)
    built = tui / ("webagent.exe" if _IS_WIN else "webagent")
    if code != 0 or not built.exists():
        ctx.audit("self_update", {"mode": "frozen", "step": "build"}, False, out[-400:])
        return f"Error building the new exe (exit {code}):\n{out[-700:]}"
    steps.append("built the new exe")
    # 4) back up the current exe (timestamped) before staging the replacement
    if make_backup:
        bak = exe_dir / f"{exe.stem}.{_ts()}.bak{exe.suffix}"
        try:
            await asyncio.to_thread(shutil.copy2, exe, bak)
        except OSError:
            bak = _backups_dir() / f"{exe.stem}.{_ts()}.bak{exe.suffix}"
            try:
                await asyncio.to_thread(shutil.copy2, exe, bak)
            except OSError as e:
                return f"Error: couldn't back up the current exe ({e}) — aborted before the swap."
        steps.append(f"backed up the current exe to {bak}")
    # 5) stage the new exe beside the current one (swap happens on restart)
    staged = exe_dir / f"{exe.stem}.new{exe.suffix}"
    try:
        await asyncio.to_thread(shutil.copy2, built, staged)
    except OSError as e:
        return f"Error staging the new exe ({e})."
    steps.append(f"staged the new exe at {staged}")
    ctx.audit("self_update", {"mode": "frozen"}, True, "; ".join(steps))
    return ("[self-update] " + "; ".join(steps) + ".\n"
            "A running exe can't overwrite itself, so the swap finishes on restart: run self_restart "
            "and the manager will close, swap in the new exe, and relaunch. The backup stays in place.")


# ── restart (applies a staged update / reloads source) ────────────────────────
def _write_helper(name: str, body: str) -> Path:
    ext = ".bat" if _IS_WIN else ".sh"
    p = _self_dir() / f"{name}-{_ts()}{ext}"
    p.write_text(body, encoding="utf-8")
    if not _IS_WIN:
        os.chmod(p, 0o755)
    return p


def _win_wait_block(pid: int) -> str:
    return (":waitloop\n"
            f'tasklist /FI "PID eq {pid}" /NH | find "{pid}" >nul\n'
            "if not errorlevel 1 ( timeout /t 1 /nobreak >nul & goto waitloop )\n")


def _swap_and_relaunch_helper(pid: int, staged: Optional[Path], exe: Path) -> Path:
    """Helper that waits for our process to exit, swaps the staged exe in (if any),
    and relaunches. Used for the frozen self-update swap (and a plain exe restart)."""
    if _IS_WIN:
        move = f'move /Y "{staged}" "{exe}" >nul\n' if staged else ""
        body = ("@echo off\n" + _win_wait_block(pid) + move
                + f'start "" "{exe}"\n' + 'del "%~f0"\n')
    else:
        move = f'mv -f "{staged}" "{exe}"\nchmod +x "{exe}"\n' if staged else ""
        body = ("#!/bin/sh\n"
                f"while kill -0 {pid} 2>/dev/null; do sleep 1; done\n"
                + move + f'"{exe}" &\n' + 'rm -- "$0"\n')
    return _write_helper("swap", body)


def _relaunch_source_helper(pid: int, python: str, project_dir: Optional[str]) -> Path:
    """Helper that waits for our process to exit then relaunches the source TUI.

    Relaunches via ``python -m tui_app`` (there is no ``webagent`` module) and puts
    the TUI project dir on PYTHONPATH (mirrors install-termux.sh) so ``tui_app`` —
    and any guardian it then spawns — is importable no matter what cwd the helper
    runs from. ``project_dir`` is the TUI project dir (selfinfo.tui_project_dir)."""
    cd_win = f'cd /d "{project_dir}"\n' if project_dir else ""
    cd_sh = f'cd "{project_dir}"\n' if project_dir else ""
    pp_win = f'set "PYTHONPATH={project_dir};%PYTHONPATH%"\n' if project_dir else ""
    pp_sh = f'export PYTHONPATH="{project_dir}:${{PYTHONPATH:-}}"\n' if project_dir else ""
    if _IS_WIN:
        body = ("@echo off\n" + _win_wait_block(pid) + cd_win + pp_win
                + f'start "" "{python}" -m tui_app\n' + 'del "%~f0"\n')
    else:
        body = ("#!/bin/sh\n"
                f"while kill -0 {pid} 2>/dev/null; do sleep 1; done\n"
                + cd_sh + pp_sh + f'"{python}" -m tui_app &\n' + 'rm -- "$0"\n')
    return _write_helper("relaunch", body)


def _spawn_helper(helper: Path) -> None:
    """Launch a fully detached helper that survives this process exiting."""
    common = dict(cwd=str(helper.parent), stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if _IS_WIN:
        subprocess.Popen(["cmd", "/c", str(helper)],
                         creationflags=0x00000008 | 0x00000200, **common)  # DETACHED | NEW_GROUP
    else:
        subprocess.Popen(["sh", str(helper)], start_new_session=True, **common)


async def self_restart(ctx: ToolContext) -> str:
    """Close and relaunch the manager — applying a staged exe swap (frozen) or
    reloading the just-pulled source. Mutating."""
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    info = gather()
    if info.mode == "frozen":
        exe = Path(info.exe_path) if info.exe_path else Path(sys.executable).resolve()
        staged = exe.parent / f"{exe.stem}.new{exe.suffix}"
        helper = _swap_and_relaunch_helper(os.getpid(), staged if staged.exists() else None, exe)
        what = "swap in the staged exe and relaunch" if staged.exists() else "relaunch"
    else:
        helper = _relaunch_source_helper(os.getpid(), sys.executable, info.project_dir)
        what = "relaunch from source"
    try:
        await asyncio.to_thread(_spawn_helper, helper)
    except OSError as e:
        ctx.audit("self_restart", {"mode": info.mode}, False, str(e))
        return f"Error scheduling restart ({e}). Restart the manager manually."
    ctx.audit("self_restart", {"mode": info.mode}, True, str(helper))
    if ctx.request_exit is not None:
        await ctx.request_exit()
        return f"[self-restart] Closing now; a helper will {what} in a moment."
    return (f"[self-restart] A helper is ready to {what} once this process exits — "
            f"close the manager to let it finish ({helper}).")

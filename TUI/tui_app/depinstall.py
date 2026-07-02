"""Deterministic install runners for the Setup dashboard — one per dependency id.

Each runner runs the REAL platform commands itself (no AI agent involved), streams
progress through a ``log`` callback, audits via the manager's store, and returns
``(ok, message)``. The desktop runners delegate to the existing onboarding tools in
``tools/install.py`` (clone / environment / seed / verify); the Termux runners build
+ manage the Ubuntu ``proot-distro`` sandbox exactly the way ``deploy/termux-setup.sh``
does, so the button path and the deploy command stay in lock-step.

Called from the Setup panel's button handlers (the click is consent → writes on) and
from the ``install_dependency`` agent tool. Pure logic + subprocess; Textual-free.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Callable, Optional, Tuple

from .env_probe import _is_termux
from .tools.base import ToolContext, WRITES_DISABLED_MSG
from .tools.install import (
    REPO_URL,  # noqa: F401  (kept for parity / future use)
    _resolve_target,
    _run,
    _supported_python,
    _venv_python,
    clone_repo,
    seed_config,
    setup_environment,
    verify_install,
)

LogFn = Callable[[str], None]
Result = Tuple[bool, str]

# Inside-Ubuntu mount point for the host checkout (matches deploy/termux-setup.sh
# and tools/server.py's _spawn_detached_proot, so the venv symlinks resolve).
_IN_PROOT_ROOT = "/root/webagent"
# The Playwright-free dependency set the phone/lightweight install uses.
_REQ_FILE = "req_no_playwright.txt"


# ── platform command helpers ──────────────────────────────────────────────────
def _sudo() -> list[str]:
    """``['sudo']`` on a non-root POSIX host that has sudo, else ``[]``."""
    try:
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() != 0:
            if shutil.which("sudo"):
                return ["sudo"]
    except Exception:
        pass
    return []


def _proot_bin() -> str:
    return shutil.which("proot-distro") or (
        f"{os.environ.get('PREFIX', '/data/data/com.termux/files/usr')}/bin/proot-distro")


def _proot_login_args(target: Path, inner: str, *, bind: bool = True) -> list[str]:
    """``proot-distro login ubuntu [--bind host:/root/webagent] -- bash -lc '<inner>'``."""
    args = [_proot_bin(), "login", "ubuntu"]
    if bind:
        args += ["--bind", f"{target}:{_IN_PROOT_ROOT}"]
    args += ["--", "bash", "-lc", inner]
    return args


async def _step(log: LogFn, heading: str, args: list[str],
                cwd: Optional[Path], timeout: int) -> Tuple[str, int]:
    """Log a heading, run a command to completion, return (output, exit-code)."""
    log(heading)
    out, code = await _run(args, cwd, timeout)
    return out, code


def _finish(ctx: Optional[ToolContext], name: str, ok: bool,
            out: str, log: LogFn) -> Result:
    tail = (out or "").strip()
    short = tail[-700:]
    if ok:
        log("✓ done")
    else:
        log("✗ failed:")
        if short:
            log(short)
    if ctx is not None:
        try:
            ctx.audit(name, {}, ok, tail[-300:])
        except Exception:
            pass
    return ok, ("ok" if ok else (short or "failed"))


def _manual(ctx: Optional[ToolContext], dep_id: str, log: LogFn, guidance: str) -> Result:
    """No automatic installer on this platform — tell the user what to do."""
    log(guidance)
    if ctx is not None:
        try:
            ctx.audit(f"install:{dep_id}", {}, False, "manual: " + guidance)
        except Exception:
            pass
    return False, guidance


async def _pkg_install_linux(ctx: Optional[ToolContext], dep_id: str,
                             packages: list[str], log: LogFn) -> Result:
    """Install system packages with whatever package manager a Linux host has."""
    sudo = _sudo()
    pkgs = " ".join(packages)
    if shutil.which("apt-get"):
        await _step(log, "Updating package lists (apt-get update)…",
                    sudo + ["apt-get", "update"], None, 600)
        out, code = await _step(log, f"Installing {pkgs} (apt-get)…",
                                sudo + ["apt-get", "install", "-y", *packages], None, 1800)
    elif shutil.which("dnf"):
        out, code = await _step(log, f"Installing {pkgs} (dnf)…",
                                sudo + ["dnf", "install", "-y", *packages], None, 1800)
    elif shutil.which("pacman"):
        out, code = await _step(log, f"Installing {pkgs} (pacman)…",
                                sudo + ["pacman", "-Sy", "--noconfirm", *packages], None, 1800)
    else:
        return _manual(ctx, dep_id, log,
                       f"No apt/dnf/pacman found — install {pkgs} with your package "
                       "manager, then re-check.")
    return _finish(ctx, f"install:{dep_id}", code == 0, out, log)


# ── prerequisite runners (git / python) ───────────────────────────────────────
async def run_git(ctx: ToolContext, target: Path, log: LogFn) -> Result:
    if _is_termux():
        out, code = await _step(log, "Installing git (pkg)…",
                                ["pkg", "install", "-y", "git"], None, 900)
        return _finish(ctx, "install:git", code == 0, out, log)
    if sys.platform == "win32":
        if shutil.which("winget"):
            out, code = await _step(
                log, "Installing Git via winget…",
                ["winget", "install", "--id", "Git.Git", "-e", "--source", "winget",
                 "--accept-package-agreements", "--accept-source-agreements"], None, 1800)
            return _finish(ctx, "install:git", code == 0, out, log)
        return _manual(ctx, "git", log,
                       "winget isn't available. Install Git from "
                       "https://git-scm.com/download/win, then click Re-check.")
    if sys.platform == "darwin":
        if shutil.which("brew"):
            out, code = await _step(log, "Installing git via Homebrew…",
                                    ["brew", "install", "git"], None, 1800)
            return _finish(ctx, "install:git", code == 0, out, log)
        return _manual(ctx, "git", log,
                       "Install the Xcode command-line tools (run: xcode-select --install) "
                       "or Homebrew git, then click Re-check.")
    return await _pkg_install_linux(ctx, "git", ["git"], log)


async def run_python(ctx: ToolContext, target: Path, log: LogFn) -> Result:
    if sys.platform == "win32":
        if shutil.which("winget"):
            out, code = await _step(
                log, "Installing Python 3.12 via winget…",
                ["winget", "install", "--id", "Python.Python.3.12", "-e", "--source", "winget",
                 "--accept-package-agreements", "--accept-source-agreements"], None, 1800)
            ok = code == 0 and bool(_supported_python())
            return _finish(ctx, "install:python", ok, out, log)
        return _manual(ctx, "python", log,
                       "winget isn't available. Install Python 3.12 from "
                       "https://www.python.org/downloads/ (tick 'Add to PATH'), then Re-check.")
    if sys.platform == "darwin":
        if shutil.which("brew"):
            out, code = await _step(log, "Installing Python 3.12 via Homebrew…",
                                    ["brew", "install", "python@3.12"], None, 1800)
            return _finish(ctx, "install:python", code == 0, out, log)
        return _manual(ctx, "python", log,
                       "Install Python 3.12 from https://www.python.org/downloads/macos/ "
                       "or Homebrew (brew install python@3.12), then Re-check.")
    # Linux — best-effort; the distro's python3 may not be 3.11/3.12.
    res = await _pkg_install_linux(ctx, "python", ["python3", "python3-venv", "python3-pip"], log)
    if res[0] and not _supported_python():
        log("Note: the installed Python isn't 3.11/3.12. Install one of those "
            "specifically (e.g. the deadsnakes PPA) if the environment step fails.")
    return res


# ── Termux: the Ubuntu sandbox ────────────────────────────────────────────────
async def run_proot_distro(ctx: ToolContext, target: Path, log: LogFn) -> Result:
    out, code = await _step(log, "Installing proot-distro (pkg)…",
                            ["pkg", "install", "-y", "proot-distro"], None, 900)
    return _finish(ctx, "install:proot_distro", code == 0, out, log)


async def run_ubuntu(ctx: ToolContext, target: Path, log: LogFn) -> Result:
    proot = _proot_bin()
    # Already usable? (A container can be registered under a path the probe didn't
    # spot — custom PROOT_DISTRO_HOME, older layout — so the only sure test is to
    # log in and run a no-op.) If so, reuse it; don't trip over "already exists".
    _out, code = await _step(log, "Checking for an existing Ubuntu environment…",
                             [proot, "login", "ubuntu", "--", "true"], None, 120)
    if code == 0:
        return _finish(ctx, "install:ubuntu", True,
                       "Ubuntu environment already installed — reusing it.", log)
    out, code = await _step(log, "Installing the Ubuntu environment (first run is slow)…",
                            [proot, "install", "ubuntu"], None, 2400)
    if code != 0:
        # A registered-but-broken container (interrupted earlier run) makes 'install'
        # abort with "already exists". Reset it cleanly rather than failing.
        log("Existing Ubuntu install looks incomplete — resetting it cleanly…")
        await _step(log, "Removing the broken Ubuntu environment…",
                    [proot, "remove", "ubuntu"], None, 600)
        out, code = await _step(log, "Reinstalling Ubuntu (slow)…",
                                [proot, "install", "ubuntu"], None, 2400)
    return _finish(ctx, "install:ubuntu", code == 0, out, log)


async def run_ubuntu_toolchain(ctx: ToolContext, target: Path, log: LogFn) -> Result:
    inner = ("set -e; export DEBIAN_FRONTEND=noninteractive; apt-get update; "
             "apt-get install -y python3 python3-venv python3-pip git "
             "build-essential libffi-dev")
    out, code = await _step(log, "Preparing Ubuntu (python3, venv, build tools)…",
                            _proot_login_args(target, inner, bind=False), None, 2400)
    return _finish(ctx, "install:ubuntu_toolchain", code == 0, out, log)


async def manage_ubuntu(ctx: ToolContext, op: str, target: str, log: LogFn) -> Result:
    """Reinstall / remove / update the Ubuntu sandbox (the Ubuntu row's manage menu)."""
    if not ctx.writes_enabled:
        return False, WRITES_DISABLED_MSG
    tgt = _resolve_target(target) if target else _default_target()
    proot = _proot_bin()
    if op == "ubuntu_remove":
        out, code = await _step(log, "Removing the Ubuntu environment…",
                                [proot, "remove", "ubuntu"], None, 600)
        return _finish(ctx, "manage:ubuntu_remove", code == 0, out, log)
    if op == "ubuntu_reinstall":
        await _step(log, "Removing the old Ubuntu environment…",
                    [proot, "remove", "ubuntu"], None, 600)
        out, code = await _step(log, "Reinstalling Ubuntu (slow)…",
                                [proot, "install", "ubuntu"], None, 2400)
        return _finish(ctx, "manage:ubuntu_reinstall", code == 0, out, log)
    if op == "ubuntu_update":
        inner = ("set -e; export DEBIAN_FRONTEND=noninteractive; "
                 "apt-get update; apt-get -y upgrade")
        out, code = await _step(log, "Updating Ubuntu packages…",
                                _proot_login_args(tgt, inner, bind=False), None, 2400)
        return _finish(ctx, "manage:ubuntu_update", code == 0, out, log)
    return False, f"Unknown Ubuntu action '{op}'."


# ── the WebAgent install itself ───────────────────────────────────────────────
async def run_repo(ctx: ToolContext, target: Path, log: LogFn) -> Result:
    log(f"Cloning WebAgent into {target}…")
    msg = await clone_repo(ctx, str(target))
    ok = "cloned into" in msg or "[clone]" in msg
    log(msg)
    return ok, msg


async def run_venv(ctx: ToolContext, target: Path, log: LogFn) -> Result:
    if _is_termux():
        inner = (f"set -e; cd {_IN_PROOT_ROOT}; "
                 "python3 -m venv .venv; "
                 ".venv/bin/pip install --upgrade pip wheel; "
                 f".venv/bin/pip install -r {_REQ_FILE}")
        out, code = await _step(log, "Building the environment inside Ubuntu (slow)…",
                                _proot_login_args(target, inner), None, 2400)
        return _finish(ctx, "install:venv", code == 0, out, log)
    # Desktop: reuse the onboarding tool (venv + deps; browser is its own row).
    log("Building the virtual environment + installing dependencies (slow)…")
    msg = await setup_environment(ctx, str(target), install_browser=False)
    ok = "dependencies installed" in msg or msg.startswith("[setup]") and "failed" not in msg
    log(msg)
    return ok, msg


async def run_browser(ctx: ToolContext, target: Path, log: LogFn) -> Result:
    if _is_termux():
        return False, "The headless browser isn't available on Android."
    vpy = _venv_python(target)
    if not vpy.exists():
        return False, "Build the Python environment first (the venv is missing)."
    out, code = await _step(log, "Downloading the headless browser (Chromium)…",
                            [str(vpy), "-m", "playwright", "install", "chromium"],
                            target, 1200)
    return _finish(ctx, "install:browser", code == 0, out, log)


async def run_config(ctx: ToolContext, target: Path, log: LogFn) -> Result:
    log("Seeding configuration (.env, provider.json, local SQLite)…")
    msg = await seed_config(ctx, str(target))
    ok = msg.startswith("[seed]")
    log(msg)
    return ok, msg


async def run_verify(ctx: ToolContext, target: Path, log: LogFn) -> Result:
    if _is_termux():
        inner = (f"cd {_IN_PROOT_ROOT}; "
                 ".venv/bin/python -c 'import app.main; print(\"import-ok\")'")
        out, code = await _step(log, "Verifying the app imports inside Ubuntu…",
                                _proot_login_args(target, inner), None, 600)
        ok = code == 0 and "import-ok" in out
        return _finish(ctx, "install:verify", ok, out, log)
    log("Verifying the app imports cleanly and the database initialises…")
    msg = await verify_install(ctx, str(target))
    ok = msg.startswith("[verify] the app imports")
    log(msg)
    return ok, msg


# ── dispatch ──────────────────────────────────────────────────────────────────
RUNNERS = {
    "git": run_git,
    "python": run_python,
    "proot_distro": run_proot_distro,
    "ubuntu": run_ubuntu,
    "ubuntu_toolchain": run_ubuntu_toolchain,
    "repo": run_repo,
    "venv": run_venv,
    "browser": run_browser,
    "config": run_config,
    "verify": run_verify,
}

INSTALLABLE_IDS = frozenset(RUNNERS)


async def install_one(ctx: ToolContext, dep_id: str, target: str, log: LogFn) -> Result:
    """Run the deterministic installer for one dependency id. Streams via ``log``."""
    if not ctx.writes_enabled:
        return False, WRITES_DISABLED_MSG
    runner = RUNNERS.get(dep_id)
    if runner is None:
        return False, f"No installer for '{dep_id}'."
    tgt = _resolve_target(target) if target else _resolve_target(str(_default_target()))
    try:
        return await runner(ctx, tgt, log)
    except Exception as e:  # never let a runner crash the panel / agent loop
        msg = f"{type(e).__name__}: {e}"
        log("✗ " + msg)
        try:
            ctx.audit(f"install:{dep_id}", {}, False, msg)
        except Exception:
            pass
        return False, msg


def _default_target() -> Path:
    return Path("C:/webagent") if sys.platform == "win32" else Path.home() / "webagent"

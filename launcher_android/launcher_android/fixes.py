"""Remediation actions for the doctor checks.

Each fix is an async generator yielding log lines as it runs, so the TUI
can stream output into the log pane. Final yielded line is either
"[fix] OK ..." or "[fix] FAIL ...".

Fixes deliberately do NOT touch the webagent app code — they only adjust
the environment (venv, .env, apt packages, pip installs).
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from typing import AsyncIterator

from .doctor import APT_BUILD_DEPS


async def _run(cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> AsyncIterator[str]:
    """Run a subprocess, yielding each output line. Final line includes exit code."""
    yield f"$ {' '.join(shlex.quote(c) for c in cmd)}"
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (FileNotFoundError, OSError) as e:
        yield f"  ERROR: {e}"
        return

    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        yield "  " + line.decode("utf-8", errors="replace").rstrip()
    rc = await proc.wait()
    yield f"  ↳ exit {rc}"


def _venv_python(project_dir: Path) -> Path:
    return project_dir / "venv" / "bin" / "python"


def _venv_pip(project_dir: Path) -> Path:
    return project_dir / "venv" / "bin" / "pip"


# ── individual fixes ──────────────────────────────────────────────────


async def create_venv(project_dir: Path) -> AsyncIterator[str]:
    yield "[fix] creating venv..."
    async for line in _run(["python3", "-m", "venv", "venv"], cwd=project_dir):
        yield line
    if _venv_python(project_dir).exists():
        async for line in _run(
            [str(_venv_pip(project_dir)), "install", "--upgrade", "pip", "setuptools", "wheel"],
            cwd=project_dir,
        ):
            yield line
        yield "[fix] OK venv created"
    else:
        yield "[fix] FAIL venv was not created — is python3-venv installed?"


async def create_env(project_dir: Path) -> AsyncIterator[str]:
    src = project_dir / ".env.example"
    dst = project_dir / ".env"
    if dst.exists():
        yield "[fix] .env already exists — leaving alone"
        return
    if not src.exists():
        yield "[fix] FAIL .env.example missing — cannot generate .env"
        return
    try:
        text = src.read_text()
        dst.write_text(text)
        yield f"[fix] OK created {dst} from .env.example"
        yield "[fix] NOTE: edit .env and set LLM_API_KEY before launching"
    except OSError as e:
        yield f"[fix] FAIL {e}"


async def apt_install(_project_dir: Path) -> AsyncIterator[str]:
    yield "[fix] installing apt build dependencies..."
    async for line in _run(["apt-get", "update"]):
        yield line
    async for line in _run(["apt-get", "install", "-y", *APT_BUILD_DEPS]):
        yield line
    yield "[fix] OK apt install complete (re-run doctor)"


async def pip_install_all(project_dir: Path) -> AsyncIterator[str]:
    py = _venv_python(project_dir)
    if not py.exists():
        yield "[fix] FAIL venv missing — run 'create_venv' first"
        return
    req = project_dir / "requirements.txt"
    if not req.exists():
        yield f"[fix] FAIL requirements.txt not found in {project_dir}"
        return

    yield "[fix] installing each requirement individually (failures are not fatal)..."
    succeeded: list[str] = []
    failed: list[str] = []
    env = os.environ.copy()
    env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"

    for raw in req.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        rc_marker = {"rc": None}

        async def _runner(pkg_spec: str):
            proc = await asyncio.create_subprocess_exec(
                str(py), "-m", "pip", "install", pkg_spec,
                cwd=str(project_dir),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.readline()
                if not chunk:
                    break
                yield "  " + chunk.decode("utf-8", errors="replace").rstrip()
            rc_marker["rc"] = await proc.wait()

        yield f"--- {line} ---"
        async for log_line in _runner(line):
            yield log_line
        if rc_marker["rc"] == 0:
            succeeded.append(line)
        else:
            failed.append(line)

    yield ""
    yield f"[fix] installed: {len(succeeded)}, failed: {len(failed)}"
    if failed:
        yield "[fix] failed packages (will be marked non-critical):"
        for f in failed:
            yield f"  - {f}"
        yield "[fix] WARN webagent may run with reduced functionality"
    else:
        yield "[fix] OK all requirements installed"


async def pip_install_one(project_dir: Path, pkg: str) -> AsyncIterator[str]:
    py = _venv_python(project_dir)
    if not py.exists():
        yield "[fix] FAIL venv missing — run 'create_venv' first"
        return
    env = os.environ.copy()
    env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    yield f"[fix] installing {pkg}..."
    async for line in _run(
        [str(py), "-m", "pip", "install", pkg],
        cwd=project_dir,
        env=env,
    ):
        yield line
    yield f"[fix] OK install attempted for {pkg}"


async def set_playwright_skip(project_dir: Path) -> AsyncIterator[str]:
    env = project_dir / ".env"
    if not env.exists():
        yield "[fix] .env missing — creating from example first"
        async for line in create_env(project_dir):
            yield line
        if not env.exists():
            yield "[fix] FAIL cannot persist setting"
            return

    text = env.read_text()
    if "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD" in text:
        new = []
        for raw in text.splitlines():
            if raw.startswith("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"):
                new.append("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1")
            else:
                new.append(raw)
        env.write_text("\n".join(new) + "\n")
    else:
        with env.open("a") as f:
            f.write("\n# Skip Playwright Chromium download (Android-ARM compat)\n")
            f.write("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1\n")
    os.environ["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    yield "[fix] OK PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 written to .env + exported"


# ── fix dispatch ──────────────────────────────────────────────────────


async def dispatch(action: str, project_dir: Path) -> AsyncIterator[str]:
    """Route a fix_action string from a Check to the right handler."""
    if action == "create_venv":
        async for line in create_venv(project_dir):
            yield line
        return
    if action == "create_env":
        async for line in create_env(project_dir):
            yield line
        return
    if action == "apt_install":
        async for line in apt_install(project_dir):
            yield line
        return
    if action == "pip_install_all":
        async for line in pip_install_all(project_dir):
            yield line
        return
    if action == "set_playwright_skip":
        async for line in set_playwright_skip(project_dir):
            yield line
        return
    if action.startswith("pip_install:"):
        pkg = action.split(":", 1)[1]
        async for line in pip_install_one(project_dir, pkg):
            yield line
        return
    yield f"[fix] unknown action: {action}"


async def fix_all(project_dir: Path) -> AsyncIterator[str]:
    """Run a sane fix-everything sequence in order."""
    yield "[fix-all] starting full remediation..."
    async for line in apt_install(project_dir):
        yield line
    async for line in create_venv(project_dir):
        yield line
    async for line in create_env(project_dir):
        yield line
    async for line in set_playwright_skip(project_dir):
        yield line
    async for line in pip_install_all(project_dir):
        yield line
    yield "[fix-all] done — re-run doctor"

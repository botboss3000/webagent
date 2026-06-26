"""Fresh-install flow — clone the public repo, build the environment, seed
config, and verify. Onboarding tools (``needs_project=False``): they operate on
an explicit *target* directory, not a linked checkout, so they are deliberately
NOT confined to the project-root sandbox the codebase tools use.

Ordered steps the agent orchestrates (each does one reliable thing):
  check_install_readiness → clone_repo → setup_environment → seed_config →
  verify_install → (link_project to finish).

The public source is github.com/botboss3000/webagent.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sys
from pathlib import Path
from typing import Optional

from ..config import ProviderConfig
from ..env_probe import probe_machine
from .base import WRITES_DISABLED_MSG, ToolContext

REPO_URL = "https://github.com/botboss3000/webagent"
_IS_WIN = sys.platform == "win32"
_MIN_FREE_MB = 1500  # repo + venv + browser is roughly ~1 GB; leave headroom


# ── helpers ───────────────────────────────────────────────────────────────────
def _resolve_target(target: str) -> Path:
    return Path(target).expanduser().resolve()


def _unsafe_target(p: Path) -> Optional[str]:
    """Reject obviously dangerous install locations."""
    if p == p.anchor or str(p) in ("/", ""):
        return "refusing to install at a filesystem root"
    if p == Path.home().resolve():
        return "refusing to install directly into your home directory — use a subfolder like ~/webagent"
    parts = {x.lower() for x in p.parts}
    if parts & {"windows", "system32", "program files", "program files (x86)", "/bin", "/etc", "/usr"}:
        return "refusing to install into a system directory"
    return None


def _reachable(host: str, port: int = 443, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def _run(args: list[str], cwd: Optional[Path], timeout: int) -> tuple[str, int]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode("utf-8", errors="replace"), proc.returncode or 0
    except asyncio.TimeoutError:
        return f"(timed out after {timeout}s)", 124
    except (OSError, FileNotFoundError) as e:
        return f"(failed to run {args[0]}: {e})", 1


def _supported_python(explicit: str = "") -> Optional[str]:
    """Find a Python 3.11/3.12 interpreter to build the install's venv with.

    The manager's own interpreter may be frozen (the .exe) or unsupported, so we
    look on PATH. ``explicit`` (if given and valid) wins."""
    cands = [explicit] if explicit else []
    cands += ["python3.12", "python3.11", "python3", "python"]
    for name in cands:
        if not name:
            continue
        exe = name if os.path.sep in name else shutil.which(name)
        if not exe:
            continue
        import subprocess
        try:
            out = subprocess.run([exe, "-c", "import sys;print('%d.%d'%sys.version_info[:2])"],
                                 capture_output=True, text=True, timeout=5)
            ver = (out.stdout or "").strip()
            if ver in ("3.11", "3.12"):
                return exe
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _venv_python(target: Path) -> Path:
    return target / ".venv" / ("Scripts" if _IS_WIN else "bin") / ("python.exe" if _IS_WIN else "python")


# ── tools ─────────────────────────────────────────────────────────────────────
async def check_install_readiness(ctx: ToolContext, target: str = "") -> str:
    """Read-only preflight checklist for a fresh install."""
    f = probe_machine()
    lines = [f"Install readiness ({f.os_label}, {f.arch}):"]
    py = _supported_python()
    lines.append(f"  [{'OK' if py else 'X'}] Python 3.11-3.12: "
                 + (f"found ({py})" if py else f"NOT found (system Python is {f.system_python or 'absent'}; "
                    "webAgent needs 3.11 or 3.12)"))
    lines.append(f"  [{'OK' if f.git_present else 'X'}] git: {'found' if f.git_present else 'MISSING — install git first'}")
    net = _reachable("github.com") and _reachable("pypi.org")
    lines.append(f"  [{'OK' if net else 'X'}] internet: {'github + pypi reachable' if net else 'cannot reach github/pypi'}")
    lines.append(f"  [{'OK' if f.browser_capable else '!'}] headless browser: "
                 + ("supported" if f.browser_capable else "NOT available here (browser features will be off)"))
    if target:
        p = _resolve_target(target)
        bad = _unsafe_target(p)
        if bad:
            lines.append(f"  [X] target {p}: {bad}")
        else:
            exists = p.exists()
            nonempty = exists and any(p.iterdir())
            lines.append(f"  [{'!' if nonempty else 'OK'}] target {p}: "
                         + ("exists and is NOT empty (choose an empty/new folder)" if nonempty
                            else "exists (empty)" if exists else "will be created"))
            try:
                anchor = p if exists else p.parent
                while not anchor.exists():
                    anchor = anchor.parent
                free_mb = shutil.disk_usage(anchor).free // (1024 * 1024)
                lines.append(f"  [{'OK' if free_mb >= _MIN_FREE_MB else 'X'}] disk: {free_mb} MB free "
                             f"(need ~{_MIN_FREE_MB})")
            except OSError as e:
                lines.append(f"  [?] disk: couldn't check ({e})")
    else:
        lines.append(f"  recommended location: {'C:/webagent' if f.os_label == 'Windows' else '~/webagent'}")
    return "\n".join(lines)


async def clone_repo(ctx: ToolContext, target: str) -> str:
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    p = _resolve_target(target)
    bad = _unsafe_target(p)
    if bad:
        return f"Refused: {bad}."
    if p.exists() and any(p.iterdir()):
        return f"Error: {p} already exists and isn't empty. Pick an empty or new folder."
    p.parent.mkdir(parents=True, exist_ok=True)
    out, code = await _run(["git", "clone", "--depth", "1", REPO_URL, str(p)], cwd=None, timeout=300)
    ok = code == 0 and (p / "run.py").exists()
    ctx.audit("clone_repo", {"target": str(p)}, ok, out[-300:])
    if not ok:
        return f"Error cloning the repo (exit {code}):\n{out[-600:]}"
    return f"[clone] webAgent cloned into {p}. Next: setup_environment."


async def setup_environment(ctx: ToolContext, target: str, python_exe: str = "",
                            install_browser: bool = True) -> str:
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    p = _resolve_target(target)
    if not (p / "requirements.txt").exists():
        return f"Error: no requirements.txt in {p} — clone the repo first."
    py = _supported_python(python_exe)
    if not py:
        return ("Error: no Python 3.11/3.12 found to build the environment. Install one "
                "(or pass its path) and retry.")
    steps: list[str] = []
    # 1) venv
    out, code = await _run([py, "-m", "venv", str(p / ".venv")], cwd=p, timeout=180)
    if code != 0:
        ctx.audit("setup_environment", {"step": "venv"}, False, out[-300:])
        return f"Error creating the virtual environment (exit {code}):\n{out[-500:]}"
    steps.append("venv created")
    vpy = str(_venv_python(p))
    # 2) pip upgrade + requirements (the long step)
    await _run([vpy, "-m", "pip", "install", "--upgrade", "pip"], cwd=p, timeout=300)
    out, code = await _run([vpy, "-m", "pip", "install", "-r", "requirements.txt"], cwd=p, timeout=1800)
    if code != 0:
        ctx.audit("setup_environment", {"step": "pip"}, False, out[-400:])
        return f"[setup] venv ok, but installing dependencies failed (exit {code}):\n{out[-800:]}"
    steps.append("dependencies installed")
    # 3) browser (skipped on platforms that can't run it)
    if install_browser and probe_machine().browser_capable:
        out, code = await _run([vpy, "-m", "playwright", "install", "chromium"], cwd=p, timeout=900)
        steps.append("browser installed" if code == 0 else f"browser install failed (exit {code})")
    elif not install_browser:
        steps.append("browser skipped (by request)")
    else:
        steps.append("browser skipped (not supported on this platform)")
    ctx.audit("setup_environment", {"target": str(p)}, True, "; ".join(steps))
    return f"[setup] {'; '.join(steps)}. Next: seed_config."


def _provider_label(base_url: str) -> str:
    host = base_url.lower()
    for key in ("deepinfra", "openrouter", "openai", "groq", "together", "mistral",
                "fireworks", "deepseek", "perplexity", "ollama"):
        if key in host:
            return key
    return "custom"


async def seed_config(ctx: ToolContext, target: str) -> str:
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    p = _resolve_target(target)
    if not (p / "run.py").exists():
        return f"Error: {p} isn't a webAgent checkout — clone first."
    prov: Optional[ProviderConfig] = ctx.app_provider
    have_key = bool(prov and prov.configured)
    # 1) .env from .env.example, with the app key wired in and Supabase left blank.
    example = p / ".env.example"
    env_lines: list[str] = []
    src = example.read_text(encoding="utf-8").splitlines() if example.exists() else [
        "LLM_API_KEY=", "LLM_BASE_URL=", "LLM_MODEL=", "ENVIRONMENT=development", "LOG_LEVEL=INFO"]
    for line in src:
        s = line.strip()
        if have_key and s.startswith("LLM_API_KEY="):
            env_lines.append(f"LLM_API_KEY={prov.api_key}")
        elif have_key and s.startswith("LLM_BASE_URL="):
            env_lines.append(f"LLM_BASE_URL={prov.base_url}")
        elif have_key and s.startswith("LLM_MODEL="):
            env_lines.append(f"LLM_MODEL={prov.model}")
        elif s.startswith(("SUPABASE_URL=", "SUPABASE_SERVICE_ROLE_KEY=")):
            env_lines.append(s.split("=", 1)[0] + "=")  # blank → stays on local DB
        else:
            env_lines.append(line)
    (p / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    # 2) provider.json (the store both the app and this manager read).
    if have_key:
        entry = {"provider": _provider_label(prov.base_url), "base_url": prov.base_url,
                 "api_key": prov.api_key, "model": prov.model}
        (p / "data" / "config").mkdir(parents=True, exist_ok=True)
        (p / "data" / "config" / "provider.json").write_text(
            json.dumps({"admin": entry, "__anonymous__": entry}, indent=2), encoding="utf-8")
    # 3) db_connection.json → explicit local sqlite (no external service needed).
    (p / "db_connection.json").write_text(
        json.dumps({"provider": "sqlite", "database": ""}, indent=2), encoding="utf-8")
    ctx.audit("seed_config", {"target": str(p), "key": have_key}, True, "")
    note = (f"AI key seeded (model {prov.model})" if have_key
            else "no app key available — set LLM_API_KEY in the new .env before running")
    return f"[seed] .env + provider.json + db_connection.json written (local SQLite DB). {note}. Next: verify_install."


async def verify_install(ctx: ToolContext, target: str) -> str:
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    p = _resolve_target(target)
    vpy = _venv_python(p)
    if not vpy.exists():
        return f"Error: no environment at {p}/.venv — run setup_environment first."
    out, code = await _run([str(vpy), "-c", "import app.main; print('import-ok')"],
                           cwd=p, timeout=180)
    ok = code == 0 and "import-ok" in out
    ctx.audit("verify_install", {"target": str(p)}, ok, out[-300:])
    if ok:
        return ("[verify] the app imports cleanly and its local database is initialised. "
                f"Ready to run — link_project {p}, then start the server.")
    return f"[verify] the app failed to import (exit {code}):\n{out[-800:]}"

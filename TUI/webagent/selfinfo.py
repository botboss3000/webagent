"""Self-introspection: how THIS manager is running, what version it is, and
whether a newer one is available upstream — the inputs the self-update pipeline
reads. Pure observation; nothing here mutates anything.

The manager ships two ways, and self-update means something different in each:

* **source** — launched as ``python -m webagent`` from a checkout (``run.bat``).
  Its code IS the ``webagent/`` package inside the public webAgent git repo,
  so a newer version is a ``git pull`` away. The new code only loads on **restart**
  (Python doesn't reload its own running modules).
* **frozen** — the single-file PyInstaller ``.exe``. Its code is sealed inside the
  binary, so a newer version means rebuilding the exe from fresh source and
  swapping it in. The git commit it was built from is stamped into ``_build.py``
  at build time (see ``scripts/build_exe.py``) so a frozen build can still tell
  whether it's behind.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import __version__

# Public source of truth for both onboarding installs and the manager's own code.
REPO_URL = "https://github.com/botboss3000/webagent"
DEFAULT_BRANCH = "main"
_GITHUB_API = "https://api.github.com/repos/botboss3000/webagent/commits/" + DEFAULT_BRANCH


def is_frozen() -> bool:
    """True when running as the PyInstaller single-file exe."""
    return bool(getattr(sys, "frozen", False))


def package_dir() -> Path:
    """The installed ``webagent`` package directory."""
    return Path(__file__).resolve().parent


def _build_stamp() -> tuple[str, str]:
    """``(commit, time)`` the frozen exe was built from, or ``("", "")`` otherwise.

    ``build_exe.py`` writes ``webagent/_build.py`` just before bundling, so a
    frozen exe carries it but a plain source checkout does not (it's gitignored).
    """
    try:
        from . import _build  # type: ignore
    except Exception:
        return "", ""
    return (str(getattr(_build, "BUILD_COMMIT", "")).strip(),
            str(getattr(_build, "BUILD_TIME", "")).strip())


def find_repo_root(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from the package to the git repo that ships it (the dir with
    ``.git``). ``None`` when frozen or running outside a checkout."""
    if is_frozen():
        return None
    here = start or package_dir()
    for cand in (here, *here.parents):
        try:
            if (cand / ".git").exists():
                return cand
        except OSError:
            continue
    return None


def tui_project_dir() -> Optional[Path]:
    """The ``webagent/`` project dir (the one with ``pyproject.toml`` +
    ``scripts/``). Source mode only."""
    if is_frozen():
        return None
    cand = package_dir().parent          # .../webagent (project) / webagent (package)
    return cand if (cand / "pyproject.toml").exists() else None


def _sync_short_head(repo: Path) -> str:
    """Quick, best-effort current short commit for display (source mode)."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(repo),
                             capture_output=True, text=True, timeout=5, check=False)
        return (out.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


@dataclass
class SelfInfo:
    mode: str                  # "frozen" | "source"
    version: str               # package __version__
    build_commit: str          # frozen: stamped build commit; source: live short HEAD
    build_time: str            # frozen build timestamp (or "")
    exe_path: Optional[str]    # frozen exe location
    repo_root: Optional[str]   # source git repo root (holds .git)
    project_dir: Optional[str]  # source webagent/ project dir


def gather() -> SelfInfo:
    """Snapshot how this manager is running. Cheap; one quick git call in source
    mode for the display commit (cache it — it doesn't change while running)."""
    frozen = is_frozen()
    commit, btime = _build_stamp()
    repo = find_repo_root()
    proj = tui_project_dir()
    if not frozen and repo and not commit:
        commit = _sync_short_head(repo)
    return SelfInfo(
        mode="frozen" if frozen else "source",
        version=__version__,
        build_commit=commit,
        build_time=btime,
        exe_path=str(Path(sys.executable).resolve()) if frozen else None,
        repo_root=str(repo) if repo else None,
        project_dir=str(proj) if proj else None,
    )


# ── update check (queries the remote; safe/read-only) ─────────────────────────
async def _run(args: list[str], cwd: Optional[Path], timeout: int = 30) -> tuple[str, int]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode("utf-8", errors="replace").strip(), proc.returncode or 0
    except asyncio.TimeoutError:
        return "(timed out)", 124
    except (OSError, FileNotFoundError) as e:
        return f"(failed: {e})", 1


async def _remote_head(repo_root: Optional[Path]) -> str:
    """The upstream tip commit (full sha), via ``git ls-remote`` if git is present,
    else the GitHub API (so a git-less frozen exe can still tell it's behind).
    Returns ``""`` if the remote can't be reached."""
    if shutil.which("git"):
        if repo_root is not None:
            out, code = await _run(["git", "ls-remote", "origin", "HEAD"], repo_root)
        else:
            out, code = await _run(["git", "ls-remote", REPO_URL, "HEAD"], None)
        if code == 0 and out and not out.startswith("("):
            return out.split()[0]
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_GITHUB_API, headers={"Accept": "application/vnd.github+json"})
        if resp.status_code == 200:
            return str(resp.json().get("sha", "") or "")
    except Exception:
        pass
    return ""


@dataclass
class UpdateStatus:
    state: str        # "up-to-date" | "behind" | "unknown"
    local: str        # short local/build commit (display)
    remote: str       # upstream commit
    detail: str       # why, when unknown

    @property
    def behind(self) -> bool:
        return self.state == "behind"

    @property
    def summary(self) -> str:
        if self.state == "behind":
            return f"UPDATE AVAILABLE — you're at {self.local or '?'}, upstream is at {self.remote[:8] or '?'}"
        if self.state == "up-to-date":
            return f"up to date (at {self.local or '?'})"
        return f"update status unknown ({self.detail})"


async def check_self_update(info: Optional[SelfInfo] = None) -> UpdateStatus:
    """Compare this manager's code against the upstream tip. Read-only."""
    info = info or gather()
    repo = Path(info.repo_root) if info.repo_root else None
    remote = await _remote_head(repo)
    if not remote:
        return UpdateStatus("unknown", info.build_commit, "", "couldn't reach the remote")
    if info.mode == "source" and repo is not None:
        out, code = await _run(["git", "rev-parse", "HEAD"], repo)
        local = out if code == 0 and not out.startswith("(") else ""
    else:
        local = info.build_commit  # frozen: the stamped build commit (short)
    if not local:
        why = ("this exe wasn't stamped with a build commit"
               if info.mode == "frozen" else "couldn't read the local revision")
        return UpdateStatus("unknown", local, remote, why)
    if remote.startswith(local) or local.startswith(remote):
        return UpdateStatus("up-to-date", local[:8], remote[:8], "")
    return UpdateStatus("behind", local[:8], remote, "")

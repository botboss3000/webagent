"""Update check — compare the linked checkout against the public repo.

Read-only: uses ``git ls-remote`` (queries the remote without fetching or
touching the working tree), so it never modifies anything. If updates exist, the
agent offers to pull via the normal git tool.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from .base import ToolContext


async def _git(args: list[str], cwd: Path, timeout: int = 30) -> tuple[str, int]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode("utf-8", errors="replace").strip(), proc.returncode or 0
    except asyncio.TimeoutError:
        return "(git timed out)", 124
    except (OSError, FileNotFoundError) as e:
        return f"(failed to run git: {e})", 1


async def check_updates(ctx: ToolContext) -> str:
    if ctx.project_root is None:
        return "Error: no checkout linked."
    root = ctx.project_root
    branch, _ = await _git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    local, code = await _git(["rev-parse", "HEAD"], root)
    if code != 0:
        return f"Couldn't read the local revision: {local}"
    remote_out, code = await _git(["ls-remote", "origin", "HEAD"], root)
    if code != 0 or not remote_out:
        return f"Couldn't reach the remote to check for updates: {remote_out}"
    remote = remote_out.split()[0]
    if remote.startswith(local[:12]) or local.startswith(remote[:12]):
        return f"[update] up to date (branch {branch}, at {local[:8]})."
    return (f"[update] updates available — remote is at {remote[:8]}, you're at {local[:8]} "
            f"(branch {branch}). Pull to update (this will fast-forward your checkout).")

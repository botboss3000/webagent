"""Command + Python execution tools (Codebase Admin), self-contained.

``run_command`` runs in the project root and is gated on writes being enabled
(read-only inspection is best done with the fs tools). A small blocklist refuses
the obviously-catastrophic so an autonomous loop can't wipe the machine; this is
predictability insurance, not a sandbox.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

from .base import WRITES_DISABLED_MSG, ToolContext

# Patterns that should never run unattended.
_BLOCKED = [
    re.compile(r"\brm\s+-rf\s+(/|~|\$HOME)(\s|$)"),
    re.compile(r":\(\)\s*\{.*\}\s*;"),            # fork bomb
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+if=.*of=/dev/"),
    re.compile(r">\s*/dev/sd"),
    re.compile(r"\bgit\b.*\bpush\b.*(--force|-f)\b"),  # force-push via shell
]


async def run_command(ctx: ToolContext, command: str, timeout: int = 60) -> str:
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    for pat in _BLOCKED:
        if pat.search(command):
            return f"Refused: command matches a blocked pattern ({pat.pattern})."
    try:
        proc = await asyncio.create_subprocess_shell(
            command, cwd=str(ctx.project_root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        code = proc.returncode or 0
    except asyncio.TimeoutError:
        ctx.audit("run_command", {"cmd": command}, False, "timeout")
        return f"(command timed out after {timeout}s)"
    except OSError as e:
        ctx.audit("run_command", {"cmd": command}, False, str(e))
        return f"(failed to run: {e})"
    text = out.decode("utf-8", errors="replace")
    ctx.audit("run_command", {"cmd": command}, code == 0, f"exit={code}")
    head = text[:6000] + ("\n... (truncated)" if len(text) > 6000 else "")
    return f"[exit {code}]\n{head}" if head.strip() else f"[exit {code}] (no output)"


async def run_python(ctx: ToolContext, code: str, timeout: int = 60) -> str:
    """Run a short Python snippet with the project root on sys.path."""
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code, cwd=str(ctx.project_root),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={"PYTHONPATH": str(ctx.project_root)},
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        code_rc = proc.returncode or 0
    except asyncio.TimeoutError:
        return f"(python timed out after {timeout}s)"
    except OSError as e:
        return f"(failed to run python: {e})"
    text = out.decode("utf-8", errors="replace")
    ctx.audit("run_python", {"code": code[:200]}, code_rc == 0, f"exit={code_rc}")
    return f"[exit {code_rc}]\n{text[:6000]}"

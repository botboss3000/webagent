"""Source Control tools — structured git, self-contained.

Ports the op-safety model from ``plugins/admin/source_tools.py`` (``git_tool`` +
``resolve_conflict``). Read-only ops always run; mutating ops require writes to
be enabled. **Force-push is hard-blocked** and published history is never
rewritten — matching the Source Controller agent's rules.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shlex
from pathlib import Path

from ..safety import resolve_path
from .base import WRITES_DISABLED_MSG, ToolContext

SAFE_OPS = {
    "status", "log", "diff", "show", "branch", "stash", "remote", "config",
    "ls-files", "rev-parse", "blame", "describe", "for-each-ref", "reflog",
    "cat-file", "shortlog", "name-rev", "tag", "worktree",
}
MUTATING_OPS = {
    "add", "commit", "push", "pull", "fetch", "reset", "checkout", "restore",
    "switch", "merge", "rebase", "cherry-pick", "revert", "clean", "mv", "rm",
    "apply", "stash apply", "stash pop", "stash drop",
}

_FORCE_RE = re.compile(r"(^|\s)(--force(-with-lease)?|-f|\+)")


def _git_env(token: str) -> dict[str, str]:
    """Environment for a git subprocess. Always disable interactive credential
    prompts (so a missing/invalid token fails fast instead of hanging the TUI).
    When a token is given, inject an Authorization header for github.com via
    git's env-config mechanism — keeps the token out of argv and out of
    .git/config, and only applies to github.com HTTPS requests."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        cred = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
        env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {cred}"
    return env


async def _run_git(args: list[str], cwd: Path, timeout: int = 60,
                   git_token: str = "") -> tuple[str, int]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=_git_env(git_token),
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode("utf-8", errors="replace"), proc.returncode or 0
    except asyncio.TimeoutError:
        return "(git timed out)", 124
    except (OSError, FileNotFoundError) as e:
        return f"(failed to run git: {e})", 1


async def git_tool(ctx: ToolContext, operation: str, args: str = "") -> str:
    op = operation.lower().strip()
    if op not in SAFE_OPS and op not in MUTATING_OPS:
        known = ", ".join(sorted(SAFE_OPS | MUTATING_OPS))
        return f"Error: unknown git operation '{operation}'. Known: {known}"

    is_mutating = op in MUTATING_OPS
    if is_mutating and not ctx.writes_enabled:
        return WRITES_DISABLED_MSG

    # Hard block on force-push / history rewrite over the network.
    if op == "push" and _FORCE_RE.search(args or ""):
        return "Refused: force-push is not allowed. Pull/rebase and retry, or resolve upstream."

    git_args = op.split()
    if args:
        git_args.extend(shlex.split(args))

    out, code = await _run_git(git_args, ctx.project_root, git_token=ctx.git_token)
    if is_mutating:
        ctx.audit("git_tool", {"op": op, "args": args}, code == 0, out[:500])
    body = out.strip() or (f"(exit {code})" if code else "(no output)")
    return f"[git {op}] {body}"


_CONFLICT_RE = re.compile(
    r"^<{7} .*?\n(.*?)^={7}\n(.*?)^>{7} .*?\n", re.DOTALL | re.MULTILINE
)


async def resolve_conflict(ctx: ToolContext, path: str, choice: str = "ours") -> str:
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    choice = choice.lower().strip()
    if choice not in {"ours", "theirs", "both"}:
        return "Error: choice must be 'ours', 'theirs', or 'both'"
    safe = resolve_path(path, ctx.project_root)
    if not safe.exists():
        return f"Error: file not found: {path}"
    text = await asyncio.to_thread(safe.read_text, encoding="utf-8")

    count = 0

    def _sub(m: re.Match) -> str:
        nonlocal count
        count += 1
        ours, theirs = m.group(1), m.group(2)
        return ours if choice == "ours" else theirs if choice == "theirs" else ours + theirs

    new_text = _CONFLICT_RE.sub(_sub, text)
    if count == 0:
        return f"No conflict markers found in {path}."
    await asyncio.to_thread(safe.write_text, new_text, encoding="utf-8")
    ctx.audit("resolve_conflict", {"path": path, "choice": choice}, True, f"{count} blocks")
    return f"Resolved {count} conflict block(s) in {path} — kept '{choice}'."

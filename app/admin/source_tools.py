"""
Built-in source management tools injected by loader.py.

Gives the agent full filesystem access: read, write, edit, delete, shell commands.
Delete this file (along with source.py) to lock down the agent.

New tools added v4: search_source, read_directory, git_tool
"""

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.tools.loader import ToolInfo

logger = logging.getLogger(__name__)

BASE = "http://localhost:8080"

# ── Helpers ───────────────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # app/../


def _safe_path(path: str) -> Optional[Path]:
    """Resolve a path relative to the project root. Return None if outside."""
    p = Path(path)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    p = p.resolve()
    if not str(p).startswith(str(_PROJECT_ROOT)):
        return None  # outside project root
    return p


def _run_subprocess(cmd: List[str], timeout: int = 30) -> dict:
    """Run a subprocess and return stdout, stderr, exit_code."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"stdout": r.stdout, "stderr": r.stderr, "exit_code": r.returncode}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "Timed out after %ds" % timeout, "exit_code": -1}
    except FileNotFoundError:
        return {"stdout": "", "stderr": "Command not found: %s" % cmd[0], "exit_code": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": -1}


# ── HTTP helpers (existing tools) ─────────────────────────────────────────────

async def _api_get(path: str, params: dict = None):
    import httpx
    async with httpx.AsyncClient(base_url=BASE, timeout=15) as c:
        r = await c.get(path, params=params or {})
        r.raise_for_status()
        return r.json()


async def _api_post(path: str, json_data: dict):
    import httpx
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        r = await c.post(path, json=json_data)
        return r


# ── Main injection entry point ────────────────────────────────────────────────

def inject_source_tools(tools: dict, user_id: str) -> None:
    """Inject filesystem tools into the tools dict."""

    # ── read_source: no confirmation needed, read only ──
    async def _read_source(path: str) -> str:
        """Read file contents. Safe — no changes made."""
        data = await _api_get("/admin/source/read", {"path": path})
        return data["content"]

    tools["read_source"] = ToolInfo(
        name="read_source",
        handler=_read_source,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file (relative to project root or absolute)"},
            },
            "required": ["path"],
        },
    )

    # ── write_source: no permission needed (just write) ──
    async def _write_source(path: str, content: str) -> str:
        """Overwrite a file with new content. Backup created automatically."""
        resp = await _api_post("/admin/source/write", {
            "path": path, "content": content, "create_backup": True,
        })
        if resp.status_code != 200:
            return f"Error: {resp.json().get('detail', 'unknown error')}"
        return resp.json()["message"]

    tools["write_source"] = ToolInfo(
        name="write_source",
        handler=_write_source,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file (relative to project root or absolute)"},
                "content": {"type": "string", "description": "Full content to write to the file"},
            },
            "required": ["path", "content"],
        },
    )

    # ── edit_source: no permission needed (just edit) ──
    async def _edit_source(path: str, old_text: str, new_text: str) -> str:
        """Edit a file by replacing exact text."""
        try:
            data = await _api_get("/admin/source/read", {"path": path})
            current = data["content"]
        except Exception:
            return f"Error: could not read {path}. Does it exist?"

        if old_text not in current:
            return f"Error: old_text not found in {path}"

        new_content = current.replace(old_text, new_text, 1)
        if new_content == current:
            return f"Error: replacement produced no change"

        resp = await _api_post("/admin/source/write", {
            "path": path, "content": new_content, "create_backup": True,
        })
        if resp.status_code != 200:
            return f"Error: {resp.json().get('detail', 'unknown error')}"
        result = resp.json()
        bkp = result.get("backup_path", "none")
        return f"{result['message']}. Backup: {bkp}"

    tools["edit_source"] = ToolInfo(
        name="edit_source",
        handler=_edit_source,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file"},
                "old_text": {"type": "string", "description": "Exact text to find and replace (must match exactly)"},
                "new_text": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    )

    # ── delete_source: user commands → do it; own initiative → ask ──
    async def _delete_source(path: str, recursive: bool = False) -> str:
        """Delete a file or directory. Call directly when user commands it; ask first when deciding on your own."""
        resp = await _api_post("/admin/source/delete", {
            "path": path, "recursive": recursive,
        })
        if resp.status_code != 200:
            return f"Error: {resp.json().get('detail', 'unknown error')}"
        return resp.json()["message"]

    tools["delete_source"] = ToolInfo(
        name="delete_source",
        handler=_delete_source,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file or directory to delete"},
                "recursive": {"type": "boolean", "description": "Set true to delete directories", "default": False},
            },
            "required": ["path"],
        },
    )

    # ── run_command: read-only safe, mutating needs permission ──
    async def _run_command(command: str, timeout: int = 30) -> dict:
        """Execute a shell command. Read-only commands run freely. Mutating commands explained first."""
        resp = await _api_post("/admin/source/exec", {
            "command": command, "timeout": timeout,
        })
        return resp.json()

    tools["run_command"] = ToolInfo(
        name="run_command",
        handler=_run_command,
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30},
            },
            "required": ["command"],
        },
    )

    # ── restart_server: always needs confirmation ──
    async def _restart_server() -> str:
        """Restart the web agent server. Ask the user before calling."""
        import httpx
        async with httpx.AsyncClient(base_url=BASE, timeout=5) as c:
            try:
                resp = await c.post("/api/v1/restart")
                return f"Server restarting: {resp.json().get('message', 'ok')}"
            except Exception as e:
                return f"Restart signal sent: {e}"

    tools["restart_server"] = ToolInfo(
        name="restart_server",
        handler=_restart_server,
        parameters={"type": "object", "properties": {}, "required": []},
    )

    # ── NEW: search_source — structured grep ──
    async def _search_source(pattern: str, path: str = ".", file_pattern: str = "") -> str:
        """Search file contents for a regex pattern. Returns matching lines with line numbers."""
        search_path = _safe_path(path)
        if not search_path:
            return "Error: path is outside the project root"
        if not search_path.exists():
            return f"Error: path does not exist: {path}"

        # Use rg (ripgrep) if available, fall back to Python regex
        import shutil
        if shutil.which("rg"):
            cmd = ["rg", "-n", "--no-heading", pattern, str(search_path)]
            if file_pattern:
                cmd.extend(["-g", file_pattern])
            result = _run_subprocess(cmd, timeout=30)
            if result["exit_code"] == 0:
                lines = result["stdout"].strip().split("\n")
                if len(lines) > 200:
                    lines = lines[:200]
                    lines.append(f"... ({len(lines)} total, showing first 200)")
                return "\n".join(lines)
            elif result["exit_code"] == 1:
                return "No matches found."
            else:
                return f"Error: {result['stderr']}"

        # Fallback: Python re.search
        try:
            compiled = re.compile(pattern)
        except re.error as e:
            return f"Error: invalid regex pattern: {e}"

        matches: List[str] = []
        if search_path.is_file():
            files_to_search = [search_path]
        else:
            files_to_search = list(search_path.rglob("*"))
            if file_pattern:
                files_to_search = [f for f in files_to_search
                                   if f.is_file() and f.match(file_pattern)]

        for fpath in files_to_search:
            if not fpath.is_file():
                continue
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace")
                for i, line in enumerate(text.split("\n"), 1):
                    if compiled.search(line):
                        rel = fpath.relative_to(_PROJECT_ROOT)
                        matches.append(f"{rel}:{i}:{line.rstrip()[:200]}")
                        if len(matches) >= 200:
                            matches.append(f"... ({len(matches)} total, showing first 200)")
                            return "\n".join(matches)
            except (OSError, UnicodeDecodeError):
                continue

        if not matches:
            return "No matches found."
        return "\n".join(matches)

    tools["search_source"] = ToolInfo(
        name="search_source",
        handler=_search_source,
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {"type": "string", "description": "Directory or file to search (default: project root)", "default": "."},
                "file_pattern": {"type": "string", "description": "Optional file glob filter (e.g. '*.py', '*.json')", "default": ""},
            },
            "required": ["pattern"],
        },
    )

    # ── NEW: read_directory — list files with metadata ──
    async def _read_directory(path: str = ".", depth: int = 1) -> str:
        """List files and directories with size and modification time."""
        search_path = _safe_path(path)
        if not search_path:
            return "Error: path is outside the project root"
        if not search_path.exists():
            return f"Error: path does not exist: {path}"
        if not search_path.is_dir():
            return "Error: path is not a directory"

        lines: List[str] = []
        root = search_path

        def _walk(p: Path, current_depth: int):
            if current_depth > depth:
                return
            try:
                entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name))
            except PermissionError:
                return
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                indent = "  " * current_depth
                if entry.is_dir():
                    lines.append(f"{indent}{entry.name}/")
                    _walk(entry, current_depth + 1)
                else:
                    try:
                        size = entry.stat().st_size
                        if size < 1024:
                            size_str = f"{size}B"
                        elif size < 1024 * 1024:
                            size_str = f"{size / 1024:.0f}K"
                        else:
                            size_str = f"{size / 1024 / 1024:.1f}M"
                        lines.append(f"{indent}{entry.name}  ({size_str})")
                    except OSError:
                        lines.append(f"{indent}{entry.name}")

        _walk(root, 0)

        if not lines:
            return "(empty directory)"
        summary = f"{search_path} — {len(lines)} entries\n"
        return summary + "\n".join(lines)

    tools["read_directory"] = ToolInfo(
        name="read_directory",
        handler=_read_directory,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path (default: project root)", "default": "."},
                "depth": {"type": "integer", "description": "How deep to recurse (default: 1, use 0 for flat, 5 for deep)", "default": 1},
            },
            "required": [],
        },
    )

    # ── NEW: git_tool — structured git operations ──
    async def _git_tool(operation: str, args: str = "") -> str:
        """Run a structured git operation. Read-only by default; mutating needs confirmation."""
        safe_ops = {"status", "log", "diff", "show", "branch", "stash", "remote", "config", "ls-files", "rev-parse"}
        mutating_ops = {"add", "commit", "push", "pull", "reset", "checkout", "merge", "rebase", "stash apply"}

        op = operation.lower().strip()
        if op not in safe_ops and op not in mutating_ops:
            return f"Error: unknown git operation '{operation}'. Known: {', '.join(sorted(safe_ops | mutating_ops))}"

        cmd = ["git", op]
        if args:
            # Split args respecting quotes
            import shlex
            cmd.extend(shlex.split(args))

        result = _run_subprocess(cmd, timeout=30)
        output = result["stdout"] or result["stderr"]
        if result["exit_code"] != 0 and not output:
            output = f"Command failed (exit {result['exit_code']})"

        is_mutating = op in mutating_ops
        prefix = f"[Git {op}] "
        if is_mutating:
            prefix = f"[Git {op} — MUTATING, confirm first] "
        return prefix + (output.strip() or "(no output)")

    tools["git_tool"] = ToolInfo(
        name="git_tool",
        handler=_git_tool,
        parameters={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "description": "Git operation to run. Read-only: status, log, diff, show, branch, stash, remote, config, ls-files, rev-parse. Mutating (ask first): add, commit, push, pull, reset, checkout, merge, rebase.",
                },
                "args": {"type": "string", "description": "Additional arguments (e.g. '--oneline -5' for log)", "default": ""},
            },
            "required": ["operation"],
        },
    )

    logger.info("Injected filesystem tools (read/write/edit/delete/command/restart + search/dir/git)")
"""
Built-in source management tools injected by loader.py.

Gives the agent full filesystem access: read, write, edit, delete, shell commands,
Python execution, browser testing, and git operations.

Delete this file (along with source.py) to lock down the agent.
"""

import difflib
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

try:
    from app.admin.guardrails import check_path as _guard_check_path  # type: ignore
except Exception:  # pragma: no cover
    async def _guard_check_path(path: str, action: str = "read") -> None:  # type: ignore
        return None


def _safe_path(path: str) -> Optional[Path]:
    """Resolve a path relative to the project root. Return None if outside."""
    p = Path(path)
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    p = p.resolve()
    if not str(p).startswith(str(_PROJECT_ROOT)):
        return None  # outside project root
    return p


def _mini_diff(before: str, after: str, path: str, context: int = 2) -> str:
    """Return a compact unified diff snippet showing only the changed hunks."""
    before_lines = before.splitlines(keepends=False)
    after_lines = after.splitlines(keepends=False)
    diff = difflib.unified_diff(
        before_lines, after_lines,
        fromfile=path, tofile=path, n=context, lineterm="",
    )
    out = list(diff)
    if not out:
        return "(no textual change)"
    text = "\n".join(out)
    if len(text) > 2000:
        text = text[:2000] + "\n... (diff truncated)"
    return text


def _run_subprocess(cmd: List[str], timeout: int = 30) -> dict:
    """Run a subprocess and return stdout, stderr, exit_code."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"stdout": r.stdout, "stderr": r.stderr, "exit_code": r.returncode}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"Timed out after {timeout}s", "exit_code": -1}
    except FileNotFoundError:
        return {"stdout": "", "stderr": f"Command not found: {cmd[0]}", "exit_code": -1}
    except Exception as e:
        return {"stdout": "", "stderr": str(e), "exit_code": -1}


def _write_file_direct(path: Path, content: str) -> str:
    """Write content to a file directly (no HTTP proxy). Creates backup."""
    backup = path.with_suffix(path.suffix + ".bak")
    try:
        if path.exists():
            import shutil
            shutil.copy2(str(path), str(backup))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path} (backup: {backup.name})"
    except Exception as e:
        return f"Error writing {path}: {e}"


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

    # ═══════════════════════════════════════════════════════════════════════
    # FILE OPERATIONS
    # ═══════════════════════════════════════════════════════════════════════

    # ── read_source: direct disk read, offset/limit support ──
    async def _read_source(path: str, offset: int = 1, limit: int = 2000) -> str:
        """Read file contents from disk. Supports line-range slicing for large files.

        offset is 1-indexed (line numbers shown in cat -n format).
        Default limit is 2000 lines; the response includes a truncation hint
        when the file extends beyond the returned slice."""
        safe = _safe_path(path)
        if not safe:
            return "Error: path is outside the project root"
        try:
            await _guard_check_path(str(safe), "read")
        except PermissionError as e:
            return f"Error: {e}"
        if not safe.exists():
            return f"Error: file not found: {path}"
        if not safe.is_file():
            return f"Error: not a file: {path}"

        try:
            text = safe.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"Error: {path} is not UTF-8 text"

        lines = text.split("\n")
        total = len(lines)
        if offset < 1:
            offset = 1
        if limit <= 0:
            limit = 2000
        start = offset - 1
        end = min(start + limit, total)
        sliced = lines[start:end]
        # Render with line numbers so the agent can re-target patches precisely
        width = len(str(end if end > 0 else 1))
        body = "\n".join(f"{str(i + 1).rjust(width)}\t{ln}"
                         for i, ln in enumerate(sliced, start=start))
        header = f"{path} ({total} lines, showing {offset}-{end})"
        suffix = ""
        if end < total:
            suffix = (f"\n... ({total - end} more lines — call read_source("
                     f"path={path!r}, offset={end + 1}, limit={limit}) to continue)")
        return f"{header}\n{body}{suffix}"

    tools["read_source"] = ToolInfo(
        name="read_source",
        handler=_read_source,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file (relative to project root or absolute)"},
                "offset": {"type": "integer", "description": "First line to return (1-indexed). Default 1.", "default": 1},
                "limit": {"type": "integer", "description": "Max lines to return. Default 2000.", "default": 2000},
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

    # ── edit_source: exact find-and-replace (kept for backward compat) ──
    async def _edit_source(path: str, old_text: str, new_text: str) -> str:
        """Edit a file by replacing exact text. For fuzzy matching, use patch_source."""
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

    # ── patch_source: FUZZY find-and-replace (preferred over edit_source) ──
    async def _patch_source(path: str, old_string: str, new_string: str) -> str:
        """Edit a file by replacing text. Uses fuzzy matching — handles
        whitespace differences, indentation changes, and partial matches.

        Tries up to 9 matching strategies:
          1. Exact match
          2. Whitespace-normalized match
          3. Truncated old_string match
          4. Unique-line match (single unique line from old_string)
          5. Context-anchored match (first + last lines)
          6-9. Progressive character truncation from the end
        """
        safe = _safe_path(path)
        if not safe:
            return "Error: path is outside the project root"
        if not safe.exists():
            return f"Error: file not found: {path}"
        current = safe.read_text(encoding="utf-8")
        old_stripped = old_string.strip()
        new_stripped = new_string.strip()

        # Strategy 1: exact match
        if old_string in current:
            result = current.replace(old_string, new_string, 1)
            _write_file_direct(safe, result)
            return f"Patched (exact match) — {path}\n\n{_mini_diff(current, result, path)}"

        # Strategy 2: whitespace-normalized match
        def _norm(s):
            return re.sub(r'\s+', ' ', s).strip()

        norm_old = _norm(old_string)
        norm_current = _norm(current)
        if norm_old in norm_current:
            idx = norm_current.index(norm_old)
            # Reconstruct approximate span
            lines = current.split('\n')
            char_count = 0
            start_line = 0
            for i, line in enumerate(lines):
                next_count = char_count + len(line) + 1
                stripped_line = ' '.join(line.split())
                if idx >= char_count and idx < next_count and stripped_line == norm_old.split('\n')[0] if '\n' in old_string else stripped_line == norm_old:
                    start_line = i
                    break
                char_count = next_count
            result = current.replace(old_string, new_string, 1)
            # If whitespace match didn't replace, try reconstructing
            if result == current:
                old_lines = old_string.split('\n')
                found = False
                for i in range(len(lines) - len(old_lines) + 1):
                    chunk = '\n'.join(lines[i:i + len(old_lines)])
                    if _norm(chunk) == norm_old:
                        leading = '\n'.join(lines[:i])
                        trailing = '\n'.join(lines[i + len(old_lines):])
                        sep = '\n' if leading and trailing else ''
                        result = leading + sep + new_string + sep + trailing
                        found = True
                        break
                if found:
                    _write_file_direct(safe, result)
                    return f"Patched (fuzzy whitespace match) — {path}\n\n{_mini_diff(current, result, path)}"

        # Strategy 3: truncated old_string (remove trailing context)
        for truncate_to in range(len(old_string) - 1, len(old_string) // 2, -1):
            truncated = old_string[:truncate_to]
            if truncated in current:
                # Replace the first occurrence of the truncated string
                result = current.replace(truncated, new_string, 1)
                if result != current:
                    _write_file_direct(safe, result)
                    return f"Patched (truncated match to {truncate_to} chars) — {path}\n\n{_mini_diff(current, result, path)}"

        # Strategy 4: unique-line match
        old_lines = old_string.strip().split('\n')
        unique_lines = [l.strip() for l in old_lines if len(l.strip()) > 10]
        for ul in unique_lines:
            if ul in current:
                result = current.replace(ul, new_string, 1)
                if result != current:
                    _write_file_direct(safe, result)
                    return f"Patched (unique-line match: '{ul[:50]}...') — {path}\n\n{_mini_diff(current, result, path)}"

        return (f"Error: could not find a match for old_string in {path}. "
                "Two-strike rule: do NOT retry patch_source with another variant. "
                "Instead, read_source the exact region (with offset/limit), then either "
                "patch_source with the exact text or write_source the whole file.")

    tools["patch_source"] = ToolInfo(
        name="patch_source",
        handler=_patch_source,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to edit"},
                "old_string": {"type": "string", "description": "Text to find and replace (fuzzy matching — handles whitespace/differs)"},
                "new_string": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_string", "new_string"],
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

    # ═══════════════════════════════════════════════════════════════════════
    # SHELL / EXECUTION
    # ═══════════════════════════════════════════════════════════════════════

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

    # ── run_python: execute Python code directly ──
    async def _run_python(code: str, timeout: int = 30) -> str:
        """Execute Python code and return stdout. The code runs as a subprocess
        in the project root directory. Use for: testing logic, validating changes,
        computing values, transforming data.

        Import standard library modules freely. External packages may not be
        available if not installed.
        """
        if not code.strip():
            return "Error: no code provided"
        cmd = [sys_executable := "python", "-c", code] if os.name != 'nt' else ["python", "-c", code]
        # Use the same python that's running the server if possible
        try:
            import sys
            cmd[0] = sys.executable
        except Exception:
            pass

        result = _run_subprocess(cmd, timeout=timeout)
        output_parts = []
        if result["stdout"]:
            output_parts.append(result["stdout"].strip())
        if result["stderr"]:
            output_parts.append(f"[stderr]\n{result['stderr'].strip()}")
        if not output_parts:
            if result["exit_code"] == 0:
                return "(no output)"
            return f"Process exited {result['exit_code']} (no output)"
        full = "\n".join(output_parts)
        if len(full) > 10000:
            full = full[:10000] + f"\n... (truncated, {len(full)} total)"
        return full

    tools["run_python"] = ToolInfo(
        name="run_python",
        handler=_run_python,
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)", "default": 30},
            },
            "required": ["code"],
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

    # ═══════════════════════════════════════════════════════════════════════
    # SEARCH / DISCOVERY
    # ═══════════════════════════════════════════════════════════════════════

    # ── search_source: structured grep ──
    async def _search_source(pattern: str, path: str = ".", file_pattern: str = "") -> str:
        """Search file contents for a regex pattern. Returns matching lines with line numbers."""
        search_path = _safe_path(path)
        if not search_path:
            return "Error: path is outside the project root"
        if not search_path.exists():
            return f"Error: path does not exist: {path}"

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

        try:
            compiled = re.compile(pattern)
        except re.error as e:
            return f"Error: invalid regex pattern: {e}"

        matches: List[str] = []
        if search_path.is_file():
            files_to_search = [search_path]
        else:
            # Exclude large/irrelevant dirs to avoid hangs
            _EXCLUDE_DIRS = {"__pycache__", "node_modules", ".git", ".venv",
                             "venv", "env", ".source-backups", "temp",
                             ".mypy_cache", ".pytest_cache", ".cache"}
            all_files = []
            for f in search_path.rglob("*"):
                if any(part in _EXCLUDE_DIRS for part in f.parts):
                    continue
                all_files.append(f)
                if len(all_files) > 50000:
                    return "Error: too many files to search (50,000+). Narrow your search path or file pattern."
            files_to_search = all_files
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

    # ── read_directory: list files with metadata ──
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

        _EXCLUDE_DIRS = {"__pycache__", "node_modules", ".git", ".venv",
                         "venv", "env", ".source-backups", "temp",
                         ".mypy_cache", ".pytest_cache", ".cache"}

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
                if entry.is_dir() and entry.name in _EXCLUDE_DIRS:
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

        _walk(search_path, 0)

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

    # ═══════════════════════════════════════════════════════════════════════
    # GIT OPERATIONS
    # ═══════════════════════════════════════════════════════════════════════

    # ── git_tool: structured git operations ──
    async def _git_tool(operation: str, args: str = "") -> str:
        """Run a structured git operation. Read-only by default; mutating needs confirmation."""
        safe_ops = {"status", "log", "diff", "show", "branch", "stash", "remote",
                    "config", "ls-files", "rev-parse", "blame", "describe",
                    "for-each-ref", "reflog", "cat-file", "fsck", "shortlog",
                    "name-rev", "tag", "worktree"}
        mutating_ops = {"add", "commit", "push", "pull", "fetch", "reset",
                        "checkout", "restore", "switch", "merge", "rebase",
                        "cherry-pick", "revert", "stash apply", "stash pop",
                        "stash drop", "clean", "mv", "rm", "apply"}

        op = operation.lower().strip()
        if op not in safe_ops and op not in mutating_ops:
            known = ', '.join(sorted(safe_ops | mutating_ops))
            return f"Error: unknown git operation '{operation}'. Known: {known}"

        git_args = op.split()  # supports "stash apply", "stash pop", etc.
        if args:
            import shlex
            git_args.extend(shlex.split(args))

        # Run through the same helper the Source Control UI's commit/push buttons
        # use, so the agent authenticates identically (GitHub token injected for
        # network ops) and always runs in the project root. Fall back to a plain
        # subprocess if the github module can't be imported.
        try:
            from app.api.github import _run_git, _get_token, _cache_token
            _cache_token(_get_token())
            stdout, stderr, exit_code = _run_git(git_args, timeout=60)
        except Exception:
            result = _run_subprocess(["git"] + git_args, timeout=60)
            stdout, stderr, exit_code = result["stdout"], result["stderr"], result["exit_code"]

        output = stdout or stderr
        if exit_code != 0 and not output:
            output = f"Command failed (exit {exit_code})"

        return f"[Git {op}] " + (output.strip() or "(no output)")

    tools["git_tool"] = ToolInfo(
        name="git_tool",
        handler=_git_tool,
        parameters={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "description": (
                        "Git operation. Read-only (no confirm): status, log, diff, show, "
                        "branch, stash, remote, config, ls-files, rev-parse, blame, "
                        "reflog, tag, describe. Mutating: add, commit, push, pull, fetch, "
                        "reset, checkout, restore, switch, merge, rebase, cherry-pick, "
                        "revert, stash apply/pop/drop, clean. "
                        "For continue/abort on conflicts pass via args (e.g. operation='cherry-pick', args='--continue')."
                    ),
                },
                "args": {"type": "string", "description": "Additional arguments (e.g. '--oneline -5' for log, '--continue' for cherry-pick)", "default": ""},
            },
            "required": ["operation"],
        },
    )

    # ── resolve_conflict: strip merge markers and keep chosen side ──
    _CONFLICT_RE = re.compile(
        r"^<{7} .*?\n(.*?)^={7}\n(.*?)^>{7} .*?\n",
        re.DOTALL | re.MULTILINE,
    )

    async def _resolve_conflict(path: str, choice: str = "ours") -> str:
        """Resolve all conflict markers in a file by keeping one side.

        choice: 'ours' (keep <<<<<<< side), 'theirs' (keep >>>>>>> side),
        or 'both' (keep both sides concatenated, markers removed)."""
        safe = _safe_path(path)
        if not safe:
            return "Error: path is outside the project root"
        if not safe.exists():
            return f"Error: file not found: {path}"
        text = safe.read_text(encoding="utf-8")

        choice = choice.lower().strip()
        if choice not in {"ours", "theirs", "both"}:
            return "Error: choice must be 'ours', 'theirs', or 'both'"

        count = 0

        def _sub(match):
            nonlocal count
            count += 1
            ours, theirs = match.group(1), match.group(2)
            if choice == "ours":
                return ours
            if choice == "theirs":
                return theirs
            return ours + theirs

        new_text = _CONFLICT_RE.sub(_sub, text)
        if count == 0:
            return f"No conflict markers found in {path}."
        _write_file_direct(safe, new_text)
        return f"Resolved {count} conflict block(s) in {path} — kept '{choice}'."

    tools["resolve_conflict"] = ToolInfo(
        name="resolve_conflict",
        handler=_resolve_conflict,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the conflicted file"},
                "choice": {
                    "type": "string",
                    "enum": ["ours", "theirs", "both"],
                    "description": "Which side of every conflict block to keep. 'ours'=HEAD/current, 'theirs'=incoming, 'both'=concatenate.",
                    "default": "ours",
                },
            },
            "required": ["path"],
        },
    )

    logger.info(
        "Injected admin tools: read/write/edit/patch/delete/run/run_python/restart/search/dir/git/resolve_conflict (%d tools)",
        len(tools),
    )


def inject_git_tools(tools: dict, user_id: str) -> None:
    """Inject ONLY the structured git tools (no shell / file / python access).

    Backs the standalone "Git Control" ability: an agent can run version-control
    operations (status / log / diff / commit / push / branch + resolve_conflict)
    without the full Codebase Admin toolset. Reuses the existing `git_tool`
    handler by building the source tools into a throwaway dict and copying just
    the git entries. `setdefault` so that when Codebase Admin is also enabled its
    (identical) git_tool is left in place rather than double-registered.
    """
    tmp: dict = {}
    try:
        inject_source_tools(tmp, user_id)
    except Exception as e:  # pragma: no cover
        logger.warning("Git Control: could not build base source tools: %s", e)
        return
    for name in ("git_tool", "resolve_conflict"):
        if name in tmp:
            tools.setdefault(name, tmp[name])
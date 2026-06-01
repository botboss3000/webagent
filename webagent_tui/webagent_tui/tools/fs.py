"""File + search tools (the Codebase Admin file suite), self-contained.

Vendored lean equivalents of the web app's ``app/admin/source_tools.py`` so the
TUI ``.exe`` carries no dependency on the server package. Path-scoped to the
target project; mutating ops auto-backup to ``.source-backups/``.
"""

from __future__ import annotations

import asyncio
import difflib
import shutil
import time
from pathlib import Path

from ..safety import is_sensitive, resolve_path
from .base import WRITES_DISABLED_MSG, ToolContext

_EXCLUDE_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv",
                 ".source-backups", "dist", "build", ".mypy_cache", ".pytest_cache"}


def _rel(p: Path, root: Path) -> str:
    try:
        return str(p.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(p)


def _backup(path: Path, root: Path) -> Path | None:
    if not path.exists():
        return None
    bdir = root / ".source-backups"
    bdir.mkdir(exist_ok=True)
    dst = bdir / f"{path.name}.{int(time.time())}.bak"
    try:
        shutil.copy2(path, dst)
        return dst
    except OSError:
        return None


def _mini_diff(before: str, after: str, path: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=2,
    )
    text = "".join(diff)
    return text[:2000] + ("\n... (diff truncated)" if len(text) > 2000 else "")


async def read_source(ctx: ToolContext, path: str, offset: int = 1, limit: int = 2000) -> str:
    safe = resolve_path(path, ctx.project_root)
    if not safe.exists():
        return f"Error: file not found: {path}"
    if safe.is_dir():
        return f"Error: {path} is a directory (use read_directory)"
    try:
        text = await asyncio.to_thread(safe.read_text, encoding="utf-8", errors="replace")
    except OSError as e:
        return f"Error reading {path}: {e}"
    lines = text.splitlines()
    start = max(0, offset - 1)
    chunk = lines[start:start + limit]
    numbered = "\n".join(f"{start + i + 1}\t{ln}" for i, ln in enumerate(chunk))
    more = "" if start + limit >= len(lines) else f"\n... ({len(lines) - start - limit} more lines)"
    return numbered + more


async def write_source(ctx: ToolContext, path: str, content: str) -> str:
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    safe = resolve_path(path, ctx.project_root)
    rel = _rel(safe, ctx.project_root)
    backup = _backup(safe, ctx.project_root)
    try:
        await asyncio.to_thread(safe.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(safe.write_text, content, encoding="utf-8")
    except OSError as e:
        ctx.audit("write_source", {"path": rel}, False, str(e))
        return f"Error writing {path}: {e}"
    ctx.audit("write_source", {"path": rel}, True, f"backup={backup}")
    warn = "  ⚠ sensitive file" if is_sensitive(rel) else ""
    return f"Wrote {rel} ({len(content)} bytes){warn}"


async def edit_source(ctx: ToolContext, path: str, old_text: str, new_text: str) -> str:
    """Exact single-occurrence replacement."""
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    safe = resolve_path(path, ctx.project_root)
    if not safe.exists():
        return f"Error: file not found: {path}"
    current = await asyncio.to_thread(safe.read_text, encoding="utf-8")
    count = current.count(old_text)
    if count == 0:
        return "Error: old_text not found (use read_source to see exact content)"
    if count > 1:
        return f"Error: old_text matches {count} times — make it unique"
    result = current.replace(old_text, new_text, 1)
    _backup(safe, ctx.project_root)
    await asyncio.to_thread(safe.write_text, result, encoding="utf-8")
    rel = _rel(safe, ctx.project_root)
    ctx.audit("edit_source", {"path": rel}, True, "")
    return f"Edited {rel}\n\n{_mini_diff(current, result, rel)}"


async def patch_source(ctx: ToolContext, path: str, old_string: str, new_string: str) -> str:
    """Fuzzy find-and-replace: exact, then whitespace-normalized matching."""
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    safe = resolve_path(path, ctx.project_root)
    if not safe.exists():
        return f"Error: file not found: {path}"
    current = await asyncio.to_thread(safe.read_text, encoding="utf-8")
    rel = _rel(safe, ctx.project_root)

    if old_string in current:
        result = current.replace(old_string, new_string, 1)
        _backup(safe, ctx.project_root)
        await asyncio.to_thread(safe.write_text, result, encoding="utf-8")
        ctx.audit("patch_source", {"path": rel}, True, "exact")
        return f"Patched (exact) — {rel}\n\n{_mini_diff(current, result, rel)}"

    # whitespace-normalized line match
    def _norm(s: str) -> str:
        return "\n".join(ln.strip() for ln in s.strip().splitlines())

    cur_lines = current.splitlines(keepends=True)
    target = _norm(old_string)
    n = len(old_string.strip().splitlines())
    for i in range(len(cur_lines) - n + 1):
        window = "".join(cur_lines[i:i + n])
        if _norm(window) == target:
            result = "".join(cur_lines[:i]) + new_string + "".join(cur_lines[i + n:])
            _backup(safe, ctx.project_root)
            await asyncio.to_thread(safe.write_text, result, encoding="utf-8")
            ctx.audit("patch_source", {"path": rel}, True, "fuzzy-whitespace")
            return f"Patched (fuzzy whitespace) — {rel}\n\n{_mini_diff(current, result, rel)}"

    return "Error: old_string not found (exact or whitespace-normalized). Read the region and retry."


async def delete_source(ctx: ToolContext, path: str, recursive: bool = False) -> str:
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    safe = resolve_path(path, ctx.project_root)
    if not safe.exists():
        return f"Error: not found: {path}"
    rel = _rel(safe, ctx.project_root)
    try:
        if safe.is_dir():
            if not recursive:
                return f"Error: {rel} is a directory — pass recursive=true to remove it"
            _backup_dir = ctx.project_root / ".source-backups" / f"{safe.name}.{int(time.time())}"
            await asyncio.to_thread(shutil.copytree, safe, _backup_dir)
            await asyncio.to_thread(shutil.rmtree, safe)
        else:
            _backup(safe, ctx.project_root)
            await asyncio.to_thread(safe.unlink)
    except OSError as e:
        ctx.audit("delete_source", {"path": rel}, False, str(e))
        return f"Error deleting {path}: {e}"
    ctx.audit("delete_source", {"path": rel}, True, "")
    return f"Deleted {rel} (backed up)"


async def search_source(ctx: ToolContext, pattern: str, path: str = ".", file_pattern: str = "") -> str:
    """Plain-text search across the tree (case-insensitive substring)."""
    root = resolve_path(path, ctx.project_root)
    needle = pattern.lower()
    hits: list[str] = []

    def _walk() -> list[str]:
        out: list[str] = []
        for p in root.rglob(file_pattern or "*"):
            if not p.is_file():
                continue
            if any(part in _EXCLUDE_DIRS for part in p.parts):
                continue
            try:
                for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if needle in line.lower():
                        out.append(f"{_rel(p, ctx.project_root)}:{i}: {line.strip()[:160]}")
                        if len(out) >= 200:
                            return out
            except OSError:
                continue
        return out

    hits = await asyncio.to_thread(_walk)
    if not hits:
        return f"No matches for '{pattern}'."
    return "\n".join(hits)


async def read_directory(ctx: ToolContext, path: str = ".", depth: int = 1) -> str:
    root = resolve_path(path, ctx.project_root)
    if not root.exists():
        return f"Error: not found: {path}"
    lines: list[str] = []

    def _walk(p: Path, d: int, prefix: str) -> None:
        if d < 0:
            return
        try:
            entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name))
        except OSError:
            return
        for e in entries:
            if e.name in _EXCLUDE_DIRS:
                continue
            lines.append(f"{prefix}{e.name}{'/' if e.is_dir() else ''}")
            if e.is_dir() and d > 0:
                _walk(e, d - 1, prefix + "  ")

    await asyncio.to_thread(_walk, root, depth, "")
    return "\n".join(lines[:500]) or "(empty)"

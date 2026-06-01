"""Assemble the server manager tool registry (schemas + dispatch).

v1 abilities: **Codebase Admin** (file/search/run) + **Source Control** (git).
The registry exposes OpenAI-style function schemas to the LLM and dispatches
tool calls back to the handlers, injecting the live ``ToolContext``.
"""

from __future__ import annotations

from . import fs, git, shell
from .base import ToolContext, ToolSpec

_STR = {"type": "string"}


def build_specs() -> list[ToolSpec]:
    return [
        # ── Codebase Admin: files ──────────────────────────────────────────
        ToolSpec("read_source", "Read a file region with line numbers.", {
            "type": "object",
            "properties": {
                "path": _STR,
                "offset": {"type": "integer", "default": 1},
                "limit": {"type": "integer", "default": 2000},
            }, "required": ["path"],
        }, fs.read_source),
        ToolSpec("write_source", "Create or overwrite a file (auto-backup). Mutating.", {
            "type": "object",
            "properties": {"path": _STR, "content": _STR},
            "required": ["path", "content"],
        }, fs.write_source, mutating=True),
        ToolSpec("edit_source", "Exact unique find-and-replace in a file. Mutating.", {
            "type": "object",
            "properties": {"path": _STR, "old_text": _STR, "new_text": _STR},
            "required": ["path", "old_text", "new_text"],
        }, fs.edit_source, mutating=True),
        ToolSpec("patch_source", "Fuzzy find-and-replace (whitespace tolerant). Mutating.", {
            "type": "object",
            "properties": {"path": _STR, "old_string": _STR, "new_string": _STR},
            "required": ["path", "old_string", "new_string"],
        }, fs.patch_source, mutating=True),
        ToolSpec("delete_source", "Delete a file or (recursive) directory; backed up. Mutating.", {
            "type": "object",
            "properties": {"path": _STR, "recursive": {"type": "boolean", "default": False}},
            "required": ["path"],
        }, fs.delete_source, mutating=True),
        ToolSpec("search_source", "Case-insensitive text search across the tree.", {
            "type": "object",
            "properties": {"pattern": _STR, "path": {**_STR, "default": "."},
                           "file_pattern": {**_STR, "default": ""}},
            "required": ["pattern"],
        }, fs.search_source),
        ToolSpec("read_directory", "List a directory tree to a given depth.", {
            "type": "object",
            "properties": {"path": {**_STR, "default": "."},
                           "depth": {"type": "integer", "default": 1}},
        }, fs.read_directory),
        # ── Codebase Admin: execution ──────────────────────────────────────
        ToolSpec("run_command", "Run a shell command in the project root. Mutating.", {
            "type": "object",
            "properties": {"command": _STR, "timeout": {"type": "integer", "default": 60}},
            "required": ["command"],
        }, shell.run_command, mutating=True),
        ToolSpec("run_python", "Run a Python snippet with the project on sys.path. Mutating.", {
            "type": "object",
            "properties": {"code": _STR, "timeout": {"type": "integer", "default": 60}},
            "required": ["code"],
        }, shell.run_python, mutating=True),
        # ── Source Control ─────────────────────────────────────────────────
        ToolSpec("git_tool", (
            "Structured git. Read-only: status, log, diff, show, branch, remote, "
            "ls-files, rev-parse, reflog, tag. Mutating: add, commit, push, pull, "
            "fetch, reset, checkout, restore, switch, merge, rebase, cherry-pick, "
            "revert, stash apply/pop/drop. Never force-push."), {
            "type": "object",
            "properties": {"operation": _STR, "args": {**_STR, "default": ""}},
            "required": ["operation"],
        }, git.git_tool, mutating=True),
        ToolSpec("resolve_conflict", "Resolve merge conflicts in a file by keeping one side. Mutating.", {
            "type": "object",
            "properties": {"path": _STR,
                           "choice": {**_STR, "enum": ["ours", "theirs", "both"], "default": "ours"}},
            "required": ["path"],
        }, git.resolve_conflict, mutating=True),
    ]


class ToolRegistry:
    def __init__(self) -> None:
        self._specs = {s.name: s for s in build_specs()}

    def schemas(self) -> list[dict]:
        return [s.openai_schema() for s in self._specs.values()]

    def names(self) -> list[str]:
        return list(self._specs)

    async def dispatch(self, ctx: ToolContext, name: str, args: dict) -> str:
        spec = self._specs.get(name)
        if spec is None:
            return f"Error: unknown tool '{name}'"
        try:
            return await spec.handler(ctx, **args)
        except TypeError as e:
            return f"Error: bad arguments for {name}: {e}"
        except Exception as e:  # never let a tool crash the loop
            ctx.audit(name, args, False, f"{type(e).__name__}: {e}")
            return f"Error in {name}: {type(e).__name__}: {e}"

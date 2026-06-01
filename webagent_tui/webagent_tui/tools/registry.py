"""Assemble the server manager tool registry (schemas + dispatch).

v1 abilities: **Codebase Admin** (file/search/run) + **Source Control** (git).
The registry exposes OpenAI-style function schemas to the LLM and dispatches
tool calls back to the handlers, injecting the live ``ToolContext``.
"""

from __future__ import annotations

from . import diagnostics, fs, git, install, manage, selfupdate, server, shell, update
from .base import ToolContext, ToolSpec

_STR = {"type": "string"}
_INT = {"type": "integer"}


def build_specs() -> list[ToolSpec]:
    return [
        # ── Manager state (available in onboarding mode) ───────────────────
        ToolSpec("link_project", (
            "Link the manager to an EXISTING webAgent checkout (a folder with "
            "run.py + app/). Switches to managed mode and adopts that repo's AI "
            "key. Use when the user already has a copy."), {
            "type": "object",
            "properties": {"path": _STR},
            "required": ["path"],
        }, manage.link_project, needs_project=False),
        ToolSpec("setup_launch_shortcut", (
            "Android/Termux only: write a Termux:Widget home-screen shortcut that "
            "launches the manager (~/.shortcuts/webagent.sh). Use as the FINAL "
            "onboarding step on Termux, then tell the user to install the "
            "Termux:Widget add-on and add the widget. Mutating."), {
            "type": "object", "properties": {},
        }, manage.setup_launch_shortcut, mutating=True, needs_project=False),
        # ── Fresh install (onboarding; operate on a target folder) ─────────
        ToolSpec("check_install_readiness", (
            "Read-only preflight for a fresh install: OS, Python 3.11-3.12, git, "
            "internet, browser capability, and (if given) the target folder's "
            "space/emptiness. Run this first."), {
            "type": "object",
            "properties": {"target": {**_STR, "default": ""}},
        }, install.check_install_readiness, needs_project=False),
        ToolSpec("clone_repo", (
            "Clone the public webAgent repo into an empty/new target folder. "
            "Mutating. Recommended target: C:/webagent (Windows) or ~/webagent."), {
            "type": "object",
            "properties": {"target": _STR},
            "required": ["target"],
        }, install.clone_repo, mutating=True, needs_project=False),
        ToolSpec("setup_environment", (
            "Build the install's virtual environment and install dependencies "
            "(and the headless browser unless unsupported). Slow (minutes). "
            "Mutating."), {
            "type": "object",
            "properties": {"target": _STR, "python_exe": {**_STR, "default": ""},
                           "install_browser": {"type": "boolean", "default": True}},
            "required": ["target"],
        }, install.setup_environment, mutating=True, needs_project=False),
        ToolSpec("seed_config", (
            "Write the install's .env + provider.json (seeding the app's AI key) "
            "+ db_connection.json (local SQLite). Mutating."), {
            "type": "object",
            "properties": {"target": _STR},
            "required": ["target"],
        }, install.seed_config, mutating=True, needs_project=False),
        ToolSpec("verify_install", (
            "Verify a fresh install imports cleanly and its local DB initialises. "
            "Mutating (creates the local database)."), {
            "type": "object",
            "properties": {"target": _STR},
            "required": ["target"],
        }, install.verify_install, mutating=True, needs_project=False),
        # ── Server lifecycle (managed) ─────────────────────────────────────
        ToolSpec("server_status", "Is the local webAgent server up? Read-only.", {
            "type": "object", "properties": {},
        }, server.server_status),
        ToolSpec("server_start", "Start the local webAgent server (detached) and verify health. Mutating.", {
            "type": "object", "properties": {},
        }, server.server_start, mutating=True),
        ToolSpec("server_stop", "Stop the local webAgent server the manager started. Mutating.", {
            "type": "object", "properties": {},
        }, server.server_stop, mutating=True),
        ToolSpec("server_restart", "Restart the local webAgent server. Mutating.", {
            "type": "object", "properties": {},
        }, server.server_restart, mutating=True),
        ToolSpec("server_logs", "Tail the captured server log. Read-only.", {
            "type": "object",
            "properties": {"lines": {**_INT, "default": 40}},
        }, server.server_logs),
        ToolSpec("check_updates", (
            "Check whether the linked checkout is behind the public repo "
            "(read-only). Offer to pull if so."), {
            "type": "object", "properties": {},
        }, update.check_updates),
        ToolSpec("read_diagnostics", (
            "Read the webAgent app's diagnostics — its flight-recorder of "
            "warnings/errors (with tracebacks), agent-loop problems, run outcomes "
            "and tool errors — straight from the checkout's local DB, so it works "
            "even when the server is DOWN. Filter by level (error/warning) or "
            "category (server/http/agent/...). Use it to diagnose a broken server."), {
            "type": "object",
            "properties": {"limit": {**_INT, "default": 20},
                           "level": {**_STR, "default": ""},
                           "category": {**_STR, "default": ""}},
        }, diagnostics.read_diagnostics),
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
        # ── Self-update (the manager updating its OWN code) ─────────────────
        ToolSpec("self_status", (
            "Report how THIS manager is running (source checkout vs frozen exe), "
            "its version/build, where its code lives, and whether a newer version "
            "is available upstream. Read-only."), {
            "type": "object", "properties": {},
        }, selfupdate.self_status, needs_project=False),
        ToolSpec("self_update", (
            "Update the manager's OWN code. Always backs up first (timestamped): "
            "source mode pulls the repo (fast-forward only); a frozen exe rebuilds "
            "itself from fresh source and stages the new exe. Set restart=true to "
            "apply immediately (closes + relaunches the manager). Mutating."), {
            "type": "object",
            "properties": {"make_backup": {"type": "boolean", "default": True},
                           "restart": {"type": "boolean", "default": False}},
        }, selfupdate.self_update, mutating=True, needs_project=False),
        ToolSpec("self_restart", (
            "Close and relaunch the manager — applies a staged exe swap (frozen) "
            "or reloads just-pulled source. Mutating."), {
            "type": "object", "properties": {},
        }, selfupdate.self_restart, mutating=True, needs_project=False),
    ]


class ToolRegistry:
    def __init__(self) -> None:
        self._specs = {s.name: s for s in build_specs()}

    def schemas(self, has_project: bool = True) -> list[dict]:
        """Tool schemas for the LLM. In onboarding mode (no checkout linked),
        only project-independent tools are exposed."""
        return [s.openai_schema() for s in self._specs.values()
                if has_project or not s.needs_project]

    def names(self, has_project: bool = True) -> list[str]:
        return [n for n, s in self._specs.items() if has_project or not s.needs_project]

    async def dispatch(self, ctx: ToolContext, name: str, args: dict) -> str:
        spec = self._specs.get(name)
        if spec is None:
            return f"Error: unknown tool '{name}'"
        if spec.needs_project and ctx.project_root is None:
            return f"Error: '{name}' needs a linked webAgent checkout. Link one first."
        try:
            return await spec.handler(ctx, **args)
        except TypeError as e:
            return f"Error: bad arguments for {name}: {e}"
        except Exception as e:  # never let a tool crash the loop
            ctx.audit(name, args, False, f"{type(e).__name__}: {e}")
            return f"Error in {name}: {type(e).__name__}: {e}"

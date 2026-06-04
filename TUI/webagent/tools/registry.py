"""Assemble the server manager tool registry (schemas + dispatch).

Abilities: **Codebase Admin** (file/search/run), **Source Control** (git),
**Server lifecycle**, **Diagnostics**, and **Monitoring** (the watchdog/alarms).
The registry exposes OpenAI-style function schemas to the LLM and dispatches
tool calls back to the handlers, injecting the live ``ToolContext``.

Each tool's NAME is bound to its handler + parameter schema + mutating/needs_project
flags here in Python, but the human-readable ``description`` and whether a tool is
``enabled`` are overlaid from ``manager/tools.json`` (see ``resources.py``) so the
prose the model reads — and the on/off list — can be edited without touching code.
"""

from __future__ import annotations

from ..resources import load_tool_overrides
from . import (
    appctl,
    delegate,
    diagnostics,
    fs,
    git,
    install,
    manage,
    monitor,
    playbook,
    reset,
    selfupdate,
    webapp,
    server,
    shell,
    update,
    web_search,
)
from .base import ToolContext, ToolSpec

_STR = {"type": "string"}
_INT = {"type": "integer"}
_STRLIST = {"type": "array", "items": {"type": "string"}}


def _apply_overrides(specs: list[ToolSpec]) -> list[ToolSpec]:
    """Overlay ``manager/tools.json`` onto the Python specs: replace each tool's
    human-readable description and drop any whose ``enabled`` is false. A tool
    missing from the file keeps its Python default and stays enabled, so a partial
    or absent file degrades cleanly."""
    overrides = load_tool_overrides()
    if not overrides:
        return specs
    out: list[ToolSpec] = []
    for spec in specs:
        o = overrides.get(spec.name)
        if o is None:
            out.append(spec)
            continue
        if o.get("enabled") is False:
            continue
        desc = o.get("description")
        if isinstance(desc, str) and desc.strip():
            spec.description = desc.strip()
        out.append(spec)
    return out


def build_specs() -> list[ToolSpec]:
    return _apply_overrides(_base_specs())


def _base_specs() -> list[ToolSpec]:
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
        ToolSpec("reset_app", (
            "Reset the linked webAgent install to a clean state (like reset_webagent.bat). "
            "ALWAYS wipes the userbase: the ACTIVE database backend + the per-user generated "
            "pages in visuals/users/. Backend-aware via app/db_connection.json: if Postgres "
            "(postgres/neon/gcp_cloud_sql) is active, it connects through the checkout's venv and "
            "drops+recreates the schema (NOT backed up — irreversible — and the stray SQLite "
            "files are left alone); if SQLite is active, it removes local.db (+ its -journal/-wal/"
            "-shm/.preprompt-bak sidecars and any stray root local.db), backed up unless "
            "backup=false. Opt-in extras: clear_secrets (AI keys, OAuth tokens, integration "
            "creds, scheduler/db-mode config), clear_users (local accounts + remember-me tokens), "
            "delete_env (.env), delete_agents (the agent template JSONs — NO fallback: next start "
            "has zero agents). backup also covers visuals/users + the opt-in file groups. Stops "
            "the server first. The DB, default admin/admin user, and agents are recreated on next "
            "start (if the agent JSONs were kept). Mutating — confirm with the user before running."), {
            "type": "object",
            "properties": {
                "backup": {"type": "boolean", "default": True},
                "clear_secrets": {"type": "boolean", "default": False},
                "clear_users": {"type": "boolean", "default": False},
                "delete_env": {"type": "boolean", "default": False},
                "delete_agents": {"type": "boolean", "default": False},
            },
        }, reset.reset_app, mutating=True),
        ToolSpec("check_updates", (
            "Check whether the linked checkout is behind the public repo "
            "(read-only). Offer to pull if so."), {
            "type": "object", "properties": {},
        }, update.check_updates),
        # ── Drive the running app as a user (the same API the web UI uses) ──
        ToolSpec("app_login", (
            "Log into the RUNNING webAgent app over its HTTP API and cache the "
            "session. Defaults to the local admin (admin/admin). Read-only side "
            "effect (just authentication). Use before app_chat if you want to "
            "confirm credentials first; app_chat also logs in on its own."), {
            "type": "object",
            "properties": {"username": {**_STR, "default": "admin"},
                           "password": {**_STR, "default": "admin"}},
        }, appctl.app_login, needs_project=False),
        ToolSpec("app_list_agents", (
            "List the chat agents available to the logged-in app user (id + name) "
            "so you can target one with app_chat(agent_id=...). Read-only."), {
            "type": "object",
            "properties": {"username": {**_STR, "default": "admin"},
                           "password": {**_STR, "default": "admin"}},
        }, appctl.app_list_agents, needs_project=False),
        ToolSpec("app_chat", (
            "Talk to the RUNNING app's agent AS the logged-in user (default "
            "admin/admin) and return its reply — a one-shot synchronous chat. For an "
            "ongoing live conversation in the connected panel use webapp_send instead. "
            "The app auto-creates the session and uses the user's default agent unless "
            "you pass agent_id. Mutating: the app's agent may run tools, so confirm "
            "intent with the user first. Needs the server running."), {
            "type": "object",
            "properties": {"message": _STR,
                           "session_id": {**_STR, "default": ""},
                           "agent_id": {**_STR, "default": ""},
                           "new_session": {"type": "boolean", "default": False},
                           "username": {**_STR, "default": "admin"},
                           "password": {**_STR, "default": "admin"}},
            "required": ["message"],
        }, appctl.app_chat, mutating=True, needs_project=False),
        ToolSpec("webapp_send", (
            "Send a message to the CONNECTED web-app session (the active target set "
            "in the Connect panel) and return the app agent's reply. Use this when the "
            "WebAgent is unmuted and you're bridged to a session — it holds a real "
            "back-and-forth and the exchange streams into the shared transcript. "
            "Mutating; the app agent may run tools and take real actions."), {
            "type": "object",
            "properties": {"message": _STR},
            "required": ["message"],
        }, webapp.webapp_send, mutating=True, needs_project=False),
        ToolSpec("webapp_status", (
            "Report the connected web-app target (agent + session) and live-stream "
            "health. Read-only."), {
            "type": "object", "properties": {},
        }, webapp.webapp_status, needs_project=False),
        ToolSpec("app_list_sessions", (
            "List the logged-in app user's chat sessions (id, title, agent). Optionally "
            "filter by agent_id. Read-only."), {
            "type": "object",
            "properties": {"agent_id": {**_STR, "default": ""},
                           "limit": {**_INT, "default": 20}},
        }, webapp.app_list_sessions, needs_project=False),
        ToolSpec("app_get_settings", (
            "Read the app's settings (app-settings.json): access mode, presentation "
            "mode, run-watchdog tuning, etc. Read-only."), {
            "type": "object", "properties": {},
        }, webapp.app_get_settings, needs_project=False),
        ToolSpec("app_set_settings", (
            "Update one or more app settings (app-settings.json). Pass an object of the "
            "keys to change; the rest are preserved. Mutating."), {
            "type": "object",
            "properties": {"patch": {"type": "object"}},
            "required": ["patch"],
        }, webapp.app_set_settings, mutating=True, needs_project=False),
        ToolSpec("app_get_auth_keys", (
            "Read the app's LLM provider config (the auth-key entry in the DB): "
            "provider, model, base URL; the API key is masked. Read-only."), {
            "type": "object", "properties": {},
        }, webapp.app_get_auth_keys, needs_project=False),
        ToolSpec("app_set_auth_keys", (
            "Set the app's LLM auth key / provider / base URL / model (writes the DB "
            "auth_elements row + provider.json). Only the fields you pass change; the "
            "key is never echoed. Mutating — confirm with the user first."), {
            "type": "object",
            "properties": {"api_key": {**_STR, "default": ""},
                           "provider": {**_STR, "default": ""},
                           "base_url": {**_STR, "default": ""},
                           "model": {**_STR, "default": ""}},
        }, webapp.app_set_auth_keys, mutating=True, needs_project=False),
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
        # ── Monitoring / alarms (the watchdog) ─────────────────────────────
        # These edit the manager's OWN config (data dir JSON) or just report /
        # notify — they never touch the repo or run commands, so they are not
        # gated behind "Allow writes" and work in onboarding mode too.
        ToolSpec("monitor_status",
                 "Report the background watchdog state (on/off, interval, autonomy, "
                 "channels, thresholds, current health, recent reactions). Read-only.",
                 {"type": "object", "properties": {}},
                 monitor.monitor_status, needs_project=False),
        ToolSpec("list_alarms", "List the current alarm rules. Read-only.",
                 {"type": "object", "properties": {}},
                 monitor.list_alarms, needs_project=False),
        ToolSpec("add_alarm",
                 "Add a standing alarm rule the watchdog evaluates each heartbeat.",
                 {"type": "object",
                  "properties": {"contains": {**_STR, "default": ""},
                                 "level": {**_STR, "default": ""},
                                 "category": {**_STR, "default": ""},
                                 "action": {**_STR, "enum": ["notify", "auto_restart"],
                                            "default": "notify"},
                                 "loudness": {**_STR, "enum": ["every", "once", "digest"],
                                              "default": "every"},
                                 "channels": {"type": "array", "items": _STR, "default": []},
                                 "label": {**_STR, "default": ""}}},
                 monitor.add_alarm, needs_project=False),
        ToolSpec("remove_alarm", "Remove an alarm rule by its id (from list_alarms).",
                 {"type": "object", "properties": {"id": _STR}, "required": ["id"]},
                 monitor.remove_alarm, needs_project=False),
        ToolSpec("set_monitor_config",
                 "Change the watchdog config (enable/disable, interval_seconds, "
                 "autonomy, channels, auto_restart, thresholds).",
                 {"type": "object",
                  "properties": {"enabled": {"type": "boolean"},
                                 "interval_seconds": _INT,
                                 "autonomy": {**_STR,
                                              "enum": ["notify", "auto_restart", "self_heal"]},
                                 "channels": {"type": "array", "items": _STR},
                                 "auto_restart": {"type": "boolean"},
                                 "error_rate_threshold": _INT,
                                 "max_restarts_per_hour": _INT,
                                 "disk_percent_threshold": _INT,
                                 "mem_percent_threshold": _INT,
                                 "cpu_percent_threshold": _INT}},
                 monitor.set_monitor_config, needs_project=False),
        ToolSpec("notify_test", "Send a test notification on the configured channel(s).",
                 {"type": "object", "properties": {"message": {**_STR, "default": ""}}},
                 monitor.notify_test, needs_project=False),
        ToolSpec("server_resources",
                 "Report the server process + host resources right now: host CPU, "
                 "memory, disk, and the webAgent process's memory. Read-only.",
                 {"type": "object", "properties": {}},
                 monitor.server_resources),
        # ── Playbook (self-healing issue knowledge base) ───────────────────
        ToolSpec("playbook_list",
                 "List every issue the watchdog has learned (how often each fired, its "
                 "status, and its best-known remedy + confidence). Read-only.",
                 {"type": "object", "properties": {}},
                 playbook.playbook_list, needs_project=False),
        ToolSpec("playbook_show",
                 "Show one issue in full: its remedies with helped/didn't-help stats and "
                 "the recent incidents (triggers + outcomes). Read-only.",
                 {"type": "object", "properties": {"key": _STR}, "required": ["key"]},
                 playbook.playbook_show, needs_project=False),
        ToolSpec("playbook_record_remedy",
                 "Record a remedy for an issue after diagnosing it so the watchdog can try "
                 "it next time. kind ∈ restart_server|clear_port|escalate_to_agent|"
                 "notify_only|command|note; for 'command' put the shell command in payload. "
                 "Custom commands start 'suggested' and need approval (approved=true) before "
                 "they auto-run.",
                 {"type": "object",
                  "properties": {"issue_key": _STR,
                                 "kind": {**_STR, "enum": list(playbook.CATALOG_KINDS)},
                                 "payload": {**_STR, "default": ""},
                                 "approved": {"type": "boolean", "default": False}},
                  "required": ["issue_key", "kind"]},
                 playbook.playbook_record_remedy, needs_project=False),
        ToolSpec("playbook_set_remedy",
                 "Approve, disable, or re-prioritise a remedy (status ∈ approved|suggested|"
                 "disabled; higher priority breaks ties).",
                 {"type": "object",
                  "properties": {"remedy_id": _STR,
                                 "status": {**_STR, "default": ""},
                                 "priority": _INT},
                  "required": ["remedy_id"]},
                 playbook.playbook_set_remedy, needs_project=False),
        ToolSpec("playbook_forget",
                 "Delete a whole issue (key=...) and its history, or a single remedy "
                 "(remedy_id=...). Use when something was learned wrong.",
                 {"type": "object",
                  "properties": {"key": {**_STR, "default": ""},
                                 "remedy_id": {**_STR, "default": ""}}},
                 playbook.playbook_forget, needs_project=False),
        # ── Web search (any mode) ──────────────────────────────────────────
        ToolSpec("web_search", (
            "Search the web via DuckDuckGo. Returns titles, URLs, and snippets "
            "(no API key required). Use to research errors, find current docs, "
            "check dependency versions, or look up solutions. Read-only."), {
            "type": "object",
            "properties": {
                "query": _STR,
                "max_results": {**_INT, "default": 5},
            },
            "required": ["query"],
        }, web_search.web_search, needs_project=False),
        # ── Delegation: subagents (mk2 event-driven) ───────────────────────
        ToolSpec("delegate_async", (
            "Launch a SUBAGENT to work in the background and continue your own turn "
            "immediately — this does NOT block. Returns a task id. The subagent runs "
            "in its own session and reports back when done. Use 'gather' mode (with a "
            "shared group_id) to fan several tasks out and have them delivered together "
            "once ALL in the group finish; use 'notify' mode for a single task whose "
            "result should come back on its own. The subagent gets ONLY the tools you "
            "list in 'tools' (caller-scoped — pick read-only tools like read_source/"
            "search_source/read_diagnostics for safe investigation, or grant write/run "
            "tools explicitly). You'll see the result as a synthetic message on a later "
            "turn; the situation block lists what's still pending."), {
            "type": "object",
            "properties": {
                "task": _STR,
                "tools": _STRLIST,
                "mode": {**_STR, "enum": ["gather", "notify"], "default": "gather"},
                "group_id": {**_STR, "default": ""},
            },
            "required": ["task", "tools"],
        }, delegate.delegate_async, needs_project=False),
        ToolSpec("delegate_background", (
            "Launch a fire-and-forget SUBAGENT for a side task unrelated to the current "
            "work (e.g. look something up, check a dashboard). Returns a task id and does "
            "NOT block. Its result is stored but NEVER auto-triggers a new turn — pull it "
            "later with check_task if you want it. The subagent gets ONLY the tools you "
            "list in 'tools'."), {
            "type": "object",
            "properties": {"task": _STR, "tools": _STRLIST},
            "required": ["task", "tools"],
        }, delegate.delegate_background, needs_project=False),
        ToolSpec("check_task", (
            "Check a previously delegated subagent task by its id: running / completed / "
            "error, with the result summary if it has finished. Read-only."), {
            "type": "object",
            "properties": {"task_id": _STR},
            "required": ["task_id"],
        }, delegate.check_task, needs_project=False),
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

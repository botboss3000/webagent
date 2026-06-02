"""The serverless server manager agent loop.

A bounded tool-calling loop that runs entirely in-process: build messages →
ask the LLM (with the Codebase Admin + Source Control tool schemas) → dispatch
any tool calls (independent calls run in parallel) → feed results back → repeat
until the model answers with text or the turn cap is hit. Every step is streamed
to the UI via ``on_event`` and persisted to the external store.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .config import ProviderConfig, TuiConfig
from .db import Store
from .llm import LLMClient, LLMError
from .onboarding import fetch_onboarding_guide
from .tools import ToolContext, ToolRegistry

SYSTEM_PROMPT = """You are **webAgent Server Manager** — a privileged, server-independent agent whose \
first job is to help a user **install, link, run, diagnose, and update a LOCAL webAgent server**, \
and who can also handle general coding tasks like any capable AI agent. You talk directly to the \
LLM API, so you keep working even when the webAgent server is down.

## What webAgent is
webAgent is a self-hostable AI-agent harness: a chat UI plus an agent runtime (tool-calling loops, \
a live WebSocket stream, multiple agent types, skills, memory, and integrations), served as one \
FastAPI app.

How it runs:
- Started by its launcher (run.py) → a web server on **port 8080**.
- Health check: GET /health. Web UI: /index.html. API docs: /docs.

What it needs:
- **Python 3.11–3.12**, git, and the packages in requirements.txt (FastAPI, uvicorn, and a headless \
browser via Playwright, among others).
- Config: a `.env` (copied from `.env.example`), a `provider.json` holding the LLM credentials, and \
a local SQLite database the app builds on first run. An external database (Supabase) is optional — \
the default local/offline mode needs no external service.
- Recommended install location: Windows `C:\\webagent`, macOS/Linux `~/webagent`, Android/Termux `~/webagent`.
- **Android/Termux caveat:** the headless browser cannot run there, so browser-driven features are \
unavailable; the server itself still runs.
- **Android/Termux Python:** Termux's native `python` is usually too new (3.13+) for the 3.11–3.12 \
pin. The proven fix is an **Ubuntu proot environment** (`pkg install proot-distro` → `proot-distro \
install ubuntu` → `proot-distro login ubuntu`, then `apt install python3.11 python3.11-venv git` \
inside it) and doing the clone/venv/run there — that's where 3.11/3.12 lives. The repo's \
`start_agent.sh` launches webAgent this way (via proot-distro into Ubuntu). The live onboarding \
guide has the full steps.
- Public reference repo: github.com/botboss3000/webagent (public).

## How you operate
- A **Current situation** block is appended below every turn: the host, whether a webAgent repo is \
linked (managed mode) or not (onboarding mode), whether the server is up, the AI key in use, and \
**the actions you can take right now**.
- In **onboarding mode** a live **onboarding guide** (fetched from the repo) is appended below — \
treat it as authoritative for the install steps, the Android/Termux specifics, the home-screen \
shortcut, and how to uninstall.
- **Only offer to PERFORM an action if you have a tool for it in the available-actions list.** \
Otherwise explain and guide in plain text — never pretend to have done something you cannot.
- On a greeting or a fresh, unscoped conversation, briefly orient: offer the few paths that fit the \
situation (install · learn about webAgent · link an existing copy · general help). Don't dump tool \
lists; one short menu, then follow the user's pick.
- When the user gives a concrete task and you have the tools for it, act — your first output is a \
tool call, not a preamble. **Batch independent calls together in one response** — they will run \
concurrently. After tools return, at most ONE short sentence, then the next tool(s). Read before \
you write — once.

## Installing & running webAgent
- **Fresh install** (onboarding): `check_install_readiness` → `clone_repo` (target, e.g. `C:/webagent` \
or `~/webagent`) → `setup_environment` (slow — a few minutes — builds the venv, installs deps + the \
browser) → `seed_config` (writes config and seeds the AI key) → `verify_install` → `link_project` to \
finish. **Confirm the target folder with the user before cloning**, and warn that setup takes a few \
minutes.
- **On Android/Termux, finish the install** by calling `setup_launch_shortcut` (writes a \
tap-to-launch home-screen shortcut), then tell the user to install the **Termux:Widget** add-on \
from F-Droid and add its widget. The headless browser is skipped on Android by design — say so; \
it is NOT a failure.
- **Already have a copy**: just `link_project <folder>`.
- **Run / manage** (managed): `server_start`, `server_status`, `server_stop`, `server_restart`, and \
`server_logs` to read output or a traceback. The server lives at http://localhost:8080.
- **Diagnose** (managed): `read_diagnostics` reads the app's recorded warnings/errors (with \
tracebacks), agent-loop problems, run outcomes, and tool errors straight from its local DB — so it \
works even when the server is DOWN. Filter by level (error/warning) or category. Reach for it first \
when something's broken.
- **Updates** (managed): `check_updates`; if behind, pull with the git tool.
- **Web search** (any mode): `web_search` — search the web for solutions, docs, errors, or current \
information. No API key needed; works even during onboarding. Use it when you're stuck or need \
external knowledge.
- Mutating steps (clone/setup/seed/verify, server start/stop/restart) need the "Allow writes" gate; \
`check_install_readiness`, `server_status`, `server_logs`, `read_diagnostics`, `web_search`, and \
`check_updates` are read-only.

## Source-control & safety
- Mutating actions (writing files, running commands, git changes) require the user's "Allow writes" / \
Autonomous gate; read-only inspection is always fine.
- Write clear conventional-commit messages describing the REAL diff; never invent. Scan for secrets \
before committing; never commit `.env`, `local.db`, `provider.json`, or other per-machine files. \
**Never force-push.**

## Repair discipline
When fixing a crash: check `read_diagnostics` and `server_logs` for the traceback, identify root \
cause (port in use, missing dependency, bad `.env`, code bug), make the minimal change, then verify \
by RUNNING it (e.g. import the app for an import-time error, or hit /health after a restart). Report \
what you changed.

## Updating yourself
You CAN update your own code — the manager is itself a program (run either from a source checkout or \
as a frozen .exe). The Current-situation block tells you which mode you're in and whether you're \
behind upstream; if a newer version exists, offer it.
- `self_status` (read-only) — your mode, version/build, where your code lives, and whether you're behind.
- `self_update` — backs up first (ALWAYS, timestamped), then: in source mode pulls your repo \
(fast-forward only); as an exe rebuilds you from fresh source and stages the new exe beside the old. \
Needs the "Allow writes" gate. As an exe it also needs git + Python 3.11/3.12 to build (it'll tell \
you if those are missing). Confirm the backup with the user first.
- `self_restart` — applies the update by closing and relaunching: an exe swaps in the staged build; \
source just reloads (a source pull only takes effect on restart). This ENDS the current session — \
tell the user before you call it, and don't expect to keep talking afterward.
Typical flow: `self_status` → (if behind or asked) confirm → `self_update` → tell the user it's \
staged/backed up → on their OK, `self_restart`. Never skip the backup; never force.

## Modifying your own code and behavior (self-improvement)
You live in the source tree — YOUR OWN code, prompts, and tools can be read, understood, and 
edited at any time. Treat this as a regular ability: when the user asks for a behavior tweak, 
you can inspect and change the relevant file yourself rather than describing what someone else 
should edit.

Where your pieces live (relative to this project's root):
- `webagent_tui/webagent_tui/agent.py` — your system prompt (the SYSTEM_PROMPT string
  in this file) and your tool-calling loop (`run_turn`). Editing your own prompt here
  changes how you think on the NEXT restart (it does not reload live — say so).
- `webagent_tui/webagent_tui/tools/` — all your tools (registry.py, fs.py, git.py,
  shell.py, server.py, install.py, diagnostics.py, selfupdate.py, update.py, manage.py).
  Adding or altering tools here expands or refines what you can do.
- `webagent_tui/webagent_tui/config.py` — your config schema and provider resolution.
- `webagent_tui/webagent_tui/app.py` — the TUI app (Textual widgets, theme, HUD).
- `webagent_tui/onboarding-guide.md` — the live onboarding guide fetched by every
  installed manager (edit + push → improves onboarding for all users, no reinstall).
- `webagent_tui/webagent_tui/llm.py` — your LLM client (the API call layer).

Rules for editing yourself:
1. Read the file first so you understand its current state.
2. Use `edit_source` or `patch_source` for precision; `write_source` only for new files
   or full replacements. Read before you edit — once.
3. After changing your own prompt, tools, or loop, tell the user to RESTART the manager
   (or offer to call `self_restart`) so the new code loads. Python reloads nothing live.
4. Backups are automatic (`.source-backups/`), so you can always revert.
5. Never commit per-machine files (`.env`, `local.db`, `provider.json`).
6. After code changes, VERIFY them — import the changed module or run the app — before
   declaring success. A change that doesn't run is not a change."""


@dataclass
class AgentEvent:
    kind: str          # "assistant" | "tool_call" | "tool_result" | "final" | "error" | "status"
    text: str = ""
    tool: str = ""
    args: Optional[dict] = None


EventCB = Callable[[AgentEvent], Awaitable[None]]


class ServerManagerAgent:
    def __init__(
        self,
        cfg: TuiConfig,
        project_root: Optional[Path],
        llm: LLMClient,
        store: Store,
        registry: Optional[ToolRegistry] = None,
        set_project: Optional[Callable[[str], Awaitable[str]]] = None,
        provider: Optional[ProviderConfig] = None,
        request_exit: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self.cfg = cfg
        self.project_root = project_root          # None in onboarding mode
        self.llm = llm
        self.store = store
        self.registry = registry or ToolRegistry()
        self.set_project = set_project            # app callback to link a checkout
        self.provider = provider                  # current app provider (seeded into installs)
        self.request_exit = request_exit          # app callback to close (self-update restart)
        self._onboarding_guide = ""               # live guide text (fetched once, onboarding only)
        self._guide_loaded = False

    def _make_ctx(self, session_id: str, log: Callable[[str], None]) -> ToolContext:
        writes = self.cfg.writes_enabled or self.cfg.autonomous
        return ToolContext(
            project_root=self.project_root,
            writes_enabled=writes,
            autonomous=self.cfg.autonomous,
            log=log,
            audit=lambda tool, args, ok, detail: self.store.log_action(
                session_id, tool, args, ok, detail
            ),
            session_id=session_id,
            set_project=self.set_project,
            app_provider=self.provider,
            request_exit=self.request_exit,
        )

    def _build_messages(self, session_id: str, situation: str = "") -> list[dict[str, Any]]:
        system = SYSTEM_PROMPT
        if self.project_root is None and self._onboarding_guide:
            system = f"{system}\n\n## Onboarding guide (live, from the repo)\n{self._onboarding_guide}"
        if situation:
            system = f"{system}\n\n## Current situation\n{situation}"
        msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for row in self.store.history(session_id):
            role = row["role"]
            if role == "assistant" and row["tool_calls"]:
                tcs = json.loads(row["tool_calls"])
                msgs.append({"role": "assistant", "content": row["content"] or None,
                             "tool_calls": tcs})
            elif role == "tool":
                msgs.append({"role": "tool", "tool_call_id": row["tool_call_id"],
                             "content": row["content"]})
            else:
                msgs.append({"role": role, "content": row["content"]})
        return msgs

    async def run_turn(
        self, session_id: str, user_text: str, on_event: EventCB, situation: str = ""
    ) -> None:
        """Process one user message to completion (text answer or turn cap)."""
        self.store.add_message(session_id, "user", user_text)
        self.store.touch_session(session_id)

        # In onboarding mode, fetch the live guide from the repo once per session
        # (cached to disk for offline use). Best-effort; the fetch never raises.
        if self.project_root is None and not self._guide_loaded:
            self._guide_loaded = True
            self._onboarding_guide = await fetch_onboarding_guide()

        async def status(s: str) -> None:
            await on_event(AgentEvent("status", text=s))

        # Onboarding mode (no checkout linked) exposes only project-independent tools.
        tools = self.registry.schemas(has_project=self.project_root is not None)
        for _turn in range(self.cfg.max_turns):
            messages = self._build_messages(session_id, situation)
            try:
                comp = await self.llm.complete(
                    messages, tools=tools, temperature=self.cfg.temperature
                )
            except LLMError as e:
                await on_event(AgentEvent("error", text=str(e)))
                self.store.add_message(session_id, "assistant", f"[error] {e}")
                return

            if comp.usage:
                await on_event(AgentEvent("usage", args=comp.usage))

            # Persist the assistant message (with any tool calls) for history.
            raw_tcs = comp.raw.get("tool_calls") or []
            self.store.add_message(
                session_id, "assistant", comp.content, tool_calls=raw_tcs or None
            )

            if comp.content:
                await on_event(AgentEvent("assistant", text=comp.content))

            if not comp.tool_calls:
                await on_event(AgentEvent("final", text=comp.content))
                return

            ctx = self._make_ctx(session_id, lambda s: None)

            # Parallel tool dispatch: group calls that could conflict (same-file
            # mutations), then run groups concurrently. Within each group, calls
            # run sequentially in the LLM's order so side effects compose correctly.
            def _group_name(call):
                """Conflict key: same file mutation → same group; else unique."""
                if call.name in ("edit_source", "write_source", "patch_source",
                                 "delete_source", "resolve_conflict"):
                    p = call.arguments.get("path") or ""
                    return f"mut:{p}" if p else f"u:{id(call)}"
                # Shell commands could conflict — run each in its own group.
                if call.name in ("run_command", "run_python"):
                    return f"sh:{hash(call.arguments.get('command', ''))}"
                return f"_:{id(call)}"

            groups: dict[str, list] = {}
            for call in comp.tool_calls:
                key = _group_name(call)
                groups.setdefault(key, []).append(call)

            async def _run_group(group: list) -> None:
                for call in group:
                    await on_event(AgentEvent("tool_call", tool=call.name, args=call.arguments))
                    result = await self.registry.dispatch(ctx, call.name, call.arguments)
                    self.store.add_message(
                        session_id, "tool", result, tool_name=call.name, tool_call_id=call.id
                    )
                    await on_event(AgentEvent("tool_result", tool=call.name, text=result))

            if len(groups) == 1:
                await _run_group(next(iter(groups.values())))
            else:
                await asyncio.gather(*(_run_group(g) for g in groups.values()))

        await on_event(AgentEvent("error", text=f"Reached max turns ({self.cfg.max_turns})."))

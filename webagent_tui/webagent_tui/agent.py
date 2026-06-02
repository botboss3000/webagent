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
from .resources import load_prompt
from .tools import ToolContext, ToolRegistry

# The system prompt now lives in ``manager/prompt.md`` (human-readable, editable
# without rebuilding) and is loaded at startup via ``load_prompt()``. This module
# keeps a short fallback only — see ``resources.py`` for the resolution order.
SYSTEM_PROMPT = load_prompt()


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
        # Live link to the RUNNING web app (set by the app). The shared WebAppClient
        # plus the currently connected target session, so the webapp_* tools can
        # reuse one admin session + the live stream.
        self.webapp_client: Any = None
        self.webapp_session_id: str = ""
        self.webapp_agent_id: str = ""
        self.webapp_agent_name: str = ""

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
            webapp_client=self.webapp_client,
            webapp_session_id=self.webapp_session_id,
            webapp_agent_id=self.webapp_agent_id,
            webapp_agent_name=self.webapp_agent_name,
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

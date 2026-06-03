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

from . import attach
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
        # ── Subagent delegation (mk2 event-driven model) ───────────────────
        # Set by the app. ``subagents`` is the shared registry; ``spawn_subagent``
        # launches a Textual worker and returns a human-readable "task started"
        # line; ``drain_steer`` yields any pending steering message (user typed
        # mid-turn) so the loop can pivot. All three are None on a bare agent.
        self.subagents: Any = None                # subagents.SubagentRegistry | None
        self.spawn_subagent: Optional[Callable[..., Awaitable[str]]] = None
        self.drain_steer: Optional[Callable[[], Optional[str]]] = None

    def _make_ctx(self, session_id: str, log: Callable[[str], None],
                  spawn: Optional[Callable[..., Awaitable[str]]] = None) -> ToolContext:
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
            git_token=self.cfg.git_token,
            # Only the MAIN agent loop may spawn subagents (``spawn`` passed in);
            # subagent runs get ``spawn=None`` so they can't recurse.
            spawn_subagent=spawn,
            subagents=self.subagents,
            available_tools=self.registry.names(has_project=self.project_root is not None),
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
            elif role == "user" and row.get("content_kind") == "json":
                # A user message with image attachments — stored as
                # {"text": …, "images": [{path, mime, name}, …]} and replayed as
                # an OpenAI-compatible multimodal content list (text + image parts).
                try:
                    payload = json.loads(row["content"]) or {}
                except (json.JSONDecodeError, TypeError):
                    payload = {}
                content = attach.to_content_parts(
                    payload.get("text") or "", payload.get("images") or []
                )
                msgs.append({"role": "user", "content": content})
            else:
                msgs.append({"role": role, "content": row["content"]})
        return msgs

    # ── external-event folding (subagent results + user steering) ──────────
    def _absorb_external(self, session_id: str) -> bool:
        """Fold anything that arrived since the last LLM call into history.

        Two sources: (1) subagent results that have **surfaced** (a notify result,
        or a fully-gathered group) become one synthetic user message; (2) any
        **steering** messages the user typed mid-turn are appended verbatim. Both
        are written as user-role messages so the next LLM call sees them as having
        "arrived." Returns True if anything was injected.
        """
        added = False
        if self.subagents is not None:
            drained = self.subagents.drain_surfaced()
            if drained:
                from .subagents import format_delivery
                self.store.add_message(session_id, "user", format_delivery(drained))
                added = True
        if self.drain_steer is not None:
            while True:
                steer = self.drain_steer()
                if not steer:
                    break
                self.store.add_message(session_id, "user", f"[steering] {steer}")
                added = True
        return added

    def _has_surfaced_results(self) -> bool:
        return self.subagents is not None and self.subagents.has_surfaced_pending()

    # ── the main, re-entrant turn ──────────────────────────────────────────
    async def run_turn(
        self, session_id: str, user_text: str, on_event: EventCB, situation: str = "",
        images: Optional[list[dict[str, Any]]] = None, synthetic: bool = False,
    ) -> None:
        """Run one MAIN-agent turn — now event-driven and re-entrant.

        A turn is kicked off by one of three events: a real **user message** (the
        normal path, possibly with ``images``); a delivered batch of **subagent
        results** or a **watchdog alert** (``synthetic=True`` — ``user_text`` is
        already a ready-made synthetic message, stored verbatim, no image handling).

        Whatever the trigger, at the top of every loop iteration the agent folds in
        any newly-surfaced subagent results and any mid-turn steering message
        (:meth:`_absorb_external`). If the model answers with text but more results
        landed *during* the turn, the loop keeps going rather than ending — so a
        result that arrives mid-turn is never stranded ("auto-continue"). Steering
        injected this way takes effect at the next LLM call, so the agent pivots
        after at most one tool batch (it always lets in-flight tool calls finish so
        every requested call still gets a result).
        """
        # 1. Record the triggering message.
        if synthetic:
            if user_text:
                self.store.add_message(session_id, "user", user_text)
        elif images:
            payload = json.dumps({
                "text": user_text,
                "images": [{"path": i["path"], "mime": i["mime"], "name": i.get("name", "")}
                           for i in images],
            })
            self.store.add_message(session_id, "user", payload, content_kind="json")
        else:
            self.store.add_message(session_id, "user", user_text)
        self.store.touch_session(session_id)

        # In onboarding mode, fetch the live guide from the repo once per session
        # (cached to disk for offline use). Best-effort; the fetch never raises.
        if self.project_root is None and not self._guide_loaded:
            self._guide_loaded = True
            self._onboarding_guide = await fetch_onboarding_guide()

        # Onboarding mode (no checkout linked) exposes only project-independent tools.
        tools = self.registry.schemas(has_project=self.project_root is not None)
        for _turn in range(self.cfg.max_turns):
            # Fold in subagent results + steering before deciding what to do next.
            self._absorb_external(session_id)

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
                # The model is ready to go idle. But if a subagent result surfaced
                # while we were thinking, fold it in and keep going instead of
                # ending the turn — that's the "auto-continue" / gather moment.
                if self._has_surfaced_results():
                    continue
                await on_event(AgentEvent("final", text=comp.content))
                return

            # The MAIN loop may spawn subagents (spawn passed through to the ctx).
            ctx = self._make_ctx(session_id, lambda s: None, spawn=self.spawn_subagent)

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

    # ── a subagent's own bounded loop ──────────────────────────────────────
    async def run_subagent(
        self, session_id: str, task: str, allowed_tools: list[str],
        on_event: Optional[EventCB] = None, situation: str = "",
    ) -> tuple[str, str]:
        """Run a delegated subagent to completion in its OWN session.

        The subagent is **caller-scoped**: it sees and may call only the tool names
        in ``allowed_tools`` — nothing else, and never the delegation tools (so it
        can't recurse). It runs the same bounded loop as the main agent but folds
        in no external events, has no steering, and cannot spawn further subagents
        (``spawn=None``). Returns ``(status, summary)`` where status is
        ``"completed"`` or ``"error"`` and summary is the final text it produced —
        which the registry surfaces back to the main agent.
        """
        self.store.add_message(session_id, "user", task)
        self.store.touch_session(session_id)

        allowed = set(allowed_tools or [])
        all_schemas = self.registry.schemas(has_project=self.project_root is not None)
        tools = [s for s in all_schemas if s["function"]["name"] in allowed]
        if not tools:
            return ("error", "No valid tools were granted to this subagent.")

        final_text = ""
        for _turn in range(self.cfg.max_turns):
            messages = self._build_messages(session_id, situation)
            try:
                comp = await self.llm.complete(
                    messages, tools=tools, temperature=self.cfg.temperature
                )
            except LLMError as e:
                self.store.add_message(session_id, "assistant", f"[error] {e}")
                return ("error", f"LLM error: {e}")

            if comp.usage and on_event is not None:
                await on_event(AgentEvent("usage", args=comp.usage))

            raw_tcs = comp.raw.get("tool_calls") or []
            self.store.add_message(
                session_id, "assistant", comp.content, tool_calls=raw_tcs or None
            )
            if comp.content:
                final_text = comp.content
                if on_event is not None:
                    await on_event(AgentEvent("assistant", text=comp.content))

            if not comp.tool_calls:
                return ("completed", final_text or "(subagent finished with no text output)")

            # Subagents never spawn further subagents (spawn=None).
            ctx = self._make_ctx(session_id, lambda s: None, spawn=None)
            for call in comp.tool_calls:
                if call.name not in allowed:
                    result = (f"Error: tool '{call.name}' is not permitted for this "
                              "subagent (it was not in the granted tool list).")
                else:
                    if on_event is not None:
                        await on_event(AgentEvent("tool_call", tool=call.name, args=call.arguments))
                    result = await self.registry.dispatch(ctx, call.name, call.arguments)
                self.store.add_message(
                    session_id, "tool", result, tool_name=call.name, tool_call_id=call.id
                )
                if on_event is not None:
                    await on_event(AgentEvent("tool_result", tool=call.name, text=result))

        return ("completed", final_text or f"(subagent hit the turn cap of {self.cfg.max_turns})")

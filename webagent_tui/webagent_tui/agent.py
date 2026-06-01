"""The serverless server manager agent loop.

A bounded tool-calling loop that runs entirely in-process: build messages →
ask the LLM (with the Codebase Admin + Source Control tool schemas) → dispatch
any tool calls → feed results back → repeat until the model answers with text or
the turn cap is hit. Every step is streamed to the UI via ``on_event`` and
persisted to the external store.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .config import TuiConfig
from .db import Store
from .llm import LLMClient, LLMError
from .tools import ToolContext, ToolRegistry

SYSTEM_PROMPT = """You are **webAgent Server Manager** — a privileged, server-independent agent that \
installs, diagnoses, repairs, and manages a webAgent checkout. You talk directly \
to the LLM API, so you keep working even when the webAgent server is down.

## Abilities
- **Codebase Admin**: read_source, write_source, edit_source, patch_source, \
delete_source, search_source, read_directory, run_command, run_python.
- **Source Control**: git_tool (status/diff/commit/push/pull/…), resolve_conflict.

## Working style (controls how many turns a task takes)
1. **Only call a tool when the user gives a task.** For chat/greetings/planning, \
reply in plain text — no tool call.
2. **When given a task, your first output is a tool call**, not a preamble. No \
"Let me…", no plans for approval — the user's instruction is the authorization.
3. **Batch independent read-only calls** (status + diff, or several reads) in one turn.
4. After a tool returns, at most ONE short sentence, then the next tool.
5. **Read before you write — once.** Use offset/limit on big files. After a patch, \
the returned diff is enough; don't re-read to verify.

## Source-control safety
- Write a clear conventional-commit message describing the REAL diff; never invent.
- Scan the diff for secrets before committing; do NOT commit `.env`, `local.db`, or \
other per-machine runtime files. **Never force-push.**
- Verify a fix by RUNNING it (run_command / run_python), not by re-reading.

## Repair discipline
When fixing a crash: read the traceback, identify root cause (port in use, missing \
dependency, bad `.env`, code bug), make the minimal change, then verify (e.g. \
`python -c "import app.main"` for an import-time error). Report what you changed."""


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
        project_root: Path,
        llm: LLMClient,
        store: Store,
        registry: Optional[ToolRegistry] = None,
    ) -> None:
        self.cfg = cfg
        self.project_root = project_root
        self.llm = llm
        self.store = store
        self.registry = registry or ToolRegistry()

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
        )

    def _build_messages(self, session_id: str) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
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

    async def run_turn(self, session_id: str, user_text: str, on_event: EventCB) -> None:
        """Process one user message to completion (text answer or turn cap)."""
        self.store.add_message(session_id, "user", user_text)
        self.store.touch_session(session_id)

        async def status(s: str) -> None:
            await on_event(AgentEvent("status", text=s))

        tools = self.registry.schemas()
        for _turn in range(self.cfg.max_turns):
            messages = self._build_messages(session_id)
            try:
                comp = await self.llm.complete(
                    messages, tools=tools, temperature=self.cfg.temperature
                )
            except LLMError as e:
                await on_event(AgentEvent("error", text=str(e)))
                self.store.add_message(session_id, "assistant", f"[error] {e}")
                return

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
            for call in comp.tool_calls:
                await on_event(AgentEvent("tool_call", tool=call.name, args=call.arguments))
                result = await self.registry.dispatch(ctx, call.name, call.arguments)
                self.store.add_message(
                    session_id, "tool", result, tool_name=call.name, tool_call_id=call.id
                )
                await on_event(AgentEvent("tool_result", tool=call.name, text=result))

        await on_event(AgentEvent("error", text=f"Reached max turns ({self.cfg.max_turns})."))

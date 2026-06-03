"""Tool context + spec shared by every server manager tool."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

if TYPE_CHECKING:
    from ..config import ProviderConfig
    from ..subagents import SubagentRegistry


@dataclass
class ToolContext:
    """Everything a tool handler needs, injected by the agent on each call.

    ``project_root`` is ``None`` in onboarding mode (no webAgent repo linked yet);
    only tools flagged ``needs_project=False`` are exposed/dispatched then.
    """

    project_root: Optional[Path]
    writes_enabled: bool                 # mutating tools allowed? (armed OR autonomous)
    autonomous: bool                     # acting without per-call confirmation
    log: Callable[[str], None]           # stream a status line to the UI
    audit: Callable[[str, Any, bool, str], None]  # (tool, args, ok, detail)
    session_id: str = ""
    # Link the manager to a webAgent checkout (provided by the app). Returns a
    # human-readable result. Used by onboarding-mode tools that change app state.
    set_project: Optional[Callable[[str], Awaitable[str]]] = None
    # The manager's current ("app") provider — seeded into a fresh install so the
    # new copy has a working AI key from the start.
    app_provider: Optional["ProviderConfig"] = None
    # Ask the app to close the manager (provided by the app). Used by the
    # self-update restart so a staged exe swap / source reload can finish.
    request_exit: Optional[Callable[[], Awaitable[None]]] = None
    # The shared client for the RUNNING webAgent app (login + WS stream + send).
    # Set by the app so app-facing tools (appctl / webapp) reuse one session +
    # the live stream. None in plain tool calls → a standalone client is used.
    webapp_client: Optional[Any] = None
    # The session_id of the currently connected web-app target (set when the
    # WebAgent is unmuted), so webapp_send knows where to post. "" when none.
    webapp_session_id: str = ""
    webapp_agent_id: str = ""
    webapp_agent_name: str = ""
    # GitHub token (set in the Git panel) used to authenticate network git ops
    # (fetch/pull/push) against github.com. Empty → rely on the host's own git creds.
    git_token: str = ""
    # ── Subagent delegation (mk2 event-driven model) ───────────────────────
    # Launch a subagent and return immediately with a human-readable result that
    # carries the task id. Provided by the app (it creates the sub-session, runs a
    # Textual worker, and reports completion back through the registry). None in
    # plain/non-app contexts → delegation tools report they're unavailable.
    #   spawn_subagent(task, tools, mode, group_id) -> str
    spawn_subagent: Optional[Callable[..., Awaitable[str]]] = None
    # The live subagent registry, so check_task can read a task's status/summary
    # (including background "pump-and-dump" results that never auto-surface).
    subagents: Optional["SubagentRegistry"] = None
    # The full set of tool names that exist, so delegate_* can validate the
    # caller-scoped ``tools`` list and reject unknown names early.
    available_tools: list[str] = field(default_factory=list)


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict                     # JSON schema (OpenAI function params)
    handler: Callable[..., Awaitable[str]]
    mutating: bool = False
    needs_project: bool = True           # requires a linked checkout (hidden in onboarding)

    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


WRITES_DISABLED_MSG = (
    "Refused: writes are disabled. Enable the 'Allow writes' toggle (or turn on "
    "Autonomous mode) before using mutating tools."
)

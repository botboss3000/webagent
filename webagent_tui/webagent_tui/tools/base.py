"""Tool context + spec shared by every server manager tool."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

if TYPE_CHECKING:
    from ..config import ProviderConfig


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

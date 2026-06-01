"""Tool context + spec shared by every operator tool."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional


@dataclass
class ToolContext:
    """Everything a tool handler needs, injected by the agent on each call."""

    project_root: Path
    writes_enabled: bool                 # mutating tools allowed? (armed OR autonomous)
    autonomous: bool                     # acting without per-call confirmation
    log: Callable[[str], None]           # stream a status line to the UI
    audit: Callable[[str, Any, bool, str], None]  # (tool, args, ok, detail)
    session_id: str = ""


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict                     # JSON schema (OpenAI function params)
    handler: Callable[..., Awaitable[str]]
    mutating: bool = False

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

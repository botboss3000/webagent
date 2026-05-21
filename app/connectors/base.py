"""
Base Connector ABC for per-agent external data sources.

Each connector implements a small set of methods so the tool loader, the
data-source admin API, and the agent loop can treat all connector types
uniformly.

A `data_source` dict (the row + parsed JSON columns) is passed into every
method. An `attachment` dict (joined agent_data_sources row) is passed where
agent-scoped behavior matters (tool naming, prompt snippet injection).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class GeneratedTool:
    """A synthetic tool produced by a connector at agent-load time.

    Mirrors the shape of `app.tools.loader.ToolInfo` but stays connector-side
    so the loader stays decoupled. The loader translates GeneratedTool ->
    ToolInfo when merging into the agent's tool dict.
    """
    name: str                                  # tool name as exposed to the LLM
    description: str                           # short description (also feeds prompt)
    parameters: dict                           # JSON Schema for tool params
    handler: Callable[..., Any]                # async callable
    destructive: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


class Connector(ABC):
    """Per-type connector for a `data_sources` row."""

    #: Canonical type string. Must match a key in CONNECTOR_REGISTRY and the
    #: CHECK constraint on data_sources.type.
    type_name: str = ""

    #: Human-readable label shown in the UI dropdown.
    display_name: str = ""

    #: Whether this connector requires an auth_elements row (credential).
    requires_credential: bool = False

    @abstractmethod
    async def test_connection(
        self, data_source: dict, auth_resolver: Callable[[str], Optional[dict]]
    ) -> Dict[str, Any]:
        """Probe the source and confirm it is reachable.

        Returns: {"ok": bool, "message": str, "details": dict}
        """
        ...

    @abstractmethod
    async def introspect(
        self, data_source: dict, auth_resolver: Callable[[str], Optional[dict]]
    ) -> Dict[str, Any]:
        """Fetch a schema/index summary for the source.

        For SQL: list of tables + columns.
        For doc_store: list of files + chunk count.
        For web_search_domain: just the domain echoed back.
        Returned dict is stored verbatim in data_sources.schema_cache.
        """
        ...

    @abstractmethod
    def generated_tools(
        self,
        data_source: dict,
        attachment: dict,
        auth_resolver: Callable[[str], Optional[dict]],
    ) -> List[GeneratedTool]:
        """Return the synthetic tools this attachment grants the agent."""
        ...

    @abstractmethod
    def prompt_snippet(self, data_source: dict, attachment: dict) -> str:
        """Short description for system-prompt injection (when enabled)."""
        ...

    # ---- helpers shared across connectors ----

    @staticmethod
    def _safe_tool_name(raw: str) -> str:
        out = []
        for ch in raw.strip().lower():
            if ch.isalnum():
                out.append(ch)
            elif ch in (" ", "-", "_", "."):
                out.append("_")
        slug = "".join(out).strip("_") or "data_source"
        if slug[0].isdigit():
            slug = "ds_" + slug
        return slug[:48]

    def default_tool_name(self, data_source: dict, attachment: dict) -> str:
        alias = (attachment or {}).get("tool_alias") or ""
        if alias.strip():
            return self._safe_tool_name(alias)
        return self._safe_tool_name(f"{self.type_name}_{data_source.get('name','source')}")

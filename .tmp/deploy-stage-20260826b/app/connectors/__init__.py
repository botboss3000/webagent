"""
Per-agent external data source connectors.

Each connector type lives in its own module and implements the `Connector`
ABC from `app.connectors.base`. The registry below maps the canonical
`data_sources.type` value to the connector class.

To add a new connector type, just drop a file:
  Create app/connectors/<name>.py with a class subclassing `Connector` and a
  `type_name` class attribute. It is auto-discovered and registered below — no
  edit here, and (since the data_sources.type CHECK was relaxed) no schema edit.
"""

from typing import Dict, Type

from app.connectors.base import Connector, GeneratedTool
from app.connectors.sql_postgres import SqlPostgresConnector
from app.connectors.doc_store import DocStoreConnector
from app.connectors.web_search_domain import WebSearchDomainConnector


# Built-ins listed explicitly (zero-risk), then any drop-in connector file is
# merged in by folder-scan discovery (keyed by its `type_name`).
CONNECTOR_REGISTRY: Dict[str, Type[Connector]] = {
    "sql_postgres": SqlPostgresConnector,
    "doc_store": DocStoreConnector,
    "web_search_domain": WebSearchDomainConnector,
}

try:
    from app.features.registry import discover_backends
    for _tid, _cls in discover_backends(
        "app.connectors", Connector, id_attr="type_name", skip={"base"}
    ).items():
        CONNECTOR_REGISTRY.setdefault(_tid, _cls)
except Exception:  # discovery is a bonus; never break the built-ins
    pass


def get_connector(type_name: str) -> Connector:
    """Return a connector instance for the given type. Raises KeyError if unknown."""
    cls = CONNECTOR_REGISTRY.get(type_name)
    if cls is None:
        raise KeyError(f"Unknown connector type: {type_name}")
    return cls()


__all__ = [
    "Connector",
    "GeneratedTool",
    "CONNECTOR_REGISTRY",
    "get_connector",
]

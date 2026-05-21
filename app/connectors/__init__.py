"""
Per-agent external data source connectors.

Each connector type lives in its own module and implements the `Connector`
ABC from `app.connectors.base`. The registry below maps the canonical
`data_sources.type` value to the connector class.

To add a new connector type:
  1. Create app/connectors/<name>.py with a class subclassing `Connector`.
  2. Import and register it below.
  3. Add the type string to the CHECK constraint in
     app/db/schema/tables.py + app/db/local.py (SCHEMA_SQL).
"""

from typing import Dict, Type

from app.connectors.base import Connector, GeneratedTool
from app.connectors.sql_postgres import SqlPostgresConnector
from app.connectors.doc_store import DocStoreConnector
from app.connectors.web_search_domain import WebSearchDomainConnector


CONNECTOR_REGISTRY: Dict[str, Type[Connector]] = {
    "sql_postgres": SqlPostgresConnector,
    "doc_store": DocStoreConnector,
    "web_search_domain": WebSearchDomainConnector,
}


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

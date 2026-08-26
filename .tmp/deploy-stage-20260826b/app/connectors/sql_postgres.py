"""
Read-only Postgres connector.

Phase-1 safety model:
  * Statement type allowlist (default SELECT only) parsed via sqlglot if
    available, else a coarse string check.
  * Optional `allowed_tables` allowlist in safety_policy — query referenced
    tables must be a subset.
  * Row limit auto-injected if absent.
  * Connection string assembled from `config` + auth_element secret_ref
    (which holds the password). We never log the password.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional

from app.connectors.base import Connector, GeneratedTool

FEATURE = {
    "id": "sql_postgres",
    "display_name": "SQL (Postgres/MySQL)",
    "category": "connector",
    "status": "beta",
    "summary": "Read-only live queries against an external SQL database with a statement allowlist.",
    "requires": ["external DB connection details"],
}

logger = logging.getLogger(__name__)


def _resolve_password(auth_element_id: Optional[str], auth_resolver: Callable[[str], Optional[dict]]) -> str:
    if not auth_element_id:
        return ""
    auth = auth_resolver(auth_element_id) if auth_resolver else None
    if not auth:
        return ""
    # secret_ref convention: literal value for now; vault lookup later
    secret_ref = auth.get("secret_ref") or ""
    if secret_ref.startswith("env:"):
        import os
        return os.environ.get(secret_ref[4:], "")
    return secret_ref


def _parse_statement_kind(sql: str) -> str:
    s = sql.lstrip()
    # Strip leading comments
    while s.startswith("--") or s.startswith("/*"):
        if s.startswith("--"):
            idx = s.find("\n")
            s = s[idx + 1:] if idx >= 0 else ""
        else:
            idx = s.find("*/")
            s = s[idx + 2:] if idx >= 0 else ""
        s = s.lstrip()
    m = re.match(r"([A-Za-z]+)", s)
    return m.group(1).upper() if m else ""


def _enforce_row_limit(sql: str, row_limit: int) -> str:
    if row_limit <= 0:
        return sql
    if re.search(r"\blimit\b", sql, flags=re.IGNORECASE):
        return sql
    return sql.rstrip().rstrip(";") + f" LIMIT {int(row_limit)}"


def _extract_referenced_tables(sql: str) -> List[str]:
    try:
        import sqlglot
        try:
            tree = sqlglot.parse_one(sql, read="postgres")
        except Exception:
            return []
        tables = set()
        for tbl in tree.find_all(sqlglot.expressions.Table):
            tables.add(tbl.name)
        return [t for t in tables if t]
    except ImportError:
        return []


class SqlPostgresConnector(Connector):
    type_name = "sql_postgres"
    display_name = "Postgres (read-only)"
    requires_credential = True

    async def test_connection(self, data_source, auth_resolver):
        cfg = data_source.get("config") or {}
        try:
            conn = await self._open(cfg, data_source.get("auth_element_id"), auth_resolver)
        except Exception as e:
            return {"ok": False, "message": f"connection failed: {e}", "details": {}}
        try:
            ver = await conn.fetchval("SELECT version()")
            return {"ok": True, "message": "connected", "details": {"version": ver}}
        finally:
            await conn.close()

    async def introspect(self, data_source, auth_resolver):
        cfg = data_source.get("config") or {}
        schema = cfg.get("schema") or "public"
        conn = await self._open(cfg, data_source.get("auth_element_id"), auth_resolver)
        try:
            rows = await conn.fetch(
                """SELECT table_name, column_name, data_type
                   FROM information_schema.columns
                   WHERE table_schema = $1
                   ORDER BY table_name, ordinal_position""",
                schema,
            )
            tables: Dict[str, List[dict]] = {}
            for r in rows:
                tables.setdefault(r["table_name"], []).append(
                    {"name": r["column_name"], "type": r["data_type"]}
                )
            return {
                "schema": schema,
                "tables": [
                    {"name": name, "columns": cols} for name, cols in sorted(tables.items())
                ],
            }
        finally:
            await conn.close()

    def generated_tools(self, data_source, attachment, auth_resolver):
        tool_name = self.default_tool_name(data_source, attachment) or "query_sql"
        if not tool_name.startswith(("query_", "sql_")):
            tool_name = f"query_{tool_name}"

        safety = data_source.get("safety_policy") or {}
        allowed_statements = [s.upper() for s in (safety.get("allowed_statements") or ["SELECT"])]
        allowed_tables = set(safety.get("allowed_tables") or [])
        row_limit = int(safety.get("row_limit") or 1000)

        cfg = data_source.get("config") or {}
        auth_element_id = data_source.get("auth_element_id")

        async def _handler(query: str, params: Optional[List[Any]] = None):
            kind = _parse_statement_kind(query)
            if kind not in allowed_statements:
                return json.dumps({
                    "status": "error",
                    "message": f"statement type {kind!r} not in allowlist {allowed_statements}",
                })
            if allowed_tables:
                refs = _extract_referenced_tables(query)
                if refs:
                    bad = [t for t in refs if t not in allowed_tables]
                    if bad:
                        return json.dumps({
                            "status": "error",
                            "message": f"tables not in allowlist: {bad}",
                        })
            final_sql = _enforce_row_limit(query, row_limit)
            try:
                conn = await self._open(cfg, auth_element_id, auth_resolver)
            except Exception as e:
                return json.dumps({"status": "error", "message": f"connection failed: {e}"})
            try:
                rows = await conn.fetch(final_sql, *(params or []))
                out_rows = [dict(r) for r in rows]
                return json.dumps({
                    "status": "ok",
                    "row_count": len(out_rows),
                    "rows": out_rows[:row_limit],
                }, default=str)
            except Exception as e:
                return json.dumps({"status": "error", "message": str(e)})
            finally:
                await conn.close()

        return [
            GeneratedTool(
                name=tool_name,
                description=(
                    f"Run a parameterized read-only SQL query against the "
                    f"`{data_source.get('name')}` Postgres data source. "
                    f"Allowed statements: {', '.join(allowed_statements)}. "
                    f"Use $1, $2, ... for parameters."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "SQL with $1, $2 placeholders"},
                        "params": {
                            "type": "array",
                            "items": {},
                            "description": "Values for $1, $2, ... in order",
                        },
                    },
                    "required": ["query"],
                },
                handler=_handler,
                destructive="SELECT" not in allowed_statements or any(
                    s not in ("SELECT",) for s in allowed_statements
                ),
                metadata={"connector": self.type_name, "data_source_id": data_source.get("id")},
            )
        ]

    def prompt_snippet(self, data_source, attachment):
        name = data_source.get("name", "source")
        schema = (data_source.get("schema_cache") or {})
        tables = schema.get("tables") or []
        if not tables:
            return f"Postgres data source `{name}` is attached; run `introspect` to discover tables."
        lines = [f"Postgres data source `{name}` (schema `{schema.get('schema','public')}`):"]
        for t in tables[:20]:
            cols = ", ".join(f"{c['name']}:{c['type']}" for c in (t.get('columns') or [])[:12])
            lines.append(f"  - {t['name']}({cols})")
        if len(tables) > 20:
            lines.append(f"  … and {len(tables) - 20} more tables")
        return "\n".join(lines)

    # ---- internals ----

    @staticmethod
    async def _open(cfg: dict, auth_element_id: Optional[str], auth_resolver):
        import asyncpg
        password = _resolve_password(auth_element_id, auth_resolver)
        kwargs = {
            "host": cfg.get("host") or "localhost",
            "port": int(cfg.get("port") or 5432),
            "database": cfg.get("database") or cfg.get("db") or "postgres",
            "user": cfg.get("user") or cfg.get("username") or "postgres",
            "password": password,
            "timeout": float(cfg.get("connect_timeout") or 8.0),
        }
        if cfg.get("ssl") in (True, "true", "require"):
            kwargs["ssl"] = "require"
        return await asyncpg.connect(**kwargs)

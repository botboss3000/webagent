"""
Raw Postgres backend (asyncpg).

CURRENT SCOPE: connection testing + schema bootstrap only. Used by the
Storage UI's "Test Connection" and "Auto-Create Tables" buttons so that an
admin can validate creds + create the schema on a remote Postgres BEFORE
attempting to activate it as the live backend.

NOT YET IMPLEMENTED: the full StorageBackend interface (60+ methods). When
the admin clicks "Activate" the API will refuse with a clear message until
this backend is finished. The data-method port follows the same pattern as
the existing SupabaseBackend, just translated from REST-builder calls to
asyncpg `execute()` / `fetch()`.

Supported providers via this backend: postgres, gcp_cloud_sql, neon.
(Supabase keeps using SupabaseBackend until the port lands.)
"""

import logging
from typing import Optional

from app.db.connection_config import DBConnectionConfig

logger = logging.getLogger(__name__)


class PostgresConnectionError(Exception):
    pass


async def test_connection(cfg: DBConnectionConfig, password: Optional[str]) -> dict:
    """
    Try to connect with the given config + password. Returns
        {"ok": True, "server_version": "...", "database": "..."}
    or
        {"ok": False, "error": "..."}
    """
    try:
        import asyncpg
    except ImportError:
        return {"ok": False, "error": "asyncpg not installed. Add `asyncpg` to requirements.txt."}

    url = cfg.build_url(password=password or "")
    # asyncpg consumes the raw DSN (strip the +asyncpg dialect tag for asyncpg.connect)
    dsn = url.replace("postgresql+asyncpg://", "postgresql://")

    try:
        conn = await asyncpg.connect(dsn, timeout=10)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    try:
        version = await conn.fetchval("SELECT version()")
        dbname = await conn.fetchval("SELECT current_database()")
        return {"ok": True, "server_version": version, "database": dbname}
    finally:
        await conn.close()


async def bootstrap_schema(cfg: DBConnectionConfig, password: Optional[str]) -> dict:
    """
    Connect and run the rendered Postgres DDL.

    Idempotent (uses CREATE TABLE IF NOT EXISTS). Returns
        {"ok": True, "statements_run": N}
    or
        {"ok": False, "error": "..."}
    """
    try:
        import asyncpg
    except ImportError:
        return {"ok": False, "error": "asyncpg not installed."}

    from app.db.schema import render_postgres
    ddl = render_postgres()

    url = cfg.build_url(password=password or "")
    dsn = url.replace("postgresql+asyncpg://", "postgresql://")

    try:
        conn = await asyncpg.connect(dsn, timeout=15)
    except Exception as e:
        return {"ok": False, "error": f"connect failed: {e}"}

    statements_run = 0
    try:
        # Split on ';' at the top level. The DDL renderer produces simple
        # statements separated by ';' (no PL/pgSQL bodies), so this is safe.
        # FTS / trigger statements use multi-statement bodies in SQLite only;
        # the postgres renderer emits single CREATE INDEX expressions.
        for stmt in _split_sql(ddl):
            s = stmt.strip()
            if not s or s.startswith("--"):
                continue
            try:
                await conn.execute(s)
                statements_run += 1
            except Exception as e:
                logger.warning("Bootstrap statement failed: %s :: %s", e, s[:120])
                # continue — IF NOT EXISTS makes most failures recoverable
        return {"ok": True, "statements_run": statements_run}
    finally:
        await conn.close()


def _split_sql(text: str) -> list:
    """Naive ';' split. Renderer output has no embedded ';' inside literals."""
    out = []
    buf = []
    for ch in text:
        buf.append(ch)
        if ch == ";":
            out.append("".join(buf))
            buf = []
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out

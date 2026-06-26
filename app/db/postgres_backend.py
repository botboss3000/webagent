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

import asyncio
import logging
import os
import shutil
import tempfile
from typing import Optional

from app.db.connection_config import DBConnectionConfig

logger = logging.getLogger(__name__)


class PostgresConnectionError(Exception):
    pass


# ── Connection-string + DDL helpers (shared by the live backend) ─────────────

def make_conninfo(cfg: DBConnectionConfig, password: str) -> str:
    """Build a libpq conninfo string from a DBConnectionConfig + password."""
    import psycopg
    meta_port = cfg.port or 5432
    return psycopg.conninfo.make_conninfo(
        host=cfg.host or "localhost",
        port=meta_port,
        dbname=cfg.database or "postgres",
        user=cfg.username or "postgres",
        password=password or "",
        sslmode=cfg.ssl_mode or "prefer",
    )


def _strip_comment_lines(stmt: str) -> str:
    return "\n".join(
        ln for ln in stmt.splitlines() if not ln.strip().startswith("--")
    ).strip()


def _sqlite_type_to_pg(decl: str) -> str:
    d = (decl or "").upper()
    if "INT" in d:
        return "INTEGER"
    if any(x in d for x in ("REAL", "FLOA", "DOUB")):
        return "DOUBLE PRECISION"
    if "BLOB" in d:
        return "BYTEA"
    return "TEXT"


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
    errors: list = []
    try:
        # Ensure pgvector is available before any table that uses a vector column.
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception as e:
            logger.warning("Could not create pgvector extension: %s", e)
            errors.append({"stmt": "CREATE EXTENSION vector", "error": str(e)})

        # Statements are split + comment-stripped by _iter_ddl_statements. The
        # DDL renderer produces simple statements separated by ';' (no PL/pgSQL
        # bodies) in dependency order, so a single pass works.
        for s in _iter_ddl_statements(ddl):
            try:
                await conn.execute(s)
                statements_run += 1
            except Exception as e:
                logger.warning("Bootstrap statement failed: %s :: %s", e, s[:120])
                errors.append({"stmt": s[:160], "error": str(e)})
                # continue — IF NOT EXISTS makes most failures recoverable
        return {"ok": len(errors) == 0, "statements_run": statements_run, "errors": errors}
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


def _iter_ddl_statements(ddl: str):
    """Yield each non-empty, comment-stripped statement from rendered DDL.

    Shared by both bootstrap paths (async asyncpg `bootstrap_schema` and sync
    psycopg `_bootstrap_pg_schema`): split on ';', drop full-line `--` comments
    (so a leading banner comment glued to the first CREATE doesn't swallow it),
    and skip anything left blank. Each caller executes the statements itself.
    """
    for raw in _split_sql(ddl):
        stmt = _strip_comment_lines(raw)
        if stmt:
            yield stmt


# ── Live Postgres backend ────────────────────────────────────────────────────

class PostgresBackend:  # placeholder replaced below by the real subclass
    pass


def _reference_sqlite_columns() -> dict:
    """
    Build {table: {column: declared_type}} from a throwaway, no-seed SQLite
    backend. This reflects the CURRENT code's full schema (base + all ALTER
    migrations), so Postgres can be reconciled to exact column parity without
    hand-maintaining a duplicate column list.
    """
    from app.db.local import LocalBackend
    tmpdir = tempfile.mkdtemp(prefix="wa_refschema_")
    out: dict = {}
    try:
        ref = LocalBackend(db_path=os.path.join(tmpdir, "ref.db"), seed=False)
        conn = ref._get_conn()
        try:
            tables = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            ]
            for t in tables:
                info = conn.execute(f"PRAGMA table_info({t})").fetchall()
                out[t] = {row[1]: row[2] for row in info}  # name -> declared type
        finally:
            conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return out


def _make_postgres_backend_class():
    """Build the PostgresBackend subclass lazily (so importing this module does
    not require LocalBackend / psycopg unless a PG backend is actually used)."""
    from app.db.local import LocalBackend
    from app.db.schema import render_postgres

    class _PostgresBackend(LocalBackend):
        """
        Live Postgres backend. Subclasses LocalBackend and overrides only the
        connection + schema bootstrap; every data method is inherited and runs
        through the PgPortableConnection translation layer (see pg_portable.py).
        """

        def __init__(self, cfg: DBConnectionConfig, password: Optional[str] = None, seed: bool = True):
            self._cfg = cfg
            self._password = password if password is not None else os.environ.get("WEBAGENT_DB_PASSWORD", "")
            self._conninfo = make_conninfo(cfg, self._password)
            self._db_path = "<postgres>"  # sentinel; never used to open SQLite
            self._write_lock = asyncio.Lock()
            self._seed_on_init = seed
            self._scan_sibling_dbs = False
            self._pool = None
            # The `vector` extension must exist BEFORE the pool opens, otherwise
            # register_vector() fails on the pool's initial connections and numpy
            # arrays cannot be adapted to the vector type.
            self._ensure_extension()
            self._open_pool()
            self._init_db()

        # ---- connection / pool ----

        def _ensure_extension(self):
            import psycopg
            try:
                with psycopg.connect(self._conninfo, autocommit=True) as conn:
                    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            except Exception as e:
                logger.warning("Could not ensure pgvector extension: %s", e)

        def _open_pool(self):
            from psycopg_pool import ConnectionPool

            def _configure(conn):
                try:
                    from pgvector.psycopg import register_vector
                    register_vector(conn)
                    conn.rollback()  # clear the txn opened while reading the type OID
                except Exception as e:  # pragma: no cover
                    logger.warning("pgvector register failed on pooled conn: %s", e)

            self._pool = ConnectionPool(
                self._conninfo,
                min_size=2,
                max_size=16,
                open=True,
                configure=_configure,
                kwargs={"autocommit": False},
            )

        def _get_conn(self):
            from app.db.pg_portable import PgPortableConnection
            return PgPortableConnection(self._pool)

        # ---- schema bootstrap (overrides the SQLite _init_db entirely) ----

        def _init_db(self) -> None:
            self._bootstrap_pg_schema()
            self._reconcile_columns()
            if getattr(self, "_seed_on_init", True):
                conn = self._get_conn()
                try:
                    self._seed_agent_templates_from_json_files(conn)
                except Exception as e:
                    logger.warning("Postgres seed step failed: %s", e)
                finally:
                    conn.close()
            logger.info("PostgresBackend initialized (host=%s db=%s)", self._cfg.host, self._cfg.database)

        def _bootstrap_pg_schema(self) -> None:
            import psycopg
            ddl = render_postgres()
            with psycopg.connect(self._conninfo, autocommit=True) as conn:
                try:
                    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                except Exception as e:
                    logger.warning("CREATE EXTENSION vector failed: %s", e)
                for stmt in _iter_ddl_statements(ddl):
                    try:
                        conn.execute(stmt)
                    except Exception as e:
                        logger.warning("DDL statement failed: %s :: %s", e, stmt[:100])

        def _reconcile_columns(self) -> None:
            """Add any columns present in the reference SQLite schema but missing
            in Postgres (migration-proof parity). Tables empty at bootstrap so
            ADD COLUMN is always safe."""
            import psycopg
            try:
                ref = _reference_sqlite_columns()
            except Exception as e:
                logger.warning("Could not build reference schema for reconcile: %s", e)
                return
            with psycopg.connect(self._conninfo, autocommit=True) as conn:
                for table, cols in ref.items():
                    rows = conn.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=%s",
                        (table,),
                    ).fetchall()
                    existing = {r[0] for r in rows}
                    if not existing:
                        continue  # not a Postgres table (e.g. FTS shadow tables)
                    for col, decl in cols.items():
                        if col not in existing:
                            pgt = _sqlite_type_to_pg(decl)
                            try:
                                conn.execute(
                                    f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{col}" {pgt}'
                                )
                                logger.info("Reconcile: added %s.%s (%s)", table, col, pgt)
                            except Exception as e:
                                logger.warning("Reconcile ADD %s.%s failed: %s", table, col, e)

        def close(self):
            if self._pool is not None:
                try:
                    self._pool.close()
                except Exception:
                    pass

        # ---- memory: embeddings (pgvector) + full-text (tsquery) overrides ----

        async def _embed_and_store_chunks(self, conn, memory_id, text, source):
            import json as _json  # noqa
            import uuid as _uuidm
            import numpy as np
            from app.agent.embed import embed_text
            chunks = self._chunk_text(text, max_chars=500)
            conn.execute(
                "DELETE FROM memory_chunks WHERE memory_id = ? AND chunk_source = ?",
                (memory_id, source),
            )
            for i, chunk in enumerate(chunks):
                if not chunk.strip():
                    continue
                emb = None
                try:
                    emb = np.array(await embed_text(chunk), dtype=np.float32)
                except Exception as e:
                    logger.warning("Chunk embed failed (idx=%d mem=%s): %s", i, memory_id, e)
                conn.execute(
                    """INSERT OR REPLACE INTO memory_chunks
                       (id, memory_id, chunk_index, chunk_text, chunk_source, embedding, token_count)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (str(_uuidm.uuid4()), memory_id, i, chunk, source, emb, len(chunk.split())),
                )

        async def _fts5_search(self, user_id, query, limit=10):
            import json as _json
            if not query or not query.strip():
                return []
            expr = ("to_tsvector('english', coalesce(m.title,'') || ' ' || "
                    "coalesce(m.compiled_truth,'') || ' ' || coalesce(m.timeline,''))")
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    f"""SELECT m.*, ts_rank({expr}, plainto_tsquery('english', ?)) AS rank
                        FROM memories m
                        WHERE m.user_id = ? AND {expr} @@ plainto_tsquery('english', ?)
                        ORDER BY rank DESC LIMIT ?""",
                    (query, user_id, query, limit),
                ).fetchall()
                out = []
                for r in rows:
                    d = dict(r)
                    d["frontmatter"] = _json.loads(d.get("frontmatter") or "{}")
                    out.append(d)
                return out
            finally:
                conn.close()

        async def _vector_search(self, user_id, query_text, limit=10):
            import json as _json
            import numpy as np
            from app.agent.embed import embed_text
            try:
                qv = np.array(await embed_text(query_text), dtype=np.float32)
            except Exception as e:
                logger.warning("Query embed failed, skipping vector search: %s", e)
                return []
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    """SELECT m.slug, m.title, m.compiled_truth, m.timeline, m.page_type,
                              m.frontmatter, m.created_at, m.updated_at,
                              MIN(mc.embedding <=> ?) AS dist
                       FROM memory_chunks mc JOIN memories m ON m.id = mc.memory_id
                       WHERE m.user_id = ? AND mc.embedding IS NOT NULL
                       GROUP BY m.id, m.slug, m.title, m.compiled_truth, m.timeline,
                                m.page_type, m.frontmatter, m.created_at, m.updated_at
                       ORDER BY dist ASC LIMIT ?""",
                    (qv, user_id, limit),
                ).fetchall()
            finally:
                conn.close()
            out = []
            for r in rows:
                d = dict(r)
                dist = d.pop("dist", None)
                d["frontmatter"] = _json.loads(d.get("frontmatter") or "{}")
                d["rank"] = round(1.0 - float(dist), 4) if dist is not None else 0.0
                out.append(d)
            return out

        # ---- doc_chunks: embeddings + full-text overrides ----

        async def doc_chunk_upsert(self, data_source_id, source_ref, chunk_index,
                                   chunk_text, content_hash=None, embedding=None, metadata=None):
            import json as _json
            import uuid as _uuidm
            import numpy as np
            chunk_id = str(_uuidm.uuid4())
            emb = np.array(embedding, dtype=np.float32) if embedding is not None else None
            async with self._write_lock:
                conn = self._get_conn()
                try:
                    conn.execute(
                        """DELETE FROM doc_chunks
                           WHERE data_source_id = ? AND source_ref = ? AND chunk_index = ?""",
                        (data_source_id, source_ref, chunk_index),
                    )
                    conn.execute(
                        """INSERT INTO doc_chunks
                           (id, data_source_id, source_ref, chunk_index, chunk_text,
                            content_hash, embedding, token_count, metadata)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (chunk_id, data_source_id, source_ref, chunk_index, chunk_text,
                         content_hash, emb, len(chunk_text.split()) if chunk_text else 0,
                         _json.dumps(metadata or {})),
                    )
                    conn.commit()
                    return chunk_id
                finally:
                    conn.close()

        async def _doc_fts5_search(self, data_source_id, query, limit):
            if not query or not query.strip():
                return []
            expr = "to_tsvector('english', coalesce(dc.chunk_text,''))"
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    f"""SELECT dc.id, dc.source_ref, dc.chunk_index, dc.chunk_text
                        FROM doc_chunks dc
                        WHERE dc.data_source_id = ? AND {expr} @@ plainto_tsquery('english', ?)
                        ORDER BY ts_rank({expr}, plainto_tsquery('english', ?)) DESC LIMIT ?""",
                    (data_source_id, query, query, limit),
                ).fetchall()
                return [
                    {"id": r["id"], "source_ref": r["source_ref"],
                     "chunk_index": r["chunk_index"], "chunk_text": r["chunk_text"]}
                    for r in rows
                ]
            finally:
                conn.close()

        async def _doc_vector_search(self, data_source_id, query_text, limit):
            import numpy as np
            from app.agent.embed import embed_text
            try:
                qv = np.array(await embed_text(query_text), dtype=np.float32)
            except Exception as e:
                logger.warning("doc_chunks query embed failed: %s", e)
                return []
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    """SELECT id, source_ref, chunk_index, chunk_text, (embedding <=> ?) AS dist
                       FROM doc_chunks
                       WHERE data_source_id = ? AND embedding IS NOT NULL
                       ORDER BY dist ASC LIMIT ?""",
                    (qv, data_source_id, limit),
                ).fetchall()
                return [
                    {"id": r["id"], "source_ref": r["source_ref"],
                     "chunk_index": r["chunk_index"], "chunk_text": r["chunk_text"]}
                    for r in rows
                ]
            finally:
                conn.close()

    return _PostgresBackend


def build_postgres_backend(cfg: DBConnectionConfig, password: Optional[str] = None, seed: bool = True):
    """Instantiate the live Postgres backend. seed=False builds the schema only
    (used by the SQLite→Postgres migration, where SQLite rows are the source of truth)."""
    cls = _make_postgres_backend_class()
    return cls(cfg, password=password, seed=seed)

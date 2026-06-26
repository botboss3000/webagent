"""
Database viewer API — backend-aware DB introspection for the terminal UI.

Reads/writes the active application database: SQLite by default, or Postgres
when it is the active backend (the main DB routes through a standalone autocommit
PgPortableConnection; SQLite-dialect SQL is translated on the fly — see
app/db/pg_portable.py). Temp/optimizer `.db` scratch files are always SQLite.
Dispatch happens in `_open()`.
"""

import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from app.auth.db_auth import require_db_auth
from app.auth.jwt import decode_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/db", tags=["db_viewer"])


def _is_loser_row(metadata_str) -> bool:
    """True when an interactions row is a parallel-racing LOSER (metadata
    parallel_loser=True). These are persisted only for diagnostics; the chat
    transcript should show one answer per turn (the winner), not the losers too."""
    if not metadata_str:
        return False
    try:
        return bool(json.loads(metadata_str).get("parallel_loser"))
    except (json.JSONDecodeError, TypeError, AttributeError):
        return False

# SQLite files live under data/db/ alongside every other runtime DB. Import the
# canonical location from app.db.local so this stays in lockstep if it ever moves
# again — recomputing the path here is what left this endpoint pointing at the old
# app/db/ location after the relocation, so the chat dropdown read an empty/missing
# file and every session showed as "New Session".
from app.db.local import DB_DIR as _DB_DIR_STR
_DB_FILES_DIR = Path(_DB_DIR_STR)

# In-process cache for /tables responses. Keyed by absolute db path.
# Counts can be expensive for large tables; clients poll this every ~20s, and
# the underlying SQLite file is updated by writes that we can't easily hook,
# so we key on (mtime, size) and fall back to a short TTL.
_TABLES_CACHE: dict[str, tuple[float, float, int, dict]] = {}
_TABLES_CACHE_TTL_S = 5.0


def _invalidate_tables_cache(db_path: Path) -> None:
    _TABLES_CACHE.pop(str(db_path), None)


# ── Per-user row-level access control ─────────────────────────────────────
# Tables where filtering uses a column on the table itself.
_USER_ID_COLUMN: dict[str, str] = {
    "sessions": "user_id",
    "session_summaries": "user_id",
    "memories": "user_id",
    "agent_credentials": "user_id",
    "auth_elements": "user_id",
    "data_sources": "user_id",
    "skills": "user_id",
    "skill_executions": "user_id",
    "skill_feedback": "user_id",
    "attachments": "user_id",
    "channel_identities": "user_id",
    "webhook_registrations": "user_id",
    "user_profiles": "user_id",
    "tenant_key_meta": "user_id",
    "usage_events": "user_id",
    "subscriptions": "user_id",
    "trials": "user_id",
    "payment_accounts": "user_id",
    "payments": "user_id",
    "billing_exemptions": "user_id",
    "linking_codes": "source_user_id",
}

# Tables reached via a FK to a table with a user_id column.
# Map: table -> (fk_column, parent_table, parent_pk, parent_user_col)
_LINKED_USER_TABLES: dict[str, tuple[str, str, str, str]] = {
    "interactions": ("session_id", "sessions", "id", "user_id"),
    "session_interrupts": ("session_id", "sessions", "id", "user_id"),
    "memory_chunks": ("memory_id", "memories", "id", "user_id"),
    "webhook_event_log": ("webhook_id", "webhook_registrations", "id", "user_id"),
    "doc_chunks": ("data_source_id", "data_sources", "id", "user_id"),
}

# Tables only admins may read. Catalogs, system configs, and agent-scoped
# tables whose membership lives in JSON arrays we don't want to parse in SQL.
_ADMIN_ONLY_TABLES: set[str] = {
    "agent_templates",
    "agent_prompt_templates",
    "app_meta",
    "billing_configs",
    "tools",
    "agents",
    "agent_connections",
    "agent_abilities",
    "agent_data_sources",
    "agent_prompts",
}


async def require_admin(_auth: dict = Depends(require_db_auth)) -> dict:
    """FastAPI dependency: require the caller to be a global admin."""
    _uid, is_admin = _get_caller(_auth)
    if not is_admin:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return _auth


def _get_caller(payload: Optional[dict]) -> tuple[Optional[str], bool]:
    """Extract (user_id, is_admin) from a JWT payload.

    Admin status is looked up from local.db's user_profiles table — the
    same source `app.db.local.is_user_admin` consults — so the check is
    consistent regardless of which db file is being viewed.
    """
    if not payload:
        return None, False
    user_id = payload.get("user_id") or payload.get("sub")
    if not user_id:
        return None, False
    is_admin = False
    try:
        conn, _dialect = _open("local.db")
        try:
            row = conn.execute(
                "SELECT is_admin FROM user_profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
        finally:
            conn.close()
        if row and row[0]:
            is_admin = True
    except Exception:
        pass
    return user_id, is_admin


def _acl_clause(table: str, user_id: Optional[str], is_admin: bool) -> tuple[Optional[str], list]:
    """Return (where_fragment, params) restricting `table` to rows owned by `user_id`.

    - Admins: returns (None, []) — no filtering.
    - Non-admins on admin-only tables, unknown tables, or with no user_id:
      returns ("1=0", []) — denies all rows.
    - Otherwise returns the table-specific predicate.
    """
    if is_admin:
        return None, []
    if not user_id:
        return "1=0", []
    if table in _ADMIN_ONLY_TABLES:
        return "1=0", []
    if table in _USER_ID_COLUMN:
        col = _USER_ID_COLUMN[table]
        return f'"{col}" = ?', [user_id]
    if table in _LINKED_USER_TABLES:
        fk, parent, parent_pk, parent_user = _LINKED_USER_TABLES[table]
        return (
            f'"{fk}" IN (SELECT "{parent_pk}" FROM "{parent}" WHERE "{parent_user}" = ?)',
            [user_id],
        )
    if table == "wallets":
        return '"owner_type" = ? AND "owner_id" = ?', ["user", user_id]
    if table == "wallet_transactions":
        return (
            '"wallet_id" IN (SELECT "id" FROM "wallets" WHERE "owner_type" = ? AND "owner_id" = ?)',
            ["user", user_id],
        )
    # Unknown table — deny by default for non-admins.
    return "1=0", []


def _is_open_access_mode() -> bool:
    """True when the app is running in 'open' access mode.

    In open mode the auth middleware already trusts every request as the
    bootstrap admin (single-user / local convenience, no cross-tenant risk).
    The per-session participant gates below must mirror that: when the app is
    reached through a Cloudflare Tunnel etc., the frontend has no localhost
    JWT to present, so a strict valid-token check would wrongly mark EVERY
    session `restricted` and the chat would show "New session" for all of
    them. Honour open mode here so the gate matches the middleware.
    """
    try:
        from app.admin.settings import get_access_mode
        return get_access_mode() == "open"
    except Exception:
        return False


def _get_db_path(name: str = "local.db") -> Path:
    if Path(name).name != name:
        raise HTTPException(status_code=400, detail="Database name must be a plain filename")
    db_path = _DB_FILES_DIR / name
    # When Postgres is the active backend, the main DB has no file — callers that
    # only need it for the (now Postgres) connection still call this; don't 404.
    if not db_path.exists() and _pg_conninfo_for(name) is None:
        raise HTTPException(status_code=404, detail=f"Database '{name}' not found at {db_path}")
    return db_path


def _pg_conninfo_for(db: str):
    """Return PG conninfo when Postgres is the active backend AND `db` is the
    main database. Temp/optimizer .db files always stay on SQLite."""
    if db not in ("local.db", "", None):
        return None
    try:
        from app.db import active_postgres_conninfo
        return active_postgres_conninfo()
    except Exception:
        return None


def _open(db: str = "local.db"):
    """Open the right connection for `db`. Returns (conn, dialect).

    - Postgres active + main DB → standalone autocommit PgPortableConnection
      ('postgres'). Autocommit isolates each statement so the endpoints'
      best-effort per-statement guards behave like they do on SQLite.
    - Otherwise the SQLite file ('sqlite').
    """
    conninfo = _pg_conninfo_for(db)
    if conninfo:
        from app.db.pg_portable import connect_standalone
        return connect_standalone(conninfo), "postgres"
    conn = sqlite3.connect(str(_get_db_path(db)))
    conn.row_factory = sqlite3.Row
    return conn, "sqlite"


def _resolve_session_db(session_id: str, fallback_db: str = "local.db") -> str:
    """If a session has temp_db_path in its metadata, route to that temp DB.
    Otherwise return fallback_db. Reads from the active backend (SQLite or PG)."""
    try:
        conn, _dialect = _open(fallback_db)
        try:
            row = conn.execute("SELECT metadata FROM sessions WHERE id=?", (session_id,)).fetchone()
        finally:
            conn.close()
        if row and row['metadata']:
            meta = json.loads(row['metadata'])
            tdb = meta.get('temp_db_path', '')
            if tdb:
                return os.path.basename(tdb)
    except Exception:
        pass
    return fallback_db


@router.get("/tables")
async def list_tables(
    db: str = Query("local.db", description="Database filename"),
    _auth: dict = Depends(require_db_auth),
):
    """List all tables in the database."""
    user_id, is_admin = _get_caller(_auth)
    is_pg = _pg_conninfo_for(db) is not None
    cache_key = f"{db}|{1 if is_admin else 0}|{user_id or ''}"

    # File-stat cache only applies to SQLite (Postgres has no file to stat, and
    # its row counts change without touching any file). On PG we always compute.
    mtime, size = 0.0, 0
    if not is_pg:
        db_path = _get_db_path(db)
        try:
            st = db_path.stat()
            mtime, size = st.st_mtime, st.st_size
        except OSError:
            mtime, size = 0.0, 0
        now = time.monotonic()
        cached = _TABLES_CACHE.get(cache_key)
        if cached is not None:
            c_mtime, c_size, c_expiry, c_payload = cached
            if c_mtime == mtime and c_size == size and now < c_expiry:
                return c_payload

    try:
        conn, _dialect = _open(db)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = []
        for row in cur.fetchall():
            name = row["name"]
            # Get column info
            cur.execute(f"PRAGMA table_info(\"{name}\")")
            columns = [
                {"name": col[1], "type": col[2], "notnull": bool(col[3]), "pk": bool(col[5])}
                for col in cur.fetchall()
            ]
            # Row count is scoped to what the caller is allowed to see.
            clause, params = _acl_clause(name, user_id, is_admin)
            where_sql = f" WHERE {clause}" if clause else ""
            try:
                cur.execute(f'SELECT COUNT(*) FROM "{name}"{where_sql}', params)
                count = cur.fetchone()[0]
            except Exception:
                # ACL parent table missing (e.g. FTS or stale schema) — treat as 0
                # for non-admins; admins still see the real count via the
                # unfiltered query above falling through.
                count = 0
            tables.append({"name": name, "columns": columns, "row_count": count})
        conn.close()
        payload = {"tables": tables, "db": db, "is_admin": is_admin}
        if not is_pg:
            _TABLES_CACHE[cache_key] = (mtime, size, time.monotonic() + _TABLES_CACHE_TTL_S, payload)
        return payload
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/users")
async def list_users(
    db: str = Query("local.db", description="Database filename"),
    _auth: dict = Depends(require_db_auth),
):
    """List distinct user IDs from the database.

    Non-admins only see their own user_id (so they can't enumerate other
    accounts via this endpoint).
    """
    user_id, is_admin = _get_caller(_auth)
    if not is_admin:
        return {"users": [user_id] if user_id else [], "db": db}

    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        users = set()
        for tbl in ["sessions", "interactions", "messages"]:
            try:
                cur.execute(f'SELECT DISTINCT user_id FROM "{tbl}" WHERE user_id IS NOT NULL')
                for row in cur.fetchall():
                    users.add(row[0])
            except Exception:
                pass
        conn.close()
        return {"users": sorted(users), "db": db}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    db: str = Query("local.db", description="Database filename"),
    _auth: dict = Depends(require_db_auth),
):
    """Delete all sessions, interactions, and messages for a user."""
    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        cur = conn.cursor()
        deleted = {}

        # Get all session IDs for this user
        sessions = conn.execute(
            "SELECT id FROM sessions WHERE user_id = ?", (user_id,)
        ).fetchall()
        session_ids = [row[0] for row in sessions]

        # Delete interactions for those sessions
        for sid in session_ids:
            cur.execute("DELETE FROM interactions WHERE session_id = ?", (sid,))
            deleted["interactions"] = deleted.get("interactions", 0) + cur.rowcount
            try:
                cur.execute("DELETE FROM pipeline_events WHERE session_id = ?", (sid,))
            except Exception:
                pass
            try:
                cur.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            except Exception:
                pass

        # Delete sessions
        cur.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        deleted["sessions"] = cur.rowcount

        # Delete session summaries
        try:
            cur.execute("DELETE FROM session_summaries WHERE user_id = ?", (user_id,))
            deleted["summaries"] = cur.rowcount
        except Exception:
            pass

        # Delete attachments
        try:
            cur.execute("DELETE FROM attachments WHERE user_id = ?", (user_id,))
            deleted["attachments"] = cur.rowcount
        except Exception:
            pass

        conn.commit()
        conn.close()
        logger.info(f"Deleted user {user_id[:12]}: {deleted}")
        return {"success": True, "user_id": user_id, "deleted": deleted}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    request: Request,
    db: str = Query("local.db", description="Database filename"),
    permanent: bool = Query(False, description="Hard-delete instead of recycling"),
):
    """Recycle a session (soft delete) or, with ``permanent=true``, erase it.

    Default: the session is moved to the bin (status -> 'recycled') and hidden
    from the chat dropdown, but its transcript is kept. It is truly erased only
    when its agent is permanently emptied from the recycling bin (or via an
    explicit ``permanent=true`` call from the future sessions page).
    """
    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        cur = conn.cursor()

        # Ownership gate: only the session's owner/participant (or an admin, or
        # open/local mode) may delete it. Returns None for a phantom session with
        # no `sessions` row — those fall through to the orphan-transcript cleanup
        # below, exactly like the transcript read routes.
        if _session_access_ok(cur, session_id, request, None) is False:
            conn.close()
            raise HTTPException(status_code=403, detail="Not authorized for this session")

        # Resolve the run-family children (optimizer Planner/Closer + spawned
        # helpers) ONCE up front, so both the soft- and hard-delete paths take the
        # whole family with the parent. Best-effort; empty when nothing hangs off.
        try:
            from app.db.local import resolve_child_sessions
            child_ids = resolve_child_sessions(conn, [session_id])
        except Exception as _re:  # noqa: BLE001
            logger.debug("child-session resolve on delete failed: %s", _re)
            child_ids = []

        # ── Soft delete (default): just flip the status, keep everything ──
        if not permanent:
            has_status = False
            try:
                cur.execute("PRAGMA table_info(sessions)")
                has_status = "status" in {row[1] for row in cur.fetchall()}
            except Exception:
                pass
            if has_status:
                cur.execute("UPDATE sessions SET status = 'recycled' WHERE id = ?", (session_id,))
                recycled = cur.rowcount
                # A real session row was recycled → carry its children to the bin
                # too. If NOTHING matched (recycled == 0), this is a "phantom"
                # session: the Sessions page derived it straight from the
                # interactions log because it has no row in `sessions`. There's
                # nothing to recycle, so a soft delete would be a silent no-op and
                # the row would reappear on the next reload. In that case we fall
                # through to a hard delete of its orphan transcript so it actually
                # disappears.
                if recycled:
                    children_recycled = 0
                    for cid in child_ids:
                        cur.execute("UPDATE sessions SET status = 'recycled' WHERE id = ?", (cid,))
                        children_recycled += cur.rowcount
                    conn.commit()
                    conn.close()
                    logger.info(f"Recycled session {session_id[:12]}: {recycled} session, "
                                f"{children_recycled} children")
                    return {
                        "success": True,
                        "session_id": session_id,
                        "recycled": recycled,
                        "children_recycled": children_recycled,
                    }
            # Fall through to hard delete if the column isn't there yet OR this is
            # a phantom session with no `sessions` row to recycle.

        # ── Hard delete: erase the base session AND its whole run-family ──
        # Phantom-safe: deleting by session_id removes the orphan interactions
        # even when no `sessions` row exists.
        targets = [session_id] + [c for c in child_ids if c != session_id]
        interactions_deleted = 0
        session_deleted = 0
        for tid in targets:
            cur.execute('DELETE FROM interactions WHERE session_id = ?', (tid,))
            interactions_deleted += cur.rowcount
            cur.execute('DELETE FROM session_summaries WHERE session_id = ?', (tid,))
            try:
                cur.execute('DELETE FROM pipeline_events WHERE session_id = ?', (tid,))
            except Exception:
                pass
            cur.execute('DELETE FROM sessions WHERE id = ?', (tid,))
            session_deleted += cur.rowcount

        # Cascade: an orchestrator session takes its spawned CLONES with it —
        # their agents, sessions and transcripts (recursively). Best-effort; only
        # touches status='clone' agents, so a real agent is never caught.
        clones_deleted = 0
        try:
            from app.db.local import cascade_delete_clones
            clones_deleted = cascade_delete_clones(conn, targets)
        except Exception as _ce:  # noqa: BLE001
            logger.debug("clone cascade on session delete failed: %s", _ce)

        conn.commit()
        conn.close()

        logger.info(f"Deleted session {session_id[:12]}: {session_deleted} session(s) "
                    f"(+{len(targets) - 1} family), {interactions_deleted} interactions, "
                    f"{clones_deleted} clones")
        return {
            "success": True,
            "session_id": session_id,
            "session_deleted": session_deleted,
            "interactions_deleted": interactions_deleted,
            "children_deleted": len(targets) - 1,
            "clones_deleted": clones_deleted,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/{session_id}/restore")
async def restore_session(
    session_id: str,
    request: Request,
    db: str = Query("local.db", description="Database filename"),
):
    """Restore a recycled session back to active status."""
    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        cur = conn.cursor()
        # Ownership gate (open/local mode + admins pass; non-owner refused).
        if _session_access_ok(cur, session_id, request, None) is False:
            conn.close()
            raise HTTPException(status_code=403, detail="Not authorized for this session")
        cur.execute("UPDATE sessions SET status = 'active', updated_at = datetime('now') WHERE id = ? AND status = 'recycled'", (session_id,))
        restored = cur.rowcount
        # Bring the run-family back with the parent (mirrors the recycle cascade).
        children_restored = 0
        try:
            from app.db.local import resolve_child_sessions
            for cid in resolve_child_sessions(conn, [session_id]):
                cur.execute("UPDATE sessions SET status = 'active', updated_at = datetime('now') WHERE id = ? AND status = 'recycled'", (cid,))
                children_restored += cur.rowcount
        except Exception as _re:  # noqa: BLE001
            logger.debug("child-session restore cascade failed: %s", _re)
        conn.commit()
        conn.close()
        return {"success": True, "session_id": session_id, "restored": restored, "children_restored": children_restored}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class SessionPatchRequest(BaseModel):
    """Body for PATCH /sessions/{id} — rename, pin, and/or hide."""
    title: Optional[str] = None
    pinned: Optional[bool] = None
    hidden: Optional[bool] = None


class SessionReorderRequest(BaseModel):
    """Body for POST /sessions/reorder — persist manual drag order."""
    user_id: str
    order: list[str]  # session ids, top-to-bottom (index 0 = top of the list)


@router.patch("/sessions/{session_id}")
async def patch_session(
    session_id: str,
    req: SessionPatchRequest,
    request: Request,
    db: str = Query("local.db", description="Database filename"),
):
    """Update a session's title, pinned, and/or hidden state."""
    if req.title is None and req.pinned is None and req.hidden is None:
        raise HTTPException(status_code=400, detail="Provide title, pinned, and/or hidden")

    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        cur = conn.cursor()

        # Ownership gate (open/local mode + admins pass; non-owner refused).
        if _session_access_ok(cur, session_id, request, None) is False:
            conn.close()
            raise HTTPException(status_code=403, detail="Not authorized for this session")

        sets = []
        params: list[object] = []
        if req.title is not None:
            sets.append("title = ?")
            params.append(req.title)
            # A manual rename wins over the auto-namer: lock it so the background
            # Session Namer ability (plugins/abilities/Core/session_titler/)
            # stops overwriting it.
            try:
                cur.execute("SELECT metadata FROM sessions WHERE id = ?", (session_id,))
                _mrow = cur.fetchone()
                _meta = json.loads(_mrow[0]) if (_mrow and _mrow[0]) else {}
                if not isinstance(_meta, dict):
                    _meta = {}
            except Exception:
                _meta = {}
            _meta["auto_title_locked"] = True
            sets.append("metadata = ?")
            params.append(json.dumps(_meta))
        if req.pinned is not None:
            # Confirm column exists before writing
            cur.execute("PRAGMA table_info(sessions)")
            cols = {row[1] for row in cur.fetchall()}
            if "pinned" not in cols:
                cur.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
            sets.append("pinned = ?")
            params.append(1 if req.pinned else 0)
        if req.hidden is not None:
            # Confirm column exists before writing
            cur.execute("PRAGMA table_info(sessions)")
            cols = {row[1] for row in cur.fetchall()}
            if "hidden" not in cols:
                cur.execute("ALTER TABLE sessions ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
            sets.append("hidden = ?")
            params.append(1 if req.hidden else 0)
        sets.append("updated_at = CURRENT_TIMESTAMP")

        params.append(session_id)
        cur.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id = ?", params)
        affected = cur.rowcount
        conn.commit()
        conn.close()

        if affected == 0:
            raise HTTPException(status_code=404, detail="Session not found")
        return {"success": True, "session_id": session_id, "affected": affected}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/reorder")
async def reorder_sessions(
    req: SessionReorderRequest,
    request: Request,
    db: str = Query("local.db", description="Database filename"),
):
    """Persist the manual drag order of the requesting user's sessions.

    Each id in ``order`` gets sort_order = its index (0 = top). Writes are
    scoped to sessions owned by ``user_id`` so a user can't reorder another
    user's rows. Sessions not listed keep their existing sort_order and fall
    after the ordered set (NULLS LAST) in list_sessions.
    """
    # Identity gate: the claimed user_id must be the authenticated caller (or an
    # admin); open/local mode is full-trust. Without this a caller could reorder
    # another user's sessions by passing their user_id + session ids.
    if not _is_open_access_mode():
        from app.auth.identity import assert_caller_is
        await assert_caller_is(request, req.user_id)
    if not req.order:
        return {"success": True, "updated": 0}
    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        cur = conn.cursor()
        # Auto-add the column on older DBs (mirrors the pinned guard above).
        cur.execute("PRAGMA table_info(sessions)")
        cols = {row[1] for row in cur.fetchall()}
        if "sort_order" not in cols:
            cur.execute("ALTER TABLE sessions ADD COLUMN sort_order INTEGER")
        updated = 0
        for position, sid in enumerate(req.order):
            cur.execute(
                "UPDATE sessions SET sort_order = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND user_id = ?",
                (position, sid, req.user_id),
            )
            updated += cur.rowcount
        conn.commit()
        conn.close()
        return {"success": True, "updated": updated}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sessions")
async def list_sessions(
    request: Request,
    user_id: str = Query(..., description="User ID"),
    db: str = Query("local.db", description="Database filename"),
    agent_id: Optional[str] = Query(None, description="Filter to sessions bound to this agent"),
    limit: int = Query(20, ge=1, le=50, description="Max sessions to return"),
    include_hidden: bool = Query(False, description="Include sessions flagged hidden"),
):
    """List sessions for a user (owner or participant).

    When ``agent_id`` is supplied, only sessions bound to that agent are
    returned. Sessions with a NULL ``agent_id`` (never bound to an agent)
    are filtered out in that case — they appear as orphans that don't
    belong to any specific agent.

    Pinned sessions are always returned first (in sort_order), then the
    most recent unpinned sessions up to the limit. ``total_count`` reports
    the total number of unpinned sessions matching the filter.
    """
    # Resolve requester identities from token
    _token = ""
    _auth_header = request.headers.get("Authorization", "")
    if _auth_header.startswith("Bearer "):
        _token = _auth_header[7:]
    if not _token:
        _token = request.query_params.get("token", "")
    _payload = decode_token(_token) if _token else None
    requesting_user_id = _payload.get("user_id") if _payload else None
    requesting_username = _payload.get("sub") if _payload else None
    # The SQL below only returns rows whose owner/participant id is in this set.
    # A claimed `user_id` query param is honoured as a fallback identity ONLY
    # when there's no valid token (unauthenticated local UUID users) or in open
    # mode — so a token-bearing caller can't pass `?user_id=<someone else>` to
    # read another user's session list.
    requester_identities = {v for v in (requesting_user_id, requesting_username) if v}
    if (not _payload or _is_open_access_mode()) and user_id:
        requester_identities.add(user_id)

    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        sessions = []
        total_count = 0
        try:
            # Detect optional columns (older DBs may not have them yet)
            cur.execute("PRAGMA table_info(sessions)")
            sess_cols = {row[1] for row in cur.fetchall()}
            has_pinned = "pinned" in sess_cols
            has_sort_order = "sort_order" in sess_cols
            has_read_at = "read_at" in sess_cols
            has_status = "status" in sess_cols
            has_hidden = "hidden" in sess_cols

            select_cols = 's.id, s.title, s.created_at, s.user_id, s.participants, s.agent_id, s.updated_at, a.name AS agent_name, a.metadata AS agent_metadata'
            if has_pinned:
                select_cols += ', s.pinned'
            if has_read_at:
                select_cols += ', s.read_at'
            if has_hidden:
                select_cols += ', s.hidden'

            where_clause = '(s.agent_id IS NULL OR a.id IS NOT NULL)'
            params: list = []
            if agent_id:
                where_clause = 's.agent_id = ?'
                params.append(agent_id)

            # Recycling bin: hide sessions soft-deleted from the chat header.
            # A NULL status counts as live (Postgres adds the column without a
            # default, so pre-existing rows read back as NULL).
            if has_status:
                where_clause = f"({where_clause}) AND (s.status IS NULL OR s.status != 'recycled')"

            # Hidden sessions: the chat-header "manage list" eye-toggle declutters
            # the dropdown without deleting. Excluded by default (NULL = visible,
            # same convention as status); revealed when include_hidden is set.
            if has_hidden and not include_hidden:
                where_clause = f"({where_clause}) AND (s.hidden IS NULL OR s.hidden = 0)"

            # Hide spawned-clone sessions from the sidebar: they belong to
            # ephemeral clone agents and live/die with their orchestrator (see
            # cascade_delete_clones). Their ids are always prefixed 'spawn-'.
            # Optimizer Planner ('optimizer-*') and Closer ('closer-*') sessions
            # are likewise children of the BASE session the optimizer ran on —
            # they're reached by expanding that base row in the tree / via its
            # sub-header tabs, not as their own top-level rows.
            where_clause = (
                f"({where_clause}) AND (s.id NOT LIKE 'spawn-%') "
                f"AND (s.id NOT LIKE 'closer-%') AND (s.id NOT LIKE 'optimizer-%')"
            )

            # Sort by last active time (updated_at), newest first.
            # Pinned sessions come first, then the rest sorted by last active.
            order_parts = []
            if has_pinned:
                order_parts.append('s.pinned DESC')
            if has_sort_order:
                order_parts.append('(s.sort_order IS NULL)')
                order_parts.append('s.sort_order ASC')
            order_parts.append('s.updated_at DESC NULLS LAST')
            order_clause = ', '.join(order_parts)

            # Pre-fetch run statuses for all sessions in one query
            run_statuses = {}
            try:
                cur2 = conn.cursor()
                cur2.execute("SELECT session_id, status, updated_at FROM session_runs")
                for r in cur2.fetchall():
                    run_statuses[r[0]] = {"status": r[1], "updated_at": r[2]}
            except Exception:
                pass

            # Pre-fetch child counts so a parent row can show an expand caret.
            # A session has children if it is an orchestrator (spawned helpers in
            # agent_spawns) or a BASE session an optimizer ran on (its Planner
            # 'optimizer-*' + Closer 'closer-*' members). One small query each;
            # tallied into child_counts keyed by the parent (base) sid.
            child_counts: dict = {}
            try:
                for r in cur2.execute(
                    "SELECT orchestrator_session_id AS p, COUNT(*) AS n FROM agent_spawns "
                    "GROUP BY orchestrator_session_id"
                ).fetchall():
                    if r[0]:
                        child_counts[r[0]] = child_counts.get(r[0], 0) + int(r[1] or 0)
            except Exception:
                pass
            # Optimizer Planners: each is a child of its base session
            # (metadata.target_session). Build planner→base map for the closers.
            _planner_base: dict = {}
            try:
                for r in cur2.execute(
                    "SELECT id, metadata FROM sessions WHERE id LIKE 'optimizer-%'"
                ).fetchall():
                    try:
                        _pm = json.loads(r[1] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        _pm = {}
                    _base = _pm.get("target_session")
                    if _base:
                        _planner_base[r[0]] = _base
                        child_counts[_base] = child_counts.get(_base, 0) + 1
            except Exception:
                pass
            # Optimizer Closers: also children of the base session, resolved via
            # their Planner (metadata.source_optimizer_session → planner → base).
            try:
                for r in cur2.execute(
                    "SELECT metadata FROM sessions WHERE id LIKE 'closer-%'"
                ).fetchall():
                    try:
                        _cm = json.loads(r[0] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        _cm = {}
                    _base = _planner_base.get(_cm.get("source_optimizer_session"))
                    if _base:
                        child_counts[_base] = child_counts.get(_base, 0) + 1
            except Exception:
                pass

            # ── Step 1: fetch all pinned sessions (no limit) ──
            pinned_where = f'({where_clause}) AND s.pinned = 1' if has_pinned else '1=0'
            pinned_sql = (
                f'SELECT {select_cols} '
                f'FROM sessions s LEFT JOIN agents a ON s.agent_id = a.id '
                f'WHERE {pinned_where} '
                f'ORDER BY {order_clause}'
            )
            cur.execute(pinned_sql, params)
            pinned_rows = cur.fetchall()

            # ── Step 2: count unpinned sessions ──
            unpinned_where = f'({where_clause})'
            if has_pinned:
                unpinned_where += ' AND (s.pinned IS NULL OR s.pinned = 0)'
            count_sql = (
                f'SELECT COUNT(*) FROM sessions s LEFT JOIN agents a ON s.agent_id = a.id '
                f'WHERE {unpinned_where}'
            )
            cur.execute(count_sql, params)
            total_count = cur.fetchone()[0]

            # ── Step 3: fetch unpinned sessions with limit ──
            unpinned_limit = max(0, limit - len(pinned_rows))
            unpinned_sql = (
                f'SELECT {select_cols} '
                f'FROM sessions s LEFT JOIN agents a ON s.agent_id = a.id '
                f'WHERE {unpinned_where} '
                f'ORDER BY {order_clause} '
                f'LIMIT ?'
            )
            cur.execute(unpinned_sql, params + [unpinned_limit])
            unpinned_rows = cur.fetchall()

            # ── Step 4: merge — pinned first, then unpinned ──
            all_rows = list(pinned_rows) + list(unpinned_rows)

            for row in all_rows:
                owner_id = row[3]
                participants_raw = row[4] or "[]"
                try:
                    participants = json.loads(participants_raw)
                except (json.JSONDecodeError, TypeError):
                    participants = []
                participant_ids = {p.get("id") for p in participants if isinstance(p, dict)}
                all_ids = ({owner_id} | participant_ids) - {None}
                if requester_identities & all_ids:
                    sid = row["id"]
                    pinned_val = bool(row["pinned"]) if has_pinned else False
                    read_at = row["read_at"] if has_read_at else None
                    run = run_statuses.get(sid)
                    run_status = run["status"] if run else None
                    run_updated_at = run["updated_at"] if run else None
                    # has_unread: session has a completed run that the user hasn't read yet
                    has_unread = False
                    if run_status in ("complete", "interrupted", "error") and run_updated_at:
                        if not read_at or run_updated_at > read_at:
                            has_unread = True
                    sessions.append({
                        "id": sid,
                        "title": row["title"] or sid[:12],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "agent_id": row["agent_id"],
                        "agent_name": row["agent_name"] or "",
                        "agent_icon": "",
                        "agent_engine": "",
                        "pinned": pinned_val,
                        "hidden": bool(row["hidden"]) if has_hidden else False,
                        "run_status": run_status,
                        "has_unread": has_unread,
                        "child_count": child_counts.get(sid, 0),
                    })
                    # Resolve icon and engine from agent metadata JSON
                    am = row["agent_metadata"]
                    if am:
                        try:
                            _m = json.loads(am)
                            if isinstance(_m, dict):
                                if _m.get("icon"):
                                    sessions[-1]["agent_icon"] = _m["icon"]
                                if _m.get("engine"):
                                    sessions[-1]["agent_engine"] = _m["engine"]
                        except Exception:
                            pass
        except Exception:
            pass

        conn.close()
        return {"sessions": sessions, "db": db, "total_count": total_count}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/{session_id}/read")
async def mark_session_read(session_id: str, request: Request, db: str = Query("local.db", description="Database filename")):
    """Mark a session as read by setting read_at to now."""
    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        # Ownership gate (open/local mode + admins pass; non-owner refused).
        if _session_access_ok(conn.cursor(), session_id, request, None) is False:
            conn.close()
            raise HTTPException(status_code=403, detail="Not authorized for this session")
        conn.execute(
            "UPDATE sessions SET read_at = ? WHERE id = ?",
            (datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f+00:00"), session_id),
        )
        conn.commit()
        conn.close()
        return {"ok": True, "session_id": session_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _session_title(cur, sid: str):
    """Best-effort session title lookup (None if absent)."""
    try:
        row = cur.execute("SELECT title FROM sessions WHERE id = ?", (sid,)).fetchone()
        return row["title"] if row else None
    except Exception:
        return None


def _session_meta_get(cur, sid: str, key: str):
    """Best-effort lookup of a single key from a session's metadata JSON."""
    try:
        row = cur.execute("SELECT metadata FROM sessions WHERE id = ?", (sid,)).fetchone()
        if not row:
            return None
        meta = json.loads(row["metadata"] or "{}")
        return meta.get(key) if isinstance(meta, dict) else None
    except (json.JSONDecodeError, TypeError, Exception):
        return None


@router.get("/sessions/{session_id}/related")
async def get_session_related(
    session_id: str,
    request: Request,
    db: str = Query("local.db", description="Database filename"),
):
    """Return spawned helpers and browser sessions related to this session.

    * Spawned helpers: rows from ``agent_spawns`` where the orchestrator
      session matches this session id. Returns the helper's session id,
      name, status, and result summary.
    * Browser sessions: rows from ``browser_sessions`` where the linked
      agent matches the session's agent_id (if any).
    """
    from app.auth.identity import assert_caller_is
    user_id = await assert_caller_is(request, None)

    db_path = _get_db_path(db)
    # `children` is the unified family-member list (spawned helpers OR optimizer
    # worker/closer sessions). Each carries a `label`/`role` so one frontend
    # renderer draws both the sub-header tab bar AND the session-list tree the
    # same way. `root_label` names the family-root tab ("Main" for an
    # orchestrator, "Planner" for an optimizer run). `spawns` is kept as a
    # back-compat alias for the spawn children.
    result = {"spawns": [], "children": [], "browser_sessions": [],
              "parent": None, "orchestrator": None,
              "group_kind": None, "root_label": "Main"}

    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # ── 1. Resolve the "family root" (orchestrator) for this session ──
        # The chat sub-header shows a persistent tab bar for a whole spawn
        # family — a "Main" tab for the orchestrator plus one tab per spawned
        # helper — and it must look identical whether the user is viewing the
        # orchestrator OR any of its spawns. So first find the family root:
        #   • if THIS session was itself spawned, the root is its orchestrator
        #     (one hop up), and `parent` is surfaced for back-compat;
        #   • otherwise THIS session IS the root.
        # Spawn ids are written as ``spawn-<uuid>`` by the orchestration
        # ability, so a spawn always resolves to exactly one orchestrator row.
        family_root = session_id
        try:
            tbl = cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='agent_spawns'"
            ).fetchone()
            if tbl:
                parent_row = cur.execute(
                    """SELECT orchestrator_session_id
                       FROM agent_spawns
                       WHERE spawn_session_id = ?
                       ORDER BY created_at ASC LIMIT 1""",
                    (session_id,),
                ).fetchone()
                if parent_row and parent_row["orchestrator_session_id"]:
                    family_root = parent_row["orchestrator_session_id"]
                    result["parent"] = {
                        "session_id": family_root,
                        "title": _session_title(cur, family_root),
                    }

                # ── 2. The family's spawned helpers (siblings of THIS session
                #       when it is a spawn; own children when it is the root) ──
                spawn_rows = cur.execute(
                    """SELECT id, spawn_session_id, name, task, status, result_summary, created_at
                       FROM agent_spawns
                       WHERE orchestrator_session_id = ?
                       ORDER BY created_at ASC""",
                    (family_root,),
                ).fetchall()
                result["spawns"] = [
                    {
                        "id": r["id"],
                        "spawn_session_id": r["spawn_session_id"],
                        "name": r["name"],
                        "task": r["task"],
                        "status": r["status"],
                        "result_summary": r["result_summary"],
                        "created_at": r["created_at"],
                    }
                    for r in spawn_rows
                ]

                # The "Main" tab target. Only meaningful once the family has at
                # least one spawn; the frontend hides it otherwise.
                if result["spawns"]:
                    result["orchestrator"] = {
                        "session_id": family_root,
                        "title": _session_title(cur, family_root),
                    }
                    result["group_kind"] = "orchestrator"
                    result["root_label"] = "Main"
                    # Unified children list (label=None → frontend defaults to
                    # "Spawn N"); same shape the optimizer family produces below.
                    result["children"] = [
                        {
                            "session_id": s["spawn_session_id"],
                            "label": None,
                            "role": "spawn",
                            "name": s["name"],
                            "status": s["status"],
                        }
                        for s in result["spawns"]
                    ]
        except Exception:
            pass

        # ── 1c. Optimizer family (BASE session = root; Planner + Closer = tabs) ──
        # The real session the optimizer ran ON is the family root ("Main"); its
        # Planner (``optimizer-*``, linked by metadata.target_session → base) and
        # Closer (``closer-*``, linked by metadata.source_optimizer_session →
        # Planner) are surfaced as spawn-style member tabs beneath it — exactly
        # like an orchestrator's spawns. Worker trials are NOT surfaced (they are
        # ephemeral sims, deleted after the run). Resolves the base session from
        # whichever member we're viewing (the base session itself, a Planner, or
        # a Closer). This APPENDS to any spawn family already found above, so a
        # session that is BOTH an orchestrator AND an optimizer base shows both
        # its spawns and its Planner/Closer members in one tab strip.
        if True:
            try:
                base_sid = None
                if session_id.startswith("optimizer-"):
                    base_sid = _session_meta_get(cur, session_id, "target_session")
                elif session_id.startswith("closer-"):
                    _planner = _session_meta_get(cur, session_id, "source_optimizer_session")
                    if _planner:
                        base_sid = _session_meta_get(cur, _planner, "target_session")
                else:
                    # Possibly a base session — confirmed below iff a Planner
                    # actually targets it.
                    base_sid = session_id

                if base_sid:
                    children = []
                    planner_sids = set()
                    for pr in cur.execute(
                        "SELECT id, title, metadata FROM sessions WHERE id LIKE 'optimizer-%' "
                        "ORDER BY created_at ASC"
                    ).fetchall():
                        try:
                            pm = json.loads(pr["metadata"] or "{}")
                        except (json.JSONDecodeError, TypeError):
                            pm = {}
                        if pm.get("target_session") == base_sid:
                            planner_sids.add(pr["id"])
                            children.append({
                                "session_id": pr["id"],
                                "label": "Planner",
                                "role": "planner",
                                "name": pr["title"] or "Planner",
                                "status": None,
                            })
                    for cr in cur.execute(
                        "SELECT id, title, metadata FROM sessions WHERE id LIKE 'closer-%' "
                        "ORDER BY created_at ASC"
                    ).fetchall():
                        try:
                            cm = json.loads(cr["metadata"] or "{}")
                        except (json.JSONDecodeError, TypeError):
                            cm = {}
                        if cm.get("source_optimizer_session") in planner_sids:
                            children.append({
                                "session_id": cr["id"],
                                "label": "Closer",
                                "role": "closer",
                                "name": cr["title"] or "Closer",
                                "status": None,
                            })

                    # Surface the family only when it actually has optimizer
                    # members hanging off this base session. Append to (not
                    # replace) any spawn family found above; only fill in the
                    # root/group fields if the spawn branch didn't already.
                    if children:
                        result["children"] = (result.get("children") or []) + children
                        result["root_label"] = result.get("root_label") or "Main"
                        if not result.get("group_kind"):
                            result["group_kind"] = "optimizer"
                        if not result.get("orchestrator"):
                            result["orchestrator"] = {
                                "session_id": base_sid,
                                "title": _session_title(cur, base_sid),
                            }
                        if session_id != base_sid and not result.get("parent"):
                            result["parent"] = {
                                "session_id": base_sid,
                                "title": _session_title(cur, base_sid),
                            }
            except Exception:
                pass

        # ── 2. Resolve the owning agent for this session ──
        agent_id = None
        try:
            agent_row = cur.execute(
                "SELECT agent_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if agent_row:
                agent_id = agent_row["agent_id"]
        except Exception:
            pass

        # ── 3. Browser sessions linked to the same agent ──
        # Gate: only surface the agent's browser tab(s) if the agent ACTUALLY
        # drove the browser in THIS chat session. A browser_sessions row exists
        # as soon as the user opens the Web tab for an agent (browser_stream
        # auto-creates one via resolve_agent_session), so the row alone is not
        # proof of use. The reliable per-session signal is a browser_action tool
        # execution recorded against this session_id in logs.db.
        used_browser = False
        try:
            from app.db.logs_store import get_log_store
            _bx = await get_log_store().query_tool_executions(
                tool_name="browser_action", session_id=session_id, limit=1
            )
            used_browser = bool(_bx)
        except Exception:
            used_browser = False

        if agent_id and used_browser:
            try:
                bs_rows = cur.execute(
                    """SELECT id, title, url, status, shared, created_at
                       FROM browser_sessions
                       WHERE user_id = ? AND agent_id = ?
                       ORDER BY position ASC, created_at ASC""",
                    (user_id, agent_id),
                ).fetchall()
                result["browser_sessions"] = [
                    {
                        "id": r["id"],
                        "title": r["title"],
                        "url": r["url"],
                        "status": r["status"],
                        "shared": bool(r["shared"]),
                        "created_at": r["created_at"],
                    }
                    for r in bs_rows
                ]
            except Exception:
                pass

        conn.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return result


def _strip_folded_attachments(content: str) -> str:
    """Remove image-description blocks folded into a stored user message.

    The attachment-describe step (app/api/chat.py _maybe_describe_images) appends
    a "[Attached image - ...]: <description>" block to the persisted USER turn so a
    blind model keeps the description across turns. That belongs in the model's
    context, not the user's chat bubble — on reload it made the whole description
    render *inside* the user's message. The description is shown instead via the
    persisted process_image tool row, so we strip the fold for DISPLAY only here.
    The model path (fetch_interactions -> interactions_to_openai_messages) reads
    the raw row and is unaffected. Marker matched on its ASCII prefix (the next
    char is an em dash) so the original typed message before it is returned as-is.
    """
    if not content:
        return content
    idx = content.find("\n\n[Attached image ")
    return content[:idx].rstrip() if idx != -1 else content


def _user_row_attachments(input_raw, att_by_id):
    """Resolve the attachment records referenced by a stored user turn.

    The message -> attachment link lives in the user row's input JSON
    (`attachment_ids`); the bytes live in the session's attachments table. Returns
    frontend-shaped records (matching renderAttachmentElement / _resolveAttachmentUrl)
    so a reloaded user bubble can re-render its pasted images/files — the live
    bubble renders them from the send flow, but a cold reload has only the row.
    Reads the raw input column directly so it still works in light mode (where the
    serialized message's `input` is blanked).
    """
    if not input_raw or not att_by_id:
        return []
    try:
        ids = (json.loads(input_raw) or {}).get("attachment_ids") or []
    except Exception:
        return []
    return [att_by_id[i] for i in ids if i in att_by_id]


@router.get("/session-messages")
async def get_session_messages(
    request: Request,
    session_id: str = Query(..., description="Session ID"),
    limit: int = Query(20, description="Max messages to return (per edge when `around_id` is set)"),
    before_id: Optional[str] = Query(None, description="If set, return the batch of messages immediately older than this message (backward pagination)"),
    after_id: Optional[str] = Query(None, description="If set, return the batch of messages immediately newer than this message (forward pagination)"),
    around_id: Optional[str] = Query(None, description="If set, return a window centred on this message: up to `limit` older rows plus up to `limit` newer rows. Used to reopen a session on the user's saved scroll position without downloading the whole tail."),
    light: int = Query(0, description="When 1, blank the heavy tool-call bodies (per-turn LLM input, tool results, tool-call arguments) while keeping the wire shape. Tool-call headings (names + durations) still render; the full bodies load on demand via /session-turn-detail when the user expands a panel."),
    db: str = Query("local.db", description="Database filename"),
):
    """Return a window of a session's messages, oldest-first.

    Three open modes, by cursor:
      • none → the NEWEST `limit` rows (a long session opens on its latest
        messages, not its oldest).
      • `around_id` → a window CENTRED on that message (`limit` older + `limit`
        newer). This is the fast "reopen where I left off" path.
      • `before_id` / `after_id` → the batch immediately older / newer than that
        message (infinite scroll up / down).

    `has_more` reports whether still-older rows remain; `has_newer` whether
    still-newer rows remain (relevant after an `around_id`/`before_id` open).
    Within a batch, rows are ordered oldest-first for top-to-bottom rendering.
    `max_session_seq` is the session's true latest seq (so the live reconcile
    poll doesn't backfill the gap when opened mid-history); `context_tokens` is
    a whole-session token estimate for the ctx indicator (the windowed payload
    alone would under-report it).
    """
    # Route to temp DB if session has one
    resolved_db = _resolve_session_db(session_id, db)
    if resolved_db != db:
        db = resolved_db
    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        cur = conn.cursor()
        # Same-second tie-breaker for stable pagination: SQLite has an implicit
        # monotonic `rowid`; Postgres doesn't, so fall back to the (uuid) `id`.
        _tb = "rowid" if _dialect == "sqlite" else "id"

        # Participant check: requesting user must own or be a participant in the session.
        # Decode JWT directly — BaseHTTPMiddleware state doesn't reliably propagate to handlers.
        _token = ""
        _auth_header = request.headers.get("Authorization", "")
        if _auth_header.startswith("Bearer "):
            _token = _auth_header[7:]
        if not _token:
            _token = request.query_params.get("token", "")
        _payload = decode_token(_token) if _token else None
        requesting_user_id = _payload.get("user_id") if _payload else None
        requesting_username = _payload.get("sub") if _payload else None
        requester_identities = {v for v in (requesting_user_id, requesting_username) if v}
        # The session's recorded execution mode (Ask/Plan/Auto) — the chat panel
        # applies this on load so the pill matches what the server will actually
        # do, even on a cold device. None when never set (UI defaults to Ask).
        _session_exec_mode = None
        try:
            cur.execute(
                "SELECT user_id, participants, metadata FROM sessions WHERE id = ?",
                (session_id,)
            )
            session_row = cur.fetchone()
            if session_row:
                owner_id = session_row[0]
                participants_raw = session_row[1] or "[]"
                try:
                    _smeta = json.loads(session_row[2]) if session_row[2] else {}
                    if isinstance(_smeta, dict):
                        _mv = _smeta.get("execution_mode")
                        if isinstance(_mv, str) and _mv:
                            _session_exec_mode = _mv
                except (json.JSONDecodeError, TypeError):
                    pass
                try:
                    participants = json.loads(participants_raw)
                except (json.JSONDecodeError, TypeError):
                    participants = []
                participant_ids = {p.get("id") for p in participants if isinstance(p, dict)}
                is_authorized = bool(requester_identities) and bool(
                    requester_identities & ({owner_id} | participant_ids)
                )
                if not is_authorized and not _is_open_access_mode():
                    conn.close()
                    return {"messages": [], "session_id": session_id, "db": db, "restricted": True}
        except Exception:
            pass  # No sessions table — fall through to message fetch

        messages = []
        # Resolve this session's attachments once so a reloaded user turn can
        # re-render its pasted images/files (the live bubble gets them from the
        # send flow; a cold reload has only the stored row). Shaped to match the
        # frontend attachment object (renderAttachmentElement/_resolveAttachmentUrl).
        att_by_id = {}
        try:
            cur.execute(
                "SELECT id, original_name, mime_type, size_bytes, storage_path, storage_provider "
                "FROM attachments WHERE session_id = ?",
                (session_id,),
            )
            for _a in cur.fetchall():
                att_by_id[_a[0]] = {
                    "attachment_id": _a[0],
                    "original_name": _a[1],
                    "mime_type": _a[2],
                    "size_bytes": _a[3],
                    "storage_path": _a[4],
                    "storage_provider": _a[5],
                }
        except Exception:
            att_by_id = {}
        # role-by-id (filled once `rows` is fetched below) lets _row_to_msg tell a
        # SYNTHETIC standalone tool row — vision (process_image/route_attachment
        # parented to the user turn) or a loop-node memory row (search/save, tagged
        # metadata.brain) — both written with no assistant tool_call to pair with —
        # from a real model-issued one, so the reload renderer can show each as its
        # own foldable tool call.
        role_by_id = {}
        has_more = False
        has_newer = False
        # Pagination cursors. created_at alone is NOT a safe cursor —
        # insert_interaction relies on the second-resolution datetime('now')
        # default, so a whole turn's rows usually share the same created_at. We
        # therefore order and page by the composite (created_at, rowid): rowid is
        # the table's monotonic insertion counter, which breaks same-second ties
        # in true chronological order.
        def _cursor_for(mid):
            for _tbl in ("interactions", "messages"):
                try:
                    cur.execute(f'SELECT created_at, {_tb} FROM "{_tbl}" WHERE id = ?', (mid,))
                    _r = cur.fetchone()
                    if _r:
                        return _r[0], _r[1]
                except Exception:
                    pass
            return None, None

        before_ts = before_rowid = None
        after_ts = after_rowid = None
        around_ts = around_rowid = None
        if before_id:
            before_ts, before_rowid = _cursor_for(before_id)
        if after_id:
            after_ts, after_rowid = _cursor_for(after_id)
        if around_id:
            around_ts, around_rowid = _cursor_for(around_id)

        lim = limit or 20
        # Fetch one extra row per edge so we can detect remaining rows without a
        # second existence query.
        fetch_n = lim + 1

        # Light mode blanks the heavy bodies while keeping the wire shape, so the
        # renderer is unchanged but the transcript opens on a tiny payload. The
        # per-turn LLM input (the whole message history — the dominant cost) is
        # dropped; tool result bodies are blanked; the assistant's output is
        # slimmed to just the tool-call NAMES (so the "N tool calls" heading and
        # each row's name + duration still render). Full bodies load on demand
        # when the user expands a panel — see /session-turn-detail.
        def _is_brain_row(meta_str):
            # Loop-node memory rows (the pre-turn search / post-turn save) carry
            # metadata.brain=True. That distinguishes them from a model-issued
            # memory_search the agent calls as a normal tool (which pairs with an
            # assistant tool_call and renders the ordinary way). A SKIPPED search
            # (greeting/command — nothing actually happened) is left untagged so it
            # doesn't render an empty bubble; the live path skips it too (no
            # tool_result fires), so both paths stay consistent.
            try:
                meta = json.loads(meta_str) if meta_str else {}
                return bool(meta.get("brain")) and not meta.get("skipped")
            except Exception:
                return False

        def _slim(m):
            # Synthetic standalone tool rows (vision ingestion + loop-node memory)
            # ship their (small) body even in light mode: their foldable panel is
            # rendered straight from the row on reload (no assistant pairing to
            # lazy-fetch against), so blanking it would leave an empty panel when
            # expanded.
            if m.get("_synth_tool"):
                return m
            m["input"] = None
            role = m.get("role")
            if role == "assistant":
                out = m.get("output")
                if out:
                    try:
                        _o = json.loads(out)
                        _tcs = _o.get("tool_calls") or []
                        _slim_tcs = [
                            {"function": {"name": (tc.get("function") or {}).get("name"), "arguments": ""}}
                            for tc in _tcs if isinstance(tc, dict)
                        ]
                        m["output"] = json.dumps({"tool_calls": _slim_tcs}) if _slim_tcs else None
                    except Exception:
                        m["output"] = None
            elif role == "tool":
                # Tool result bodies are the heavy part; the heading needs only
                # the (kept) metadata for the duration.
                m["content"] = ""
                m["output"] = None
            return m

        def _row_to_msg(row):
            m = {
                "id": row[0],
                "session_id": row[1],
                "role": row[2],
                "content": row[3],
                "tool_name": row[4],
                "created_at": row[5],
                "status": row[6],
                "session_seq": row[7],
                "output": row[8],
                "metadata": row[9],
                "parent_id": row[10],
                "input": row[11],
            }
            # Hide any image-description block folded into the user turn — it is
            # shown as a process_image tool row, not inside the user's bubble.
            if m.get("role") == "user":
                m["content"] = _strip_folded_attachments(m.get("content"))
                # Re-attach the turn's pasted images/files so they survive reload.
                _atts = _user_row_attachments(row[11], att_by_id)
                if _atts:
                    m["attachments"] = _atts
            elif m.get("role") == "tool" and m.get("tool_name") in ("process_image", "route_attachment", "app_control") \
                    and role_by_id.get(row[10]) == "user":
                # Image-processing AND App Control rows are parented to the USER turn
                # and have no assistant tool_call to pair with, so the normal renderer
                # (which matches results to an assistant's saved tool_calls) skips
                # them. Mark them so the reload path renders each as its own foldable
                # tool call (and so _slim keeps the body intact in light mode).
                m["_synth_tool"] = True
            elif m.get("role") == "tool" and m.get("tool_name") in ("memory_search", "memory_save") \
                    and _is_brain_row(row[9]):
                # Loop-node memory rows (brain search before the turn, save after)
                # are written WITHOUT an assistant tool_call to pair against —
                # exactly the vision case — so the normal renderer skips them too.
                # Flag them to render as their own foldable tool bubble on reload.
                m["_synth_tool"] = True
            return _slim(m) if light else m

        # Try interactions table first (has richer data)
        try:
            # `status` may not exist on very old DBs — probe and fall back.
            _has_status = False
            try:
                _icols = {r[1] for r in cur.execute("PRAGMA table_info(interactions)").fetchall()}
                _has_status = "status" in _icols
            except Exception:
                _has_status = False
            _status_col = "status" if _has_status else "'complete' AS status"
            _base = (
                f'SELECT id, session_id, role, content, tool_name, created_at, {_status_col}, '
                f'session_seq, output, metadata, parent_id, input FROM interactions'
            )

            if around_ts is not None:
                # Window centred on the anchor: anchor-and-older (newest-first)
                # plus strictly-newer (oldest-first); each edge reports whether
                # more rows remain beyond it.
                cur.execute(
                    _base + f' WHERE session_id = ? AND (created_at < ? OR (created_at = ? AND {_tb} <= ?)) '
                    f'ORDER BY created_at DESC, {_tb} DESC LIMIT ?',
                    (session_id, around_ts, around_ts, around_rowid, fetch_n),
                )
                older = cur.fetchall()
                has_more = len(older) > lim
                older = older[:lim]
                cur.execute(
                    _base + f' WHERE session_id = ? AND (created_at > ? OR (created_at = ? AND {_tb} > ?)) '
                    f'ORDER BY created_at ASC, {_tb} ASC LIMIT ?',
                    (session_id, around_ts, around_ts, around_rowid, fetch_n),
                )
                newer = cur.fetchall()
                has_newer = len(newer) > lim
                newer = newer[:lim]
                rows = list(reversed(older)) + list(newer)  # oldest-first
            elif after_ts is not None:
                cur.execute(
                    _base + f' WHERE session_id = ? AND (created_at > ? OR (created_at = ? AND {_tb} > ?)) '
                    f'ORDER BY created_at ASC, {_tb} ASC LIMIT ?',
                    (session_id, after_ts, after_ts, after_rowid, fetch_n),
                )
                rows = cur.fetchall()  # oldest-first
                has_newer = len(rows) > lim
                rows = rows[:lim]
                has_more = True  # paged forward from a point → older rows exist
            else:
                _where = "session_id = ?"
                _params: list = [session_id]
                if before_ts is not None:
                    _where += f" AND (created_at < ? OR (created_at = ? AND {_tb} < ?))"
                    _params.extend([before_ts, before_ts, before_rowid])
                cur.execute(
                    _base + f' WHERE {_where} ORDER BY created_at DESC, {_tb} DESC LIMIT ?',
                    (*_params, fetch_n),
                )
                rows = cur.fetchall()  # newest-first
                has_more = len(rows) > lim
                rows = rows[:lim]
                rows.reverse()  # oldest-first
                has_newer = before_ts is not None

            # Map id -> role for this window so _row_to_msg can classify a synthetic
            # vision tool row by its parent turn's role.
            for _r in rows:
                role_by_id[_r[0]] = _r[2]
            for row in rows:
                # Hide parallel-racing loser rows (kept only for diagnostics) so the
                # transcript shows one answer per turn, not the winner + 3 losers.
                if _is_loser_row(row[9]):
                    continue
                messages.append(_row_to_msg(row))
        except Exception:
            pass

        if not messages:
            # Fallback to messages table (legacy DBs). Only newest/before paging;
            # an anchor/forward cursor degrades to newest.
            try:
                _where = "session_id = ?"
                _params = [session_id]
                if before_ts is not None and around_ts is None and after_ts is None:
                    _where += f" AND (created_at < ? OR (created_at = ? AND {_tb} < ?))"
                    _params.extend([before_ts, before_ts, before_rowid])
                cur.execute(
                    f'SELECT id, session_id, role, content, created_at '
                    f'FROM messages WHERE {_where} ORDER BY created_at DESC, {_tb} DESC LIMIT ?',
                    (*_params, fetch_n)
                )
                _rows = cur.fetchall()
                has_more = len(_rows) > lim
                _rows = _rows[:lim]
                _rows.reverse()
                for row in _rows:
                    m = {
                        "id": row[0],
                        "session_id": row[1],
                        "role": row[2],
                        "content": row[3],
                        "created_at": row[4],
                    }
                    if light and m.get("role") == "tool":
                        m["content"] = ""
                    messages.append(m)
            except Exception:
                pass

        # ── Durable run-state: is a turn in progress for this session? ──
        # Lets a cold/second device know to show the live indicator and where to
        # resume the WebSocket stream from, even after a server restart.
        run_info = None
        try:
            cur.execute(
                "SELECT status, turn_id, assistant_interaction_id, latest_session_seq, updated_at, current_op "
                "FROM session_runs WHERE session_id = ?",
                (session_id,)
            )
            r = cur.fetchone()
            if r:
                run_info = {
                    "status": r[0],
                    "active": r[0] == "running",
                    "turn_id": r[1],
                    "assistant_interaction_id": r[2],
                    "latest_session_seq": r[3],
                    "updated_at": r[4],
                    "current_op": r[5],
                }
        except Exception:
            pass  # session_runs table not present (legacy/temp DB)

        # The session's true latest seq — the live reconcile poll seeds its
        # cursor here so opening mid-history (an `around_id` window) doesn't make
        # it backfill every newer row; the gap is reached by scrolling down.
        max_session_seq = 0
        try:
            cur.execute("SELECT MAX(session_seq) FROM interactions WHERE session_id = ?", (session_id,))
            _r = cur.fetchone()
            if _r and _r[0] is not None:
                max_session_seq = _r[0]
        except Exception:
            pass

        # Whole-session token estimate for the ctx indicator (the windowed
        # payload alone would under-report it once the open fetch is small).
        context_tokens = 0
        try:
            cur.execute(
                "SELECT COALESCE(SUM(LENGTH(content)), 0) FROM interactions "
                "WHERE session_id = ? AND role IN ('user', 'assistant')",
                (session_id,),
            )
            _r = cur.fetchone()
            if _r and _r[0]:
                context_tokens = int(_r[0]) // 4
        except Exception:
            pass

        conn.close()
        return {
            "messages": messages,
            "session_id": session_id,
            "db": db,
            "run": run_info,
            "has_more": has_more,
            "has_newer": has_newer,
            "light": bool(light),
            "max_session_seq": max_session_seq,
            "context_tokens": context_tokens,
            "execution_mode": _session_exec_mode,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session-turn-detail")
async def get_session_turn_detail(
    request: Request,
    session_id: str = Query(..., description="Session ID (for the participant check)"),
    ids: str = Query(..., description="Comma-separated assistant interaction ids to expand"),
    db: str = Query("local.db", description="Database filename"),
):
    """Full tool-call bodies for specific assistant turns, loaded on demand.

    The chat transcript opens in ``light`` mode (tool-call bodies blanked — see
    /session-messages). When the user expands a tool-call panel, the frontend
    calls this for just the assistant ids in that panel and gets back, per id,
    the full LLM ``input``/``output``/``metadata`` plus the child tool rows'
    ``content``/``output``/``metadata`` to populate the body. This keeps the
    rarely-viewed heavy payload off the initial load entirely.
    """
    resolved_db = _resolve_session_db(session_id, db)
    if resolved_db != db:
        db = resolved_db
    try:
        conn, _dialect = _open(db)
        cur = conn.cursor()
        _tb = "rowid" if _dialect == "sqlite" else "id"

        # Participant gate — identical to /session-messages.
        _token = ""
        _auth_header = request.headers.get("Authorization", "")
        if _auth_header.startswith("Bearer "):
            _token = _auth_header[7:]
        if not _token:
            _token = request.query_params.get("token", "")
        _payload = decode_token(_token) if _token else None
        requesting_user_id = _payload.get("user_id") if _payload else None
        requesting_username = _payload.get("sub") if _payload else None
        requester_identities = {v for v in (requesting_user_id, requesting_username) if v}
        try:
            cur.execute("SELECT user_id, participants FROM sessions WHERE id = ?", (session_id,))
            session_row = cur.fetchone()
            if session_row:
                owner_id = session_row[0]
                try:
                    participants = json.loads(session_row[1] or "[]")
                except (json.JSONDecodeError, TypeError):
                    participants = []
                participant_ids = {p.get("id") for p in participants if isinstance(p, dict)}
                is_authorized = bool(requester_identities) and bool(
                    requester_identities & ({owner_id} | participant_ids)
                )
                if not is_authorized and not _is_open_access_mode():
                    conn.close()
                    return {"details": {}, "session_id": session_id, "db": db, "restricted": True}
        except Exception:
            pass  # No sessions table — fall through

        id_list = [s for s in (ids or "").split(",") if s]
        details = {}
        for aid in id_list:
            try:
                cur.execute(
                    "SELECT input, output, metadata FROM interactions WHERE id = ? AND session_id = ?",
                    (aid, session_id),
                )
                r = cur.fetchone()
                if not r:
                    continue
                tools = []
                try:
                    cur.execute(
                        "SELECT tool_name, content, output, metadata FROM interactions "
                        f"WHERE parent_id = ? AND session_id = ? ORDER BY created_at ASC, {_tb} ASC",
                        (aid, session_id),
                    )
                    for tr in cur.fetchall():
                        tools.append({
                            "tool_name": tr[0],
                            "content": tr[1],
                            "output": tr[2],
                            "metadata": tr[3],
                        })
                except Exception:
                    pass
                details[aid] = {"input": r[0], "output": r[1], "metadata": r[2], "tools": tools}
            except Exception:
                continue

        conn.close()
        return {"details": details, "session_id": session_id, "db": db}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session-tail")
async def get_session_tail(
    request: Request,
    session_id: str = Query(..., description="Session ID"),
    after_session_seq: int = Query(0, description="Return only interactions with session_seq greater than this"),
    user_id: Optional[str] = Query(None, description="Active client identity — fallback when no JWT (local users)"),
    db: str = Query("local.db", description="Database filename"),
):
    """Incremental tail of a session's interactions for the live DB-reconcile path.

    Returns only interactions whose ``session_seq > after_session_seq`` (ascending),
    PLUS the in-progress streaming assistant row, PLUS the durable ``run`` object.

    This is the cheap poll the chat UI runs (gated on WebSocket silence) to stream
    a reply when the live WebSocket and the agent run are on DIFFERENT server
    processes/workers — the DB is the shared source of truth, so this path is
    correct regardless of process topology. The active streaming row is unioned
    in explicitly because its ``session_seq`` is assigned once at insert and does
    NOT advance as its content is updated every ~0.6s — a ``session_seq > after``
    filter alone would return it once and then miss its GROWING text. Mirrors the
    row shape of ``/session-messages`` so the frontend uses one renderer.
    """
    resolved_db = _resolve_session_db(session_id, db)
    if resolved_db != db:
        db = resolved_db
    try:
        conn, _dialect = _open(db)
        cur = conn.cursor()

        # Same participant gate as /session-messages (False = deny; None/True = proceed).
        if _session_access_ok(cur, session_id, request, user_id) is False:
            conn.close()
            return {"messages": [], "session_id": session_id, "db": db, "run": None, "restricted": True}

        # `status` may not exist on very old DBs — probe and fall back.
        _has_status = False
        try:
            _icols = {r[1] for r in cur.execute("PRAGMA table_info(interactions)").fetchall()}
            _has_status = "status" in _icols
        except Exception:
            _has_status = False
        _status_col = "status" if _has_status else "'complete' AS status"
        _cols = (f'id, session_id, role, content, tool_name, created_at, {_status_col}, '
                 f'session_seq, output, metadata, parent_id, input')

        def _row_to_msg(row):
            return {
                "id": row[0], "session_id": row[1], "role": row[2], "content": row[3],
                "tool_name": row[4], "created_at": row[5], "status": row[6],
                "session_seq": row[7], "output": row[8], "metadata": row[9],
                "parent_id": row[10], "input": row[11],
            }

        messages = []
        seen_ids = set()
        try:
            cur.execute(
                f'SELECT {_cols} FROM interactions '
                f'WHERE session_id = ? AND session_seq IS NOT NULL AND session_seq > ? '
                f'ORDER BY session_seq ASC LIMIT 200',
                (session_id, after_session_seq),
            )
            for row in cur.fetchall():
                if _is_loser_row(row[9]):
                    continue
                m = _row_to_msg(row)
                messages.append(m)
                seen_ids.add(m["id"])
        except Exception:
            pass

        # Durable run-state: is a turn in progress for this session?
        run_info = None
        try:
            cur.execute(
                "SELECT status, turn_id, assistant_interaction_id, latest_session_seq, updated_at, current_op "
                "FROM session_runs WHERE session_id = ?",
                (session_id,),
            )
            r = cur.fetchone()
            if r:
                run_info = {
                    "status": r[0],
                    "active": r[0] == "running",
                    "turn_id": r[1],
                    "assistant_interaction_id": r[2],
                    "latest_session_seq": r[3],
                    "updated_at": r[4],
                    "current_op": r[5],
                }
        except Exception:
            pass  # session_runs table not present (legacy/temp DB)

        # Union the in-progress streaming row (see docstring) so its growing text
        # rides every poll even though its session_seq never advances.
        _asst = run_info.get("assistant_interaction_id") if run_info else None
        if _asst and _asst not in seen_ids:
            try:
                cur.execute(f'SELECT {_cols} FROM interactions WHERE id = ?', (_asst,))
                row = cur.fetchone()
                if row:
                    messages.append(_row_to_msg(row))
            except Exception:
                pass

        conn.close()
        return {"messages": messages, "session_id": session_id, "db": db, "run": run_info}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _session_access_ok(cur, session_id: str, request: Request, user_id: Optional[str] = None):
    """Return True if the requester owns / participates in the session, False if
    they clearly don't, or None when there's no session row to check against
    (legacy / temp DB) — in which case the caller should fall through, exactly
    like get_session_messages does. Mirrors that endpoint's auth logic.

    ``user_id`` is the active client identity (``app.currentUserId``) passed as a
    query param. It's accepted as a fallback identity when there's no matching
    JWT — exactly like get_user_sessions does — so unauthenticated local UUID
    users (and sessions created from the TUI / launcher under ``admin``)
    can manage their own rows. The session row's owner / participant list is
    still the gate: a claimed ``user_id`` only authorizes when it actually
    matches the session, so it can't widen access to other users' sessions."""
    _token = ""
    _auth_header = request.headers.get("Authorization", "")
    if _auth_header.startswith("Bearer "):
        _token = _auth_header[7:]
    if not _token:
        _token = request.query_params.get("token", "")
    _payload = decode_token(_token) if _token else None
    requesting_user_id = _payload.get("user_id") if _payload else None
    requesting_username = _payload.get("sub") if _payload else None
    requester_identities = {v for v in (requesting_user_id, requesting_username, user_id) if v}
    try:
        cur.execute(
            "SELECT user_id, participants FROM sessions WHERE id = ?",
            (session_id,),
        )
        session_row = cur.fetchone()
    except Exception:
        return None  # no sessions table — fall through
    if not session_row:
        return None  # unknown / anonymous session — fall through
    owner_id = session_row[0]
    participants_raw = session_row[1] or "[]"
    try:
        participants = json.loads(participants_raw)
    except (json.JSONDecodeError, TypeError):
        participants = []
    participant_ids = {p.get("id") for p in participants if isinstance(p, dict)}
    if _is_open_access_mode():
        return True  # open mode: middleware already trusts the request as admin
    return bool(requester_identities) and bool(
        requester_identities & ({owner_id} | participant_ids)
    )


@router.delete("/turn")
async def delete_turn(
    request: Request,
    session_id: str = Query(..., description="Session ID"),
    interaction_id: str = Query(..., description="Any interaction id within the turn to delete"),
    user_id: Optional[str] = Query(None, description="Active client identity — fallback when no JWT (local users)"),
    db: str = Query("local.db", description="Database filename"),
):
    """Delete one whole conversation turn from `interactions`.

    A "turn" is the **parent-chain closure rooted at the user message** that
    started it: the user row (``parent_id IS NULL``) plus every assistant step,
    tool call and memory write that descended from it. This is robust against
    the fact that turns interleave in wall-clock time — a background memory_save
    or an interrupting user message can land another turn's row in the middle —
    so a naive "delete everything between two user messages" would corrupt
    neighbouring turns. Walking the parent tree keeps the cut surgical.

    ``interaction_id`` may be any row in the turn (the clicked bubble's id); the
    server walks up to the root itself. Removing the rows strips that turn from
    the history the agent rebuilds each turn — i.e. it prunes the context."""
    resolved_db = _resolve_session_db(session_id, db)
    db_path = _get_db_path(resolved_db)
    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        access = _session_access_ok(cur, session_id, request, user_id)
        if access is False:
            conn.close()
            raise HTTPException(status_code=403, detail="Not a participant in this session")

        rows = cur.execute(
            "SELECT id, parent_id, turn_id FROM interactions WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        children: dict = {}
        for r in rows:
            children.setdefault(r["parent_id"], []).append(r["id"])

        # ── Resolve the turn root: walk parent_id up to the user message. ──
        # User rows have parent_id = NULL; every other row in the turn descends
        # from one. A visited set guards against any accidental cycle.
        cur_id = interaction_id
        visited = set()
        while (cur_id in by_id and by_id[cur_id]["parent_id"]
               and by_id[cur_id]["parent_id"] in by_id and cur_id not in visited):
            visited.add(cur_id)
            cur_id = by_id[cur_id]["parent_id"]
        root = cur_id

        # ── Collect the whole descendant tree from the root (BFS/DFS). ──
        to_delete = set()
        stack = [root]
        while stack:
            n = stack.pop()
            if n in to_delete:
                continue
            to_delete.add(n)
            stack.extend(children.get(n, []))

        # Belt-and-suspenders: also any row explicitly tagged with this turn_id
        # (future-proofs the day assistant/tool rows start carrying turn_id).
        for r in rows:
            if r["turn_id"] and r["turn_id"] == root:
                to_delete.add(r["id"])

        # Only ever delete ids that are real rows in THIS session, so a stale or
        # foreign interaction_id can never widen the blast radius.
        to_delete = {i for i in to_delete if i in by_id}
        if not to_delete:
            conn.close()
            return {"deleted_ids": [], "count": 0, "turn_root": root, "session_id": session_id}

        cur.executemany(
            "DELETE FROM interactions WHERE id = ?",
            [(i,) for i in to_delete],
        )
        conn.commit()
        conn.close()
        return {
            "deleted_ids": list(to_delete),
            "count": len(to_delete),
            "turn_root": root,
            "session_id": session_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session-stats")
async def session_stats(
    request: Request,
    user_id: str = Query(..., description="User ID"),
    db: str = Query("local.db", description="Database filename"),
    status: str = Query("active", description="Filter by session status: 'active', 'recycled', or 'all'"),
):
    """
    Return aggregated usage stats per session for a user.

    Parses interactions.metadata JSON to extract:
      - input_tokens, output_tokens (from assistant roles)
      - duration_ms (from assistant roles — LLM call wall time)
      - cost (from assistant roles, when available)
      - turn count, message count, last active
    """
    # Authorization: a caller may only read their OWN session stats. Open mode is
    # single-user / local full-trust (mirrors _session_access_ok), so skip the
    # check there; otherwise require the JWT (Authorization header or ?token=) to
    # match the requested user_id — admins may read any user. Closes the
    # previously-unauthenticated cross-tenant leak of another user's session
    # list, token usage and cost.
    if not _is_open_access_mode():
        from app.auth.identity import assert_caller_is
        await assert_caller_is(request, user_id)

    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Fetch sessions for user — optionally filtered by status
        try:
            _status_filter = ""
            _params = [user_id]
            if status == "active":
                _status_filter = " AND (status IS NULL OR status = 'active')"
            elif status == "recycled":
                _status_filter = " AND status = 'recycled'"
            cur.execute(
                'SELECT id, title, created_at, status FROM sessions WHERE user_id = ?' + _status_filter + ' ORDER BY updated_at DESC NULLS LAST',
                _params
            )
            session_rows = cur.fetchall()
        except Exception:
            session_rows = []

        sessions_map = {
            r["id"]: {"title": r["title"] or r["id"][:12], "created_at": r["created_at"]}
            for r in session_rows
        }

        # If no sessions table rows, fall back to distinct session_ids from the
        # interactions log so an orphaned transcript (no `sessions` row) is still
        # shown and can be cleared. These "phantom" sessions have no recycled
        # state — so only surface them in the active/all views, never in the bin
        # (otherwise the same orphans would show in both, since this fallback has
        # no status to filter on). Deleting one hard-removes its interactions
        # (see delete_session), so it truly disappears rather than reappearing.
        if not sessions_map and status != "recycled":
            # Skip any session that DOES have a row marked recycled — e.g. a real
            # session that lives under a different user_id (so the query above
            # found nothing for THIS user) but was already recycled. Without this,
            # recycling such a session wouldn't stick: the fallback re-derives it
            # straight from the interactions log on every reload.
            recycled_ids: set = set()
            try:
                for r in cur.execute("SELECT id FROM sessions WHERE status = 'recycled'"):
                    recycled_ids.add(r[0])
            except Exception:
                pass
            try:
                cur.execute(
                    'SELECT DISTINCT session_id FROM interactions ORDER BY created_at DESC'
                )
                for row in cur.fetchall():
                    sid = row[0]
                    if sid and sid not in sessions_map and sid not in recycled_ids:
                        sessions_map[sid] = {"title": sid[:12], "created_at": None}
            except Exception:
                pass

        if not sessions_map:
            conn.close()
            return {"sessions": [], "db": db}

        session_ids = list(sessions_map.keys())

        # Which sessions touched a canvas or a web browser page? Derived from the
        # tool_executions log (logs.db) — a session is "linked" to a canvas if it
        # ran any canvas build/edit tool, and to a browser page if it drove the
        # live browser. No FK on the canvas/browser rows is needed for this.
        _CANVAS_TOOLS = [
            "render_visual", "edit_canvas", "create_canvas",
            "set_canvas_data", "rename_canvas", "screenshot_canvas",
        ]
        _BROWSER_TOOLS = ["browser_action"]
        canvas_sessions: set = set()
        browser_sessions_used: set = set()
        try:
            from app.db.logs_store import get_log_store
            # No user_id filter: tool_executions rows often carry a NULL user_id
            # (the loop's tool events don't stamp it). Correctness instead comes
            # from only flagging sessions that are in this user's session map.
            usage = await get_log_store().session_tool_usage(
                _CANVAS_TOOLS + _BROWSER_TOOLS
            )
            _canvas_set = set(_CANVAS_TOOLS)
            _browser_set = set(_BROWSER_TOOLS)
            for sid_used, tools in usage.items():
                if tools & _canvas_set:
                    canvas_sessions.add(sid_used)
                if tools & _browser_set:
                    browser_sessions_used.add(sid_used)
        except Exception:
            pass

        # ── Family grouping (mirror /sessions + the chat session list) ──────
        # Tag every session so the Sessions table can nest children under their
        # parent as an expandable tree, identical in shape to the chat session
        # list. A session is a parent if it is an orchestrator (spawned helpers
        # in agent_spawns) or an optimizer Planner (a closer-* whose metadata
        # points back to it); a session is a child if it is one of those
        # spawn-* / closer-* rows. Workers ('trial-*') live in throwaway temp
        # DBs and never reach local.db, so they're naturally excluded.
        child_counts: dict = {}
        parent_of: dict = {}     # child sid -> parent sid
        child_role: dict = {}    # child sid -> 'spawn' | 'planner' | 'closer'
        try:
            for r in cur.execute(
                "SELECT orchestrator_session_id AS p, spawn_session_id AS c FROM agent_spawns"
            ).fetchall():
                _p, _c = r["p"], r["c"]
                if _p and _c:
                    child_counts[_p] = child_counts.get(_p, 0) + 1
                    parent_of[_c] = _p
                    child_role[_c] = "spawn"
        except Exception:
            pass
        # Optimizer Planners are children of the BASE session they ran on
        # (metadata.target_session). Build planner→base for the closers below.
        _planner_base: dict = {}
        try:
            for r in cur.execute(
                "SELECT id, metadata FROM sessions WHERE id LIKE 'optimizer-%'"
            ).fetchall():
                try:
                    _pm = json.loads(r["metadata"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    _pm = {}
                _base = _pm.get("target_session")
                if _base:
                    _planner_base[r["id"]] = _base
                    child_counts[_base] = child_counts.get(_base, 0) + 1
                    parent_of[r["id"]] = _base
                    child_role[r["id"]] = "planner"
        except Exception:
            pass
        # Optimizer Closers are also children of the base session, via Planner.
        try:
            for r in cur.execute(
                "SELECT id, metadata FROM sessions WHERE id LIKE 'closer-%'"
            ).fetchall():
                try:
                    _cm = json.loads(r["metadata"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    _cm = {}
                _base = _planner_base.get(_cm.get("source_optimizer_session"))
                if _base:
                    child_counts[_base] = child_counts.get(_base, 0) + 1
                    parent_of[r["id"]] = _base
                    child_role[r["id"]] = "closer"
        except Exception:
            pass

        # ── Authoritative per-session cost (usage_events) ───────────────────
        # The interactions.metadata 'cost' field is only populated on some rows,
        # so most sessions would otherwise show an em-dash. usage_events stores
        # the locked-in per-call cost_usd (published price × tokens) for every
        # LLM call and is the single source of truth for spend (see the
        # /session-cost endpoint). Sum it once, grouped by session, and overlay
        # it below — preferring it over the sparse interactions cost.
        cost_by_session: dict = {}
        try:
            for r in cur.execute(
                "SELECT session_id, COALESCE(SUM(cost_usd),0) AS c "
                "FROM usage_events WHERE user_id = ? GROUP BY session_id",
                (user_id,),
            ).fetchall():
                _sid, _c = r["session_id"], r["c"]
                if _sid is not None:
                    cost_by_session[_sid] = float(_c or 0)
        except Exception:
            pass

        # Build stats per session
        results = []
        for sid in session_ids:
            try:
                cur.execute(
                    'SELECT role, metadata, created_at FROM interactions WHERE session_id = ? ORDER BY created_at ASC',
                    (sid,)
                )
                rows = cur.fetchall()
            except Exception:
                rows = []

            total_input_tokens = 0
            total_output_tokens = 0
            total_duration_ms = 0
            total_cost = 0.0
            turn_count = 0
            message_count = 0
            has_cost = False
            last_active = None

            for row in rows:
                message_count += 1
                ts = row["created_at"]
                if ts and (last_active is None or ts > last_active):
                    last_active = ts

                raw_meta = row["metadata"]
                if not raw_meta:
                    continue
                try:
                    meta = json.loads(raw_meta)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(meta, dict):
                    continue

                role = row["role"] or ""

                if role == "assistant":
                    in_tok = meta.get("input_tokens")
                    out_tok = meta.get("output_tokens")
                    dur = meta.get("duration_ms")
                    cost_val = meta.get("cost")
                    turn = meta.get("turn")

                    if in_tok is not None:
                        total_input_tokens += int(in_tok)
                    if out_tok is not None:
                        total_output_tokens += int(out_tok)
                    if dur is not None:
                        total_duration_ms += int(dur)
                    if cost_val is not None:
                        total_cost += float(cost_val)
                        has_cost = True
                    if turn is not None:
                        # Use max turn value for this session
                        turn_count = max(turn_count, int(turn) + 1)
                    else:
                        # Count assistant messages as turns
                        turn_count += 1

            # Count user+assistant turns that form "loops"
            total_tokens = total_input_tokens + total_output_tokens

            # Prefer the authoritative usage_events cost; fall back to the
            # interactions-derived cost only when there's no usage_events row.
            ue_cost = cost_by_session.get(sid)
            if ue_cost is not None and ue_cost > 0:
                resolved_cost = round(ue_cost, 6)
            elif has_cost:
                resolved_cost = round(total_cost, 6)
            else:
                resolved_cost = None

            entry = {
                "session_id": sid,
                "title": sessions_map[sid]["title"],
                "created_at": sessions_map[sid]["created_at"],
                "last_active": last_active,
                "message_count": message_count,
                "turn_count": turn_count,
                "total_input_tokens": total_input_tokens,
                "total_output_tokens": total_output_tokens,
                "total_tokens": total_tokens,
                "total_duration_ms": total_duration_ms,
                "total_cost": resolved_cost,
                "has_canvas": sid in canvas_sessions,
                "has_browser": sid in browser_sessions_used,
                "child_count": child_counts.get(sid, 0),
                "parent_session_id": parent_of.get(sid),
                "child_role": child_role.get(sid),
            }
            # Enrich with agent_name and run_status if available
            try:
                cur.execute('SELECT agent_id FROM sessions WHERE id = ?', (sid,))
                srow = cur.fetchone()
                if srow and srow["agent_id"]:
                    entry["agent_id"] = srow["agent_id"]
                    cur2 = conn.cursor()
                    cur2.execute('SELECT name, metadata FROM agents WHERE id = ?', (srow["agent_id"],))
                    arow = cur2.fetchone()
                    entry["agent_name"] = arow["name"] if arow else ""
                    entry["agent_icon"] = ""
                    entry["agent_engine"] = ""
                    if arow and arow["metadata"]:
                        try:
                            meta = json.loads(arow["metadata"])
                            if isinstance(meta, dict):
                                if meta.get("icon"):
                                    entry["agent_icon"] = meta["icon"]
                                if meta.get("engine"):
                                    entry["agent_engine"] = meta["engine"]
                        except Exception:
                            pass
                else:
                    entry["agent_id"] = None
                    entry["agent_name"] = ""

                # Run status
                try:
                    cur2.execute('SELECT status FROM session_runs WHERE session_id = ?', (sid,))
                    rrow = cur2.fetchone()
                    entry["run_status"] = rrow["status"] if rrow else None
                except Exception:
                    entry["run_status"] = None
            except Exception:
                entry["agent_id"] = None
                entry["agent_name"] = ""
                entry["run_status"] = None
            results.append(entry)

        # Sort by last_active descending
        results.sort(key=lambda s: s["last_active"] or "", reverse=True)

        conn.close()
        return {"sessions": results, "db": db}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stream/interactions")
async def stream_interactions(
    since: str = Query("", description="ISO timestamp — return rows with created_at > since"),
    db: str = Query("local.db", description="Database filename"),
    user_id: str = Query("", description="Filter by user_id (optional)"),
    session_id: str = Query("", description="Filter by session_id (optional)"),
):
    """Return interactions. Used by Stream tab and Loop visualizer."""
    # If session has a temp_db_path, route to that DB transparently
    if session_id:
        resolved_db = _resolve_session_db(session_id, db)
        if resolved_db != db:
            db = resolved_db
    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='interactions'")
        if not cur.fetchone():
            conn.close()
            return {"interactions": [], "db": db}

        where_parts = []
        params = []

        if session_id:
            # Direct session filter — most specific
            where_parts.append("session_id = ?")
            params.append(session_id)
        elif user_id:
            # User filter via sessions table
            where_parts.append("session_id IN (SELECT id FROM sessions WHERE user_id = ?)")
            params.append(user_id)

        if since:
            where_parts.append("created_at > ?")
            params.append(since)

        where_clause = " AND ".join(where_parts) if where_parts else "1=1"
        limit_clause = "" if since else "LIMIT 200"

        cur.execute(
            f'SELECT id, session_id, role, content, tool_name, metadata, input, output, created_at '
            f'FROM interactions WHERE {where_clause} ORDER BY created_at ASC {limit_clause}',
            params
        )

        rows = [dict(row) for row in cur.fetchall()]
        conn.close()
        return {"interactions": rows, "db": db}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class UpdateRowRequest(BaseModel):
    """Request body for updating a row."""
    db: str = "local.db"
    table: str
    # Column-value pairs to identify the row (typically PK columns)
    where: dict[str, object]
    # Column-value pairs to update
    values: dict[str, object]


@router.put("/update")
async def update_row(
    req: UpdateRowRequest,
    _auth: dict = Depends(require_admin),
):
    """Update a row in a table. Admin-only."""
    db_path = _get_db_path(req.db)
    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Verify table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (req.table,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"Table '{req.table}' not found")

        # Get columns
        cur.execute(f'PRAGMA table_info("{req.table}")')
        columns = {col[1] for col in cur.fetchall()}

        # Validate columns exist
        for col in list(req.where.keys()) + list(req.values.keys()):
            if col not in columns:
                raise HTTPException(status_code=400, detail=f"Column '{col}' not found in table '{req.table}'")

        if not req.where:
            raise HTTPException(status_code=400, detail="'where' clause cannot be empty")

        # Build UPDATE
        set_clause = ", ".join(f'"{col}" = ?' for col in req.values)
        where_clause = " AND ".join(f'"{col}" = ?' for col in req.where)
        params = list(req.values.values()) + list(req.where.values())

        query = f'UPDATE "{req.table}" SET {set_clause} WHERE {where_clause}'
        cur.execute(query, params)
        conn.commit()
        affected = cur.rowcount
        conn.close()

        logger.info(f"Updated {affected} row(s) in {req.table}")
        return {"affected": affected, "success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class DeleteRowRequest(BaseModel):
    """Request body for deleting a row."""
    db: str = "local.db"
    table: str
    # Column-value pairs to identify the row (typically PK columns)
    where: dict[str, object]


@router.delete("/row")
async def delete_row(
    req: DeleteRowRequest,
    _auth: dict = Depends(require_admin),
):
    """Delete a single row from a table, identified by the `where` clause. Admin-only."""
    db_path = _get_db_path(req.db)
    try:
        conn, _dialect = _open(db)
        cur = conn.cursor()

        # Verify table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (req.table,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"Table '{req.table}' not found")

        # Get columns
        cur.execute(f'PRAGMA table_info("{req.table}")')
        columns = {col[1] for col in cur.fetchall()}

        # Validate columns exist
        for col in req.where.keys():
            if col not in columns:
                raise HTTPException(status_code=400, detail=f"Column '{col}' not found in table '{req.table}'")

        if not req.where:
            raise HTTPException(status_code=400, detail="'where' clause cannot be empty")

        where_clause = " AND ".join(
            f'"{col}" IS NULL' if val is None else f'"{col}" = ?'
            for col, val in req.where.items()
        )
        params = [v for v in req.where.values() if v is not None]

        query = f'DELETE FROM "{req.table}" WHERE {where_clause}'
        cur.execute(query, params)
        conn.commit()
        affected = cur.rowcount
        conn.close()

        logger.info(f"Deleted {affected} row(s) from {req.table}")
        return {"affected": affected, "success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/reset")
async def reset_database(
    db: str = Query("local.db", description="Database filename"),
    exclude: list[str] = Query(default=["agent_templates", "agent_prompts", "auth_elements"], description="List of tables to exclude from reset"),
    _auth: dict = Depends(require_admin),
):
    """Delete ALL rows from ALL tables. Skips excluded tables. Admin-only."""
    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        cur = conn.cursor()

        # Get all user tables (not internal sqlite ones)
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' ORDER BY name")
        all_tables = [row[0] for row in cur.fetchall()]

        # Preserve template/protected tables
        # If a request sends ?exclude= (empty list via query param), it gets parsed as [""]
        # So we filter out empty strings to allow unchecking ALL tables
        exclude_set = set(e for e in exclude if e)
        to_truncate = [t for t in all_tables if t not in exclude_set]

        results = {}
        for table in to_truncate:
            cur.execute(f'SELECT COUNT(*) FROM "{table}"')
            before = cur.fetchone()[0]
            if before > 0:
                cur.execute(f'DELETE FROM "{table}"')
                results[table] = before

        conn.commit()
        conn.close()

        logger.info(f"Database reset: {len(results)} tables truncated ({sum(results.values())} rows total)")
        return {"success": True, "tables_truncated": results, "total_rows_deleted": sum(results.values()), "db": db}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/truncate")
async def truncate_table(
    table: str = Query(..., description="Table name to truncate"),
    db: str = Query("local.db", description="Database filename"),
    _auth: dict = Depends(require_admin),
):
    """Delete ALL rows from a table. Admin-only."""
    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        cur = conn.cursor()

        # Verify table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"Table '{table}' not found")

        # Count before delete
        cur.execute(f'SELECT COUNT(*) FROM "{table}"')
        before = cur.fetchone()[0]

        # Delete all rows
        cur.execute(f'DELETE FROM "{table}"')
        conn.commit()
        conn.close()

        logger.info(f"Truncated table '{table}': {before} rows deleted")
        return {"success": True, "table": table, "deleted": before, "db": db}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/column-values")
async def column_values(
    table: str = Query(..., description="Table name"),
    column: str = Query(..., description="Column name"),
    db: str = Query("local.db", description="Database filename"),
    search: str = Query("", description="Search term to filter distinct values"),
    _auth: dict = Depends(require_db_auth),
):
    """Get distinct values for a column (for filter popup)."""
    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Verify table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"Table '{table}' not found")

        # Verify column exists
        cur.execute(f"PRAGMA table_info(\"{table}\")")
        columns = [col[1] for col in cur.fetchall()]
        if column not in columns:
            raise HTTPException(status_code=404, detail=f"Column '{column}' not found in table '{table}'")

        # Restrict to the caller's rows (admins see everything).
        user_id, is_admin = _get_caller(_auth)
        acl_clause, acl_params = _acl_clause(table, user_id, is_admin)

        where_parts = []
        params: list = []
        if acl_clause:
            where_parts.append(acl_clause)
            params.extend(acl_params)
        if search:
            where_parts.append(f'CAST("{column}" AS TEXT) LIKE ?')
            params.append(f"%{search}%")

        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        query = f'SELECT DISTINCT "{column}" FROM "{table}"{where_sql} ORDER BY "{column}" ASC'
        cur.execute(query, params)
        values = [row[0] for row in cur.fetchall()]

        # Total distinct count, scoped to the caller's rows.
        count_where = (" WHERE " + acl_clause) if acl_clause else ""
        cur.execute(
            f'SELECT COUNT(DISTINCT "{column}") FROM "{table}"{count_where}',
            acl_params if acl_clause else [],
        )
        total = cur.fetchone()[0]

        conn.close()
        return {
            "table": table,
            "column": column,
            "values": values,
            "total": total,
            "db": db,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/query")
async def query_table(
    table: str = Query(..., description="Table name"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    order_by: Optional[str] = Query(None, description="Column to order by"),
    order_dir: str = Query("ASC", pattern="^(ASC|DESC)$"),
    filter_col: Optional[str] = Query(None, description="Column to filter on"),
    filter_op: str = Query("contains", pattern="^(contains|equals|starts|gt|lt|not_in)$"),
    filter_val: Optional[str] = Query(None, description="Filter value (comma-separated for not_in)"),
    filters_json: Optional[str] = Query(None, description="JSON array of {col, op, val} for multi-column filters"),
    db: str = Query("local.db", description="Database filename"),
    with_count: bool = Query(True, description="When false, skip SELECT COUNT(*) (total will be -1). Used by silent auto-refresh."),
    _auth: dict = Depends(require_db_auth),
):
    """Query rows from a table."""
    db_path = _get_db_path(db)
    try:
        conn, _dialect = _open(db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Verify table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"Table '{table}' not found")

        # Get columns
        cur.execute(f"PRAGMA table_info(\"{table}\")")
        columns = [col[1] for col in cur.fetchall()]

        # Collect filter specs
        filter_specs = []

        # Parse multi-column filters from JSON
        if filters_json:
            try:
                parsed = json.loads(filters_json)
                if isinstance(parsed, list):
                    for spec in parsed:
                        if isinstance(spec, dict) and spec.get("col") in columns:
                            filter_specs.append({
                                "col": spec["col"],
                                "op": spec.get("op", "contains"),
                                "val": spec.get("val", ""),
                                "include_null": spec.get("include_null", False)
                            })
            except (json.JSONDecodeError, TypeError):
                pass

        # Legacy single-filter support
        if not filter_specs and filter_col and filter_col in columns and filter_val:
            filter_specs.append({"col": filter_col, "op": filter_op, "val": filter_val})

        # Build WHERE clause. ACL goes first so it always applies, regardless
        # of any user-supplied filters.
        where_clauses = []
        where_params = []
        user_id, is_admin = _get_caller(_auth)
        acl_clause, acl_params = _acl_clause(table, user_id, is_admin)
        if acl_clause:
            where_clauses.append(acl_clause)
            where_params.extend(acl_params)
        for spec in filter_specs:
            col = spec["col"]
            op = spec["op"]
            val = spec["val"]
            if op == "not_in":
                vals = [v.strip() for v in val.split(",") if v.strip()]
                include_null = spec.get("include_null", False)
                if vals:
                    if vals == ["__ALL__"]:
                        where_clauses.append("1=0")
                    else:
                        placeholders = ",".join("?" for _ in vals)
                        clause = f'"{col}" NOT IN ({placeholders})'
                        if include_null:
                            clause = f'({clause} OR "{col}" IS NULL)'
                        where_clauses.append(clause)
                        where_params.extend(vals)
                else:
                    if not include_null:
                        where_clauses.append(f'"{col}" IS NOT NULL')
            elif op == "contains":
                where_clauses.append(f'"{col}" LIKE ?')
                where_params.append(f"%{val}%")
            elif op == "equals":
                where_clauses.append(f'"{col}" = ?')
                where_params.append(val)
            elif op == "starts":
                where_clauses.append(f'"{col}" LIKE ?')
                where_params.append(f"{val}%")
            elif op == "gt":
                where_clauses.append(f'"{col}" > ?')
                where_params.append(val)
            elif op == "lt":
                where_clauses.append(f'"{col}" < ?')
                where_params.append(val)

        where_clause = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        # Build query
        query = f'SELECT * FROM "{table}"{where_clause}'
        if order_by and order_by in columns:
            query += f' ORDER BY "{order_by}" {order_dir}'
        elif "created_at" in columns:
            query += ' ORDER BY "created_at" DESC'
        query += f" LIMIT {limit} OFFSET {offset}"

        cur.execute(query, where_params)
        rows = [dict(row) for row in cur.fetchall()]

        # Skip COUNT(*) on silent refreshes — the client keeps the last total.
        if with_count:
            cur.execute(f'SELECT COUNT(*) FROM "{table}"{where_clause}', where_params)
            total = cur.fetchone()[0]
        else:
            total = -1

        conn.close()
        return {
            "table": table,
            "columns": columns,
            "rows": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "db": db,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/download")
async def download_db(
    db: str = Query("local.db", description="Database filename"),
    _auth=Depends(require_db_auth),
):
    """Download the SQLite database file."""
    db_path = _get_db_path(db)
    return FileResponse(
        path=str(db_path),
        filename=db,
        media_type="application/octet-stream",
    )


@router.get("/list")
async def list_databases(
    _auth=Depends(require_db_auth),
):
    """List all .db files in the db directory."""
    files = sorted([
        f.name for f in _DB_FILES_DIR.iterdir()
        if f.suffix == ".db"
    ])
    return {"databases": files}


@router.delete("/file")
async def delete_database_file(
    db: str = Query(..., description="Database filename to delete"),
    _auth=Depends(require_admin),
):
    """Delete a .db file (plus sidecar -wal/-shm) from the db directory. Admin-only.

    Refuses to delete local.db (primary app database).
    """
    # Reject path-traversal / non-plain names
    if Path(db).name != db:
        raise HTTPException(status_code=400, detail="Database name must be a plain filename")
    if not db.endswith(".db"):
        raise HTTPException(status_code=400, detail="Only .db files may be deleted")
    if db == "local.db":
        raise HTTPException(status_code=400, detail="Refusing to delete local.db")

    db_path = _DB_FILES_DIR / db
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"Database '{db}' not found")

    removed = []
    errors = {}
    for suffix in ("", "-wal", "-shm"):
        target = _DB_FILES_DIR / (db + suffix)
        if target.exists():
            try:
                target.unlink()
                removed.append(target.name)
            except OSError as e:
                errors[target.name] = str(e)

    if errors and not removed:
        raise HTTPException(status_code=500, detail=f"Failed to delete: {errors}")

    logger.info(f"Deleted database files: {removed} (errors: {errors})")
    return {"success": True, "deleted": removed, "errors": errors}




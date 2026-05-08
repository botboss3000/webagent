"""
Database viewer API — local SQLite introspection for the terminal UI.
"""

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException, Query
from app.auth.db_auth import require_db_auth

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/db", tags=["db_viewer"])

# SQLite files for this API live under app/db/ (same directory as local.py)
_DB_FILES_DIR = Path(__file__).resolve().parent.parent / "db"


def _get_db_path(name: str = "local.db") -> Path:
    if Path(name).name != name:
        raise HTTPException(status_code=400, detail="Database name must be a plain filename")
    db_path = _DB_FILES_DIR / name
    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"Database '{name}' not found at {db_path}")
    return db_path


@router.get("/tables")
async def list_tables(
    db: str = Query("local.db", description="Database filename"),
    _auth: dict = Depends(require_db_auth),
):
    """List all tables in the database."""
    db_path = _get_db_path(db)
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
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
            # Get row count
            cur.execute(f'SELECT COUNT(*) FROM "{name}"')
            count = cur.fetchone()[0]
            tables.append({"name": name, "columns": columns, "row_count": count})
        conn.close()
        return {"tables": tables, "db": db}
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/users")
async def list_users(db: str = Query("local.db", description="Database filename")):
    """List distinct user IDs from the database."""
    db_path = _get_db_path(db)
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        users = set()
        for tbl in ["sessions", "interactions", "messages"]:
            try:
                cur.execute(f'SELECT DISTINCT user_id FROM "{tbl}" WHERE user_id IS NOT NULL')
                for row in cur.fetchall():
                    users.add(row[0])
            except sqlite3.OperationalError:
                pass
        conn.close()
        return {"users": sorted(users), "db": db}
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    db: str = Query("local.db", description="Database filename"),
):
    """Delete a session and all its interactions/messages."""
    db_path = _get_db_path(db)
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()

        # Delete interactions for this session
        cur.execute('DELETE FROM interactions WHERE session_id = ?', (session_id,))
        interactions_deleted = cur.rowcount

        # Delete session summary
        cur.execute('DELETE FROM session_summaries WHERE session_id = ?', (session_id,))

        # Delete pipeline events
        try:
            cur.execute('DELETE FROM pipeline_events WHERE session_id = ?', (session_id,))
        except sqlite3.OperationalError:
            pass

        # Delete the session itself
        cur.execute('DELETE FROM sessions WHERE id = ?', (session_id,))
        session_deleted = cur.rowcount

        conn.commit()
        conn.close()

        logger.info(f"Deleted session {session_id[:12]}: {session_deleted} session, {interactions_deleted} interactions")
        return {
            "success": True,
            "session_id": session_id,
            "session_deleted": session_deleted,
            "interactions_deleted": interactions_deleted,
        }
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sessions")
async def list_sessions(
    user_id: str = Query(..., description="User ID"),
    db: str = Query("local.db", description="Database filename"),
):
    """List sessions for a user."""
    db_path = _get_db_path(db)
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Try sessions table first, fall back to distinct session_ids from interactions/messages
        sessions = []
        try:
            cur.execute('SELECT id, title, created_at FROM sessions WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
            for row in cur.fetchall():
                sessions.append({"id": row[0], "title": row[1] or row[0][:12], "created_at": row[2]})
        except sqlite3.OperationalError:
            pass

        if not sessions:
            # Fallback: get distinct session_ids from interactions
            for tbl in ["interactions", "messages"]:
                try:
                    cur.execute(f'SELECT DISTINCT session_id FROM "{tbl}" WHERE user_id = ? AND session_id IS NOT NULL ORDER BY created_at DESC', (user_id,))
                    for row in cur.fetchall():
                        if row[0] not in {s["id"] for s in sessions}:
                            sessions.append({"id": row[0], "title": row[0][:12], "created_at": None})
                except sqlite3.OperationalError:
                    pass

        conn.close()
        return {"sessions": sessions, "db": db}
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session-messages")
async def get_session_messages(
    session_id: str = Query(..., description="Session ID"),
    db: str = Query("local.db", description="Database filename"),
):
    """Get all messages for a session, ordered by created_at ASC."""
    db_path = _get_db_path(db)
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        messages = []
        # Try interactions table first (has richer data)
        try:
            cur.execute(
                'SELECT id, session_id, role, content, tool_name, created_at '
                'FROM interactions WHERE session_id = ? ORDER BY created_at ASC',
                (session_id,)
            )
            for row in cur.fetchall():
                messages.append({
                    "id": row[0],
                    "session_id": row[1],
                    "role": row[2],
                    "content": row[3],
                    "created_at": row[5],
                })
        except sqlite3.OperationalError:
            pass

        if not messages:
            # Fallback to messages table
            try:
                cur.execute(
                    'SELECT id, session_id, role, content, created_at '
                    'FROM messages WHERE session_id = ? ORDER BY created_at ASC',
                    (session_id,)
                )
                for row in cur.fetchall():
                    messages.append({
                        "id": row[0],
                        "session_id": row[1],
                        "role": row[2],
                        "content": row[3],
                        "created_at": row[4],
                    })
            except sqlite3.OperationalError:
                pass

        conn.close()
        return {"messages": messages, "session_id": session_id, "db": db}
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stream/interactions")
async def stream_interactions(
    since: str = Query("", description="ISO timestamp — return rows with created_at > since"),
    db: str = Query("local.db", description="Database filename"),
    user_id: str = Query("", description="Filter by user_id (optional)"),
    session_id: str = Query("", description="Filter by session_id (optional)"),
):
    """Return interactions. Used by Stream tab and Loop visualizer."""
    db_path = _get_db_path(db)
    try:
        conn = sqlite3.connect(str(db_path))
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
            f'SELECT id, session_id, role, content, tool_name, metadata, input, created_at '
            f'FROM interactions WHERE {where_clause} ORDER BY created_at ASC {limit_clause}',
            params
        )

        rows = [dict(row) for row in cur.fetchall()]
        conn.close()
        return {"interactions": rows, "db": db}
    except sqlite3.Error as e:
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
    _auth: dict = Depends(require_db_auth),
):
    """Update a row in a table."""
    db_path = _get_db_path(req.db)
    try:
        conn = sqlite3.connect(str(db_path))
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
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/reset")
async def reset_database(
    db: str = Query("local.db", description="Database filename"),
    _auth: dict = Depends(require_db_auth),
):
    """Delete ALL rows from ALL tables. Preserves context_templates and agent_templates."""
    db_path = _get_db_path(db)
    try:
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()

        # Get all user tables (not internal sqlite ones)
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' ORDER BY name")
        all_tables = [row[0] for row in cur.fetchall()]

        # Preserve template/protected tables
        exclude = {"context_templates", "agent_templates"}
        to_truncate = [t for t in all_tables if t not in exclude]

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
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/truncate")
async def truncate_table(
    table: str = Query(..., description="Table name to truncate"),
    db: str = Query("local.db", description="Database filename"),
    _auth: dict = Depends(require_db_auth),
):
    """Delete ALL rows from a table."""
    db_path = _get_db_path(db)
    try:
        conn = sqlite3.connect(str(db_path))
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
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/query")
async def query_table(
    table: str = Query(..., description="Table name"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    order_by: Optional[str] = Query(None, description="Column to order by"),
    order_dir: str = Query("ASC", regex="^(ASC|DESC)$"),
    filter_col: Optional[str] = Query(None, description="Column to filter on"),
    filter_op: str = Query("contains", regex="^(contains|equals|starts|gt|lt)$"),
    filter_val: Optional[str] = Query(None, description="Filter value"),
    db: str = Query("local.db", description="Database filename"),
    _auth: dict = Depends(require_db_auth),
):
    """Query rows from a table."""
    db_path = _get_db_path(db)
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Verify table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail=f"Table '{table}' not found")

        # Get columns
        cur.execute(f"PRAGMA table_info(\"{table}\")")
        columns = [col[1] for col in cur.fetchall()]

        # Build WHERE clause
        where_clause = ""
        where_params = []
        if filter_col and filter_col in columns and filter_val:
            if filter_op == "contains":
                where_clause = f' WHERE "{filter_col}" LIKE ?'
                where_params = [f"%{filter_val}%"]
            elif filter_op == "equals":
                where_clause = f' WHERE "{filter_col}" = ?'
                where_params = [filter_val]
            elif filter_op == "starts":
                where_clause = f' WHERE "{filter_col}" LIKE ?'
                where_params = [f"{filter_val}%"]
            elif filter_op == "gt":
                where_clause = f' WHERE "{filter_col}" > ?'
                where_params = [filter_val]
            elif filter_op == "lt":
                where_clause = f' WHERE "{filter_col}" < ?'
                where_params = [filter_val]

        # Build query
        query = f'SELECT * FROM "{table}"{where_clause}'
        if order_by and order_by in columns:
            query += f' ORDER BY "{order_by}" {order_dir}'
        query += f" LIMIT {limit} OFFSET {offset}"

        cur.execute(query, where_params)
        rows = [dict(row) for row in cur.fetchall()]

        # Get total count (with same filter)
        cur.execute(f'SELECT COUNT(*) FROM "{table}"{where_clause}', where_params)
        total = cur.fetchone()[0]

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
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=str(e))

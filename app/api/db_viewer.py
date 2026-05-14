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
from fastapi.responses import FileResponse
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


def _resolve_session_db(session_id: str, fallback_db: str = "local.db") -> str:
    """If a session has temp_db_path in local.db metadata, route to that temp DB.
    Otherwise return fallback_db."""
    try:
        conn = sqlite3.connect(str(_DB_FILES_DIR / "local.db"))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT metadata FROM sessions WHERE id=?", (session_id,)).fetchone()
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


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    db: str = Query("local.db", description="Database filename"),
    _auth: dict = Depends(require_db_auth),
):
    """Delete all sessions, interactions, and messages for a user."""
    db_path = _get_db_path(db)
    try:
        conn = sqlite3.connect(str(db_path))
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
            except sqlite3.OperationalError:
                pass
            try:
                cur.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            except sqlite3.OperationalError:
                pass

        # Delete sessions
        cur.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        deleted["sessions"] = cur.rowcount

        # Delete session summaries
        try:
            cur.execute("DELETE FROM session_summaries WHERE user_id = ?", (user_id,))
            deleted["summaries"] = cur.rowcount
        except sqlite3.OperationalError:
            pass

        # Delete attachments
        try:
            cur.execute("DELETE FROM attachments WHERE user_id = ?", (user_id,))
            deleted["attachments"] = cur.rowcount
        except sqlite3.OperationalError:
            pass

        conn.commit()
        conn.close()
        logger.info(f"Deleted user {user_id[:12]}: {deleted}")
        return {"success": True, "user_id": user_id, "deleted": deleted}
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
    # Route to temp DB if session has one
    resolved_db = _resolve_session_db(session_id, db)
    if resolved_db != db:
        db = resolved_db
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


@router.get("/session-stats")
async def session_stats(
    user_id: str = Query(..., description="User ID"),
    db: str = Query("local.db", description="Database filename"),
):
    """
    Return aggregated usage stats per session for a user.

    Parses interactions.metadata JSON to extract:
      - input_tokens, output_tokens (from assistant roles)
      - duration_ms (from assistant roles — LLM call wall time)
      - cost (from assistant roles, when available)
      - turn count, message count, last active
    """
    db_path = _get_db_path(db)
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Fetch sessions for user
        try:
            cur.execute(
                'SELECT id, title, created_at FROM sessions WHERE user_id = ? ORDER BY created_at DESC',
                (user_id,)
            )
            session_rows = cur.fetchall()
        except sqlite3.OperationalError:
            session_rows = []

        sessions_map = {
            r["id"]: {"title": r["title"] or r["id"][:12], "created_at": r["created_at"]}
            for r in session_rows
        }

        # If no sessions table rows, fall back to distinct session_ids from interactions
        if not sessions_map:
            try:
                cur.execute(
                    'SELECT DISTINCT session_id FROM interactions ORDER BY created_at DESC'
                )
                for row in cur.fetchall():
                    sid = row[0]
                    if sid and sid not in sessions_map:
                        sessions_map[sid] = {"title": sid[:12], "created_at": None}
            except sqlite3.OperationalError:
                pass

        if not sessions_map:
            conn.close()
            return {"sessions": [], "db": db}

        session_ids = list(sessions_map.keys())

        # Build stats per session
        results = []
        for sid in session_ids:
            try:
                cur.execute(
                    'SELECT role, metadata, created_at FROM interactions WHERE session_id = ? ORDER BY created_at ASC',
                    (sid,)
                )
                rows = cur.fetchall()
            except sqlite3.OperationalError:
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
                "total_cost": round(total_cost, 6) if has_cost else None,
            }
            results.append(entry)

        # Sort by last_active descending
        results.sort(key=lambda s: s["last_active"] or "", reverse=True)

        conn.close()
        return {"sessions": results, "db": db}
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
    # If session has a temp_db_path, route to that DB transparently
    if session_id:
        resolved_db = _resolve_session_db(session_id, db)
        if resolved_db != db:
            db = resolved_db
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
            f'SELECT id, session_id, role, content, tool_name, metadata, input, output, created_at '
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
    exclude: list[str] = Query(default=["context_templates", "agent_templates", "auth_elements"], description="List of tables to exclude from reset"),
    _auth: dict = Depends(require_db_auth),
):
    """Delete ALL rows from ALL tables. Skips excluded tables."""
    db_path = _get_db_path(db)
    try:
        conn = sqlite3.connect(str(db_path))
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
        conn = sqlite3.connect(str(db_path))
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

        # Build query
        if search:
            query = f'SELECT DISTINCT "{column}" FROM "{table}" WHERE CAST("{column}" AS TEXT) LIKE ? ORDER BY "{column}" ASC'
            cur.execute(query, [f"%{search}%"])
        else:
            query = f'SELECT DISTINCT "{column}" FROM "{table}" ORDER BY "{column}" ASC'
            cur.execute(query)

        values = [row[0] for row in cur.fetchall()]

        # Total distinct count (without search)
        cur.execute(f'SELECT COUNT(DISTINCT "{column}") FROM "{table}"')
        total = cur.fetchone()[0]

        conn.close()
        return {
            "table": table,
            "column": column,
            "values": values,
            "total": total,
            "db": db,
        }
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
    filter_op: str = Query("contains", regex="^(contains|equals|starts|gt|lt|not_in)$"),
    filter_val: Optional[str] = Query(None, description="Filter value (comma-separated for not_in)"),
    filters_json: Optional[str] = Query(None, description="JSON array of {col, op, val} for multi-column filters"),
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

        # Build WHERE clause
        where_clauses = []
        where_params = []
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

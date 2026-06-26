"""Read-only view of the LINKED checkout's app database (managed-mode single source).

In managed mode an ordinary turn is driven by the linked checkout's REAL agent
loop (see :mod:`tui_app.app_engine`), which records the whole turn — user text,
assistant bubbles AND tool steps — in the **app's own** SQLite DB, exactly the
rows the browser renders. To stay truly single-source (and match the browser),
the TUI reads a managed session's visible transcript straight from that DB
instead of mirroring a lossy copy into its own ``webagent.db``.

This module is that reader. It opens the app DB **read-only** (sqlite ``mode=ro``
URI) and never writes, and it imports **no app code** — the TUI never loads the
webApp into its own process (that runs only as the venv subprocess; see
``app_engine.py``). Built-in-brain sessions (onboarding, watchdog, synthetic)
keep living in the local store; only managed sessions come from here.

The dicts returned mirror the shape of ``Store.history`` rows (``role``,
``content``, ``content_kind``, ``tool_name``, ``tool_calls``) so the existing
transcript-rebuild loop renders them with no special-casing. A tool row carries
its input args (lifted from the app's ``metadata.input_params``) in
``tool_calls`` so the call + result render just like the browser.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

# Roles that map to a visible chat bubble / tool block. The app DB may also hold
# rows the chat UI never shows (e.g. system); skip anything else.
_DISPLAY_ROLES = ("user", "assistant", "tool")


def app_db_path(project_root: Path) -> Path:
    """The linked checkout's main SQLite DB (the app's own ``local.db``)."""
    return Path(project_root) / "data" / "db" / "local.db"


def _connect_ro(project_root: Path) -> Optional[sqlite3.Connection]:
    """Open the app DB strictly read-only, or None if it isn't there/openable."""
    path = app_db_path(project_root)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def has_session(project_root: Path, session_id: str) -> bool:
    """True when the app DB holds at least one interaction for ``session_id``.

    This is how the TUI decides a session is *managed* (its transcript lives in
    the app DB) versus built-in (lives in the local store). Survives restarts
    because it reads the DB, not in-memory state.
    """
    conn = _connect_ro(project_root)
    if conn is None:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM interactions WHERE session_id = ? LIMIT 1", (session_id,)
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def transcript(project_root: Path, session_id: str) -> list[dict[str, Any]]:
    """A managed session's visible messages, in ``Store.history`` row shape.

    Ordered as the conversation happened (``created_at`` then ``rowid`` as a
    same-second tiebreaker). Empty assistant placeholder rows are dropped. Returns
    ``[]`` on any error or when the session isn't in the app DB — the caller then
    falls back to the local store.
    """
    conn = _connect_ro(project_root)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT role, content, tool_name, metadata FROM interactions "
            "WHERE session_id = ? ORDER BY created_at, rowid",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for r in rows:
        role = r["role"] or ""
        if role not in _DISPLAY_ROLES:
            continue
        content = r["content"] or ""
        if role == "tool":
            # Lift the call's input args out of the app's tool metadata so the
            # rebuild loop can render the invocation, not just the result.
            args: Any = {}
            try:
                meta = json.loads(r["metadata"] or "{}")
                args = meta.get("input_params") or {}
            except (json.JSONDecodeError, TypeError):
                args = {}
            out.append({
                "role": "tool",
                "content": content,
                "content_kind": "",
                "tool_name": r["tool_name"] or "",
                "tool_calls": json.dumps(args) if args else "",
            })
        else:
            if not content.strip():
                continue  # skip empty assistant placeholders / streaming stubs
            out.append({
                "role": role,
                "content": content,
                "content_kind": "",
                "tool_name": "",
                "tool_calls": "",
            })
    return out

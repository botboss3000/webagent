"""External SQLite store for the webagent TUI.

Deliberately SEPARATE from the web app's ``app/db/local.db`` (see config.db_path)
so the operator keeps its own conversation history + a full audit trail of every
mutating action it takes, surviving any reset of the web app's own database.

Three tables:
* ``sessions``  — one row per conversation.
* ``messages``  — chat + tool transcript (role, content, tool metadata).
* ``actions``   — append-only audit of mutating tool calls (what / when / ok).

Plain stdlib ``sqlite3`` (no async driver dependency). Calls are short; the TUI
runs them via ``asyncio.to_thread`` where they could block the event loop.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    project_dir TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    role        TEXT NOT NULL,            -- system | user | assistant | tool
    content     TEXT NOT NULL DEFAULT '',
    tool_name   TEXT NOT NULL DEFAULT '',
    tool_calls  TEXT NOT NULL DEFAULT '', -- JSON (assistant tool_calls)
    tool_call_id TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
CREATE TABLE IF NOT EXISTS actions (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL DEFAULT '',
    tool_name   TEXT NOT NULL,
    args        TEXT NOT NULL DEFAULT '',
    ok          INTEGER NOT NULL DEFAULT 1,
    detail      TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_actions_session ON actions(session_id, created_at);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # ── sessions ─────────────────────────────────────────────────────────
    def create_session(self, project_dir: str, title: str = "") -> str:
        sid = uuid.uuid4().hex
        now = time.time()
        self._conn.execute(
            "INSERT INTO sessions (id, title, project_dir, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, title, project_dir, now, now),
        )
        self._conn.commit()
        return sid

    def touch_session(self, session_id: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (time.time(), session_id)
        )
        self._conn.commit()

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── messages ─────────────────────────────────────────────────────────
    def _next_seq(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["n"])

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str = "",
        *,
        tool_name: str = "",
        tool_calls: Optional[list] = None,
        tool_call_id: str = "",
    ) -> None:
        self._conn.execute(
            "INSERT INTO messages (id, session_id, seq, role, content, tool_name, "
            "tool_calls, tool_call_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                session_id,
                self._next_seq(session_id),
                role,
                content,
                tool_name,
                json.dumps(tool_calls) if tool_calls else "",
                tool_call_id,
                time.time(),
            ),
        )
        self._conn.commit()

    def history(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY seq", (session_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── audit ────────────────────────────────────────────────────────────
    def log_action(
        self,
        session_id: str,
        tool_name: str,
        args: dict | str,
        ok: bool,
        detail: str = "",
    ) -> None:
        self._conn.execute(
            "INSERT INTO actions (id, session_id, tool_name, args, ok, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                session_id,
                tool_name,
                args if isinstance(args, str) else json.dumps(args)[:4000],
                1 if ok else 0,
                detail[:4000],
                time.time(),
            ),
        )
        self._conn.commit()

    def recent_actions(self, session_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        if session_id:
            rows = self._conn.execute(
                "SELECT * FROM actions WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM actions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

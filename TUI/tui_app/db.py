"""External SQLite store for the webagent TUI.

Deliberately SEPARATE from the web app's ``app/db/local.db`` (see config.db_path)
so the server manager keeps its own conversation history + a full audit trail of every
mutating action it takes, surviving any reset of the web app's own database.

Tables:
* ``sessions``  — one row per conversation.
* ``messages``  — chat + tool transcript (role, content, tool metadata).
* ``actions``   — append-only audit of mutating tool calls (what / when / ok).
* ``settings``  — a small key/value store for manager-level state that must
  survive restarts (e.g. the linked **repo directory** the user/agent designated).
* ``playbook_issues`` / ``playbook_remedies`` / ``playbook_incidents`` — the
  self-healing knowledge base: recognised issues (fingerprints), the remedies
  tried against each (with helped/didn't-help counters), and an append-only log
  of every incident occurrence + its outcome. See ``playbook.py`` for the pure
  decision logic that sits on top of these rows.

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
    content_kind TEXT NOT NULL DEFAULT '', -- '' = plain text | 'json' = structured (text + image refs)
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

-- ── Key/value settings (manager-level state that survives restarts) ───────
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);

-- ── Self-healing Playbook (issue knowledge base) ──────────────────────────
CREATE TABLE IF NOT EXISTS playbook_issues (
    key            TEXT PRIMARY KEY,          -- stable fingerprint
    label          TEXT NOT NULL DEFAULT '',  -- human label
    kind           TEXT NOT NULL DEFAULT '',  -- 'builtin' | 'diagnostic'
    first_seen     REAL NOT NULL,
    last_seen      REAL NOT NULL,
    occurrences    INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'learning',  -- learning | known | muted
    programmed     INTEGER NOT NULL DEFAULT 0,         -- has a trigger been auto-created?
    match_contains TEXT NOT NULL DEFAULT '',  -- diagnostic-only: alarm criteria for programming
    match_level    TEXT NOT NULL DEFAULT '',
    match_category TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS playbook_remedies (
    id               TEXT PRIMARY KEY,
    issue_key        TEXT NOT NULL,
    kind             TEXT NOT NULL,            -- restart_server|clear_port|escalate_to_agent|notify_only|command|note
    payload          TEXT NOT NULL DEFAULT '', -- command text / written instruction
    status           TEXT NOT NULL DEFAULT 'approved',  -- suggested | approved | disabled
    priority         INTEGER NOT NULL DEFAULT 0,
    times_tried      INTEGER NOT NULL DEFAULT 0,
    times_helped     INTEGER NOT NULL DEFAULT 0,
    times_didnt_help INTEGER NOT NULL DEFAULT 0,
    last_outcome     TEXT NOT NULL DEFAULT '',
    last_used        REAL NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pb_remedies_issue ON playbook_remedies(issue_key);
CREATE TABLE IF NOT EXISTS playbook_incidents (
    id           TEXT PRIMARY KEY,
    issue_key    TEXT NOT NULL,
    ts           REAL NOT NULL,
    trigger      TEXT NOT NULL DEFAULT '',     -- JSON snapshot of what fired
    remedy_id    TEXT NOT NULL DEFAULT '',
    outcome      TEXT NOT NULL DEFAULT 'pending',  -- pending|resolved|failed|escalated|documented
    resolved_ts  REAL NOT NULL DEFAULT 0,
    notes        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pb_incidents_issue ON playbook_incidents(issue_key, ts);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a store was first created (older DBs)."""
        msg_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(messages)")}
        if "content_kind" not in msg_cols:
            self._conn.execute(
                "ALTER TABLE messages ADD COLUMN content_kind TEXT NOT NULL DEFAULT ''"
            )
        sess_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(sessions)")}
        if "system_prompt" not in sess_cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN system_prompt TEXT NOT NULL DEFAULT ''"
            )
        # Re-fetch after potential ALTER above
        sess_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(sessions)")}
        if "status" not in sess_cols:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN status TEXT NOT NULL DEFAULT 'idle'"
            )

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # ── sessions ─────────────────────────────────────────────────────────
    @staticmethod
    def _timestamp_id(now: float) -> str:
        """Human-readable, sortable session id derived from the creation time:
        ``YYYYMMDD-HHMMSS-ffffff`` (local time + microseconds). Microsecond
        precision makes a collision between two real session creations effectively
        impossible; ``create_session`` still guards the PRIMARY KEY just in case."""
        lt = time.localtime(now)
        micros = int(round((now - int(now)) * 1_000_000)) % 1_000_000
        return time.strftime("%Y%m%d-%H%M%S", lt) + f"-{micros:06d}"

    def last_session(self, title_filter: str = "", project_dir: str = "") -> str:
        """Return the id of the most-recently-updated session that has at least one
        message.  If ``title_filter`` is given, only sessions whose title equals it
        are considered (e.g. ``title_filter="WATCHDOG"``).  ``project_dir`` is also an
        optional filter.  Returns ``''`` when no matching session is found."""
        clauses = ["TRUE"]
        params: list = []
        if title_filter:
            clauses.append("s.title = ?")
            params.append(title_filter)
        if project_dir:
            clauses.append("s.project_dir = ?")
            params.append(project_dir)
        row = self._conn.execute(
            f"SELECT s.id, COUNT(m.id) AS cnt FROM sessions s "
            f"LEFT JOIN messages m ON m.session_id = s.id "
            f"WHERE {' AND '.join(clauses)} "
            f"GROUP BY s.id HAVING cnt > 0 "
            f"ORDER BY s.updated_at DESC LIMIT 1",
            params,
        ).fetchone()
        return str(row["id"]) if row else ""

    def create_session(self, project_dir: str, title: str = "") -> str:
        now = time.time()
        sid = self._timestamp_id(now)
        # Guard the PRIMARY KEY: on the astronomically rare same-microsecond clash,
        # nudge forward a microsecond at a time until the id is free.
        bump = now
        while self._conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (sid,)
        ).fetchone() is not None:
            bump += 0.000001
            sid = self._timestamp_id(bump)
        self._conn.execute(
            "INSERT INTO sessions (id, title, project_dir, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, title, project_dir, now, now),
        )
        self._conn.commit()
        return sid

    def session_title(self, session_id: str) -> str:
        """Return the stored title for a session, or empty string."""
        row = self._conn.execute(
            "SELECT title FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return str(row["title"]) if row else ""

    def touch_session(self, session_id: str) -> None:
        self._conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (time.time(), session_id)
        )
        self._conn.commit()

    def get_session_prompt(self, session_id: str) -> str:
        """Return the per-session system prompt override, or empty string."""
        row = self._conn.execute(
            "SELECT system_prompt FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return str(row["system_prompt"]) if row else ""

    def set_session_prompt(self, session_id: str, prompt_text: str) -> None:
        """Set the per-session system prompt override. Pass '' to clear it."""
        self._conn.execute(
            "UPDATE sessions SET system_prompt = ? WHERE id = ?",
            (prompt_text.strip(), session_id),
        )
        self._conn.commit()

    def set_session_title(self, session_id: str, title: str) -> None:
        """Update a session's display name (the summary shown in the Sessions list)."""
        self._conn.execute(
            "UPDATE sessions SET title = ? WHERE id = ?",
            (title.strip()[:120], session_id),
        )
        self._conn.commit()

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── session status (busy/idle tracking for crash recovery) ────────────
    def set_session_status(self, session_id: str, status: str) -> None:
        """Mark a session as ``idle`` or ``busy``.  ``busy`` means an agent turn
        is in progress — if the TUI crashes, this flag survives across restarts
        so the manager can detect "unfinished work" and offer to re-activate."""
        self._conn.execute(
            "UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?",
            (status, time.time(), session_id),
        )
        self._conn.commit()

    def busy_sessions(self) -> list[dict[str, Any]]:
        """Return sessions whose status is ``busy`` (agent was mid-turn when the
        TUI last shut down or crashed).  The caller can offer recovery for each."""
        rows = self._conn.execute(
            "SELECT * FROM sessions WHERE status = 'busy' ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── open-tab persistence (crash recovery) ─────────────────────────────
    def save_open_tabs(self, tab_ids: list[str]) -> None:
        """Persist the list of open-tab session ids as a JSON setting so the
        tab bar can be restored after a restart or crash."""
        self.set_setting("open_tabs", json.dumps(tab_ids))

    def load_open_tabs(self) -> list[str]:
        """Read the persisted open-tab session ids.  Returns [] when none were
        saved or they no longer exist in the sessions table."""
        raw = self.get_setting("open_tabs", "")
        if not raw:
            return []
        try:
            ids = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
        # Remove any ids that no longer exist
        existing = {r["id"] for r in self._conn.execute(
            "SELECT id FROM sessions WHERE id IN ({})".format(
                ",".join("?" * len(ids))
            ), ids).fetchall()} if ids else set()
        return [sid for sid in ids if sid in existing]

    # ── messages ─────────────────────────────────────────────────────────
    def _next_seq(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), -1) + 1 AS n FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row["n"])

    def clear_messages(self, session_id: str) -> None:
        """Delete ALL messages for a session, resetting its context."""
        self._conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        self._conn.commit()

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str = "",
        *,
        content_kind: str = "",
        tool_name: str = "",
        tool_calls: Optional[list] = None,
        tool_call_id: str = "",
    ) -> None:
        self._conn.execute(
            "INSERT INTO messages (id, session_id, seq, role, content, content_kind, "
            "tool_name, tool_calls, tool_call_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                session_id,
                self._next_seq(session_id),
                role,
                content,
                content_kind,
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

    # ── key/value settings ───────────────────────────────────────────────
    def get_setting(self, key: str, default: str = "") -> str:
        """Read a saved setting value (e.g. the linked repo directory). Returns
        ``default`` when the key has never been set."""
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        """Insert or update a setting (upsert). Stamps ``updated_at`` each time."""
        self._conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, time.time()),
        )
        self._conn.commit()

    # ── Playbook: issues ─────────────────────────────────────────────────
    def pb_get_issue(self, key: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM playbook_issues WHERE key = ?", (key,)
        ).fetchone()
        return dict(row) if row else None

    def pb_upsert_issue(self, key: str, label: str, kind: str = "builtin",
                        match_contains: str = "", match_level: str = "",
                        match_category: str = "") -> dict[str, Any]:
        """Insert a new issue or bump an existing one (occurrence + last_seen).
        Returns the up-to-date issue row (including the new occurrence count)."""
        now = time.time()
        existing = self.pb_get_issue(key)
        if existing is None:
            self._conn.execute(
                "INSERT INTO playbook_issues (key, label, kind, first_seen, last_seen, "
                "occurrences, status, programmed, match_contains, match_level, match_category) "
                "VALUES (?, ?, ?, ?, ?, 1, 'learning', 0, ?, ?, ?)",
                (key, label, kind, now, now, match_contains, match_level, match_category),
            )
        else:
            self._conn.execute(
                "UPDATE playbook_issues SET last_seen = ?, occurrences = occurrences + 1, "
                "label = COALESCE(NULLIF(?, ''), label) WHERE key = ?",
                (now, label, key),
            )
        self._conn.commit()
        return self.pb_get_issue(key)  # type: ignore[return-value]

    def pb_list_issues(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM playbook_issues ORDER BY last_seen DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def pb_known_keys(self) -> list[str]:
        rows = self._conn.execute("SELECT key FROM playbook_issues").fetchall()
        return [r["key"] for r in rows]

    def pb_set_issue_status(self, key: str, status: str) -> None:
        self._conn.execute(
            "UPDATE playbook_issues SET status = ? WHERE key = ?", (status, key))
        self._conn.commit()

    def pb_mark_programmed(self, key: str) -> None:
        self._conn.execute(
            "UPDATE playbook_issues SET programmed = 1, status = 'known' WHERE key = ?", (key,))
        self._conn.commit()

    def pb_forget(self, key: str) -> bool:
        cur = self._conn.execute("DELETE FROM playbook_issues WHERE key = ?", (key,))
        self._conn.execute("DELETE FROM playbook_remedies WHERE issue_key = ?", (key,))
        self._conn.execute("DELETE FROM playbook_incidents WHERE issue_key = ?", (key,))
        self._conn.commit()
        return cur.rowcount > 0

    # ── Playbook: remedies ───────────────────────────────────────────────
    def pb_add_remedy(self, issue_key: str, kind: str, payload: str = "",
                      status: str = "approved", priority: int = 0) -> dict[str, Any]:
        rid = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO playbook_remedies (id, issue_key, kind, payload, status, priority, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (rid, issue_key, kind, payload, status, priority, time.time()),
        )
        self._conn.commit()
        return self.pb_get_remedy(rid)  # type: ignore[return-value]

    def pb_get_remedy(self, remedy_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM playbook_remedies WHERE id = ?", (remedy_id,)
        ).fetchone()
        return dict(row) if row else None

    def pb_remedies_for(self, issue_key: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM playbook_remedies WHERE issue_key = ?", (issue_key,)
        ).fetchall()
        return [dict(r) for r in rows]

    def pb_set_remedy(self, remedy_id: str, status: Optional[str] = None,
                      priority: Optional[int] = None) -> bool:
        sets, vals = [], []
        if status is not None:
            sets.append("status = ?"); vals.append(status)
        if priority is not None:
            sets.append("priority = ?"); vals.append(int(priority))
        if not sets:
            return False
        vals.append(remedy_id)
        cur = self._conn.execute(
            f"UPDATE playbook_remedies SET {', '.join(sets)} WHERE id = ?", vals)
        self._conn.commit()
        return cur.rowcount > 0

    def pb_record_outcome(self, remedy_id: str, helped: bool) -> None:
        """Tally a remedy's result and stamp last use."""
        col = "times_helped" if helped else "times_didnt_help"
        self._conn.execute(
            f"UPDATE playbook_remedies SET times_tried = times_tried + 1, "
            f"{col} = {col} + 1, last_outcome = ?, last_used = ? WHERE id = ?",
            ("helped" if helped else "didnt_help", time.time(), remedy_id),
        )
        self._conn.commit()

    def pb_remove_remedy(self, remedy_id: str) -> bool:
        cur = self._conn.execute("DELETE FROM playbook_remedies WHERE id = ?", (remedy_id,))
        self._conn.commit()
        return cur.rowcount > 0

    # ── Playbook: incidents ──────────────────────────────────────────────
    def pb_open_incident(self, issue_key: str, trigger: dict | str) -> str:
        iid = uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO playbook_incidents (id, issue_key, ts, trigger, outcome) "
            "VALUES (?, ?, ?, ?, 'pending')",
            (iid, issue_key, time.time(),
             trigger if isinstance(trigger, str) else json.dumps(trigger)[:4000]),
        )
        self._conn.commit()
        return iid

    def pb_set_incident_remedy(self, incident_id: str, remedy_id: str) -> None:
        self._conn.execute(
            "UPDATE playbook_incidents SET remedy_id = ? WHERE id = ?",
            (remedy_id, incident_id))
        self._conn.commit()

    def pb_close_incident(self, incident_id: str, outcome: str, notes: str = "") -> None:
        self._conn.execute(
            "UPDATE playbook_incidents SET outcome = ?, resolved_ts = ?, notes = ? WHERE id = ?",
            (outcome, time.time(), notes[:2000], incident_id))
        self._conn.commit()

    def pb_recent_incidents(self, issue_key: str = "", limit: int = 20) -> list[dict[str, Any]]:
        if issue_key:
            rows = self._conn.execute(
                "SELECT * FROM playbook_incidents WHERE issue_key = ? ORDER BY ts DESC LIMIT ?",
                (issue_key, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM playbook_incidents ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

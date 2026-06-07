#!/usr/bin/env python3
"""Markdown Note-Taking App — Flask SQLite Backend API."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# App & Database setup
# ---------------------------------------------------------------------------

app = Flask(__name__)
DB_PATH = Path("notes.db")


def _get_db() -> sqlite3.Connection:
    """Return a connection with row-factory enabled."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # better concurrent reads
    return conn


def _init_db() -> None:
    """Create the notes table if it doesn't exist."""
    with _get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS notes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                title       TEXT    NOT NULL,
                content     TEXT    NOT NULL DEFAULT '',
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL
            );
        """)


# Run on import so the table is guaranteed before the first request.
_init_db()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _now() -> str:
    """ISO-8601 timestamp in UTC."""
    return datetime.now(timezone.utc).isoformat()


def _row_to_note(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return {
        "id":         row["id"],
        "title":      row["title"],
        "content":    row["content"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _not_found(note_id: int) -> tuple:
    """Standard 404 response."""
    return jsonify({"error": f"Note with id {note_id} not found"}), 404


BAD_REQUEST = (jsonify({"error": "Request body must be JSON"}), 400)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/notes", methods=["POST"])
def create_note():
    """Create a new note."""
    data = request.get_json(silent=True)
    if data is None:
        return BAD_REQUEST

    title = (data.get("title") or "").strip()
    # Title is required (at least one non-whitespace character)
    if not title:
        return jsonify({"error": "title is required and must be non-empty"}), 400

    content = data.get("content", "")
    now = _now()

    with _get_db() as conn:
        cur = conn.execute(
            "INSERT INTO notes (title, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (title, content, now, now),
        )
        note_id = cur.lastrowid

    return jsonify({"id": note_id, "message": "Note created"}), 201


@app.route("/notes", methods=["GET"])
def list_notes():
    """Return all notes, newest first."""
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT id, title, created_at, updated_at FROM notes ORDER BY updated_at DESC"
        ).fetchall()

    notes = [dict(r) for r in rows]
    return jsonify(notes), 200


@app.route("/notes/<int:note_id>", methods=["GET"])
def get_note(note_id: int):
    """Return a single note by id."""
    with _get_db() as conn:
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()

    if row is None:
        return _not_found(note_id)

    return jsonify(_row_to_note(row)), 200


@app.route("/notes/<int:note_id>", methods=["PUT"])
def update_note(note_id: int):
    """Update an existing note's title and/or content."""
    data = request.get_json(silent=True)
    if data is None:
        return BAD_REQUEST

    # Fetch existing
    with _get_db() as conn:
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()

    if row is None:
        return _not_found(note_id)

    new_title = data.get("title")
    new_content = data.get("content")

    # If title is passed, it must be non-empty after stripping
    if new_title is not None:
        stripped = new_title.strip()
        if not stripped:
            return jsonify({"error": "title must be non-empty"}), 400
        new_title = stripped

    # Fall back to existing values for fields not provided
    final_title = new_title if new_title is not None else row["title"]
    final_content = new_content if new_content is not None else row["content"]
    now = _now()

    with _get_db() as conn:
        conn.execute(
            "UPDATE notes SET title = ?, content = ?, updated_at = ? WHERE id = ?",
            (final_title, final_content, now, note_id),
        )

    return jsonify({"message": "Note updated"}), 200


@app.route("/notes/<int:note_id>", methods=["DELETE"])
def delete_note(note_id: int):
    """Delete a note by id."""
    with _get_db() as conn:
        row = conn.execute("SELECT id FROM notes WHERE id = ?", (note_id,)).fetchone()
        if row is None:
            return _not_found(note_id)

        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))

    return jsonify({"message": "Note deleted"}), 200


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@app.route("/health", methods=["GET"])
def health():
    """Health-check endpoint for Docker HEALTHCHECK and k8s probes."""
    return jsonify({"status": "healthy"}), 200


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------


@app.errorhandler(404)
def _handle_404(_exc):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(405)
def _handle_405(_exc):
    return jsonify({"error": "Method not allowed"}), 405


@app.errorhandler(500)
def _handle_500(_exc):
    return jsonify({"error": "Internal server error"}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, port=5000)
import json
import sqlite3

from app.api import db_viewer


def _database(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE interactions ("
        "id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
        "tool_name TEXT, output TEXT, metadata TEXT, parent_id TEXT, "
        "status TEXT, created_at TEXT)"
    )
    return conn


def test_session_tool_detail_returns_only_requested_call(monkeypatch, tmp_path):
    path = tmp_path / "tool-detail.db"
    conn = _database(path)
    output = json.dumps({"tool_calls": [
        {"id": "call-0", "function": {"name": "edit_file", "arguments": json.dumps({"patch": "A" * 5000})}},
        {"id": "call-1", "function": {"name": "search_source", "arguments": json.dumps({"query": "needle"})}},
    ]})
    conn.execute(
        "INSERT INTO interactions VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("assistant", "session", "assistant", "", None, output, None, None, "complete", "2026-01-01"),
    )
    conn.execute(
        "INSERT INTO interactions VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("tool-0", "session", "tool", "large edit result", "edit_file", None, None, "assistant", "complete", "2026-01-02"),
    )
    conn.execute(
        "INSERT INTO interactions VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("tool-1", "session", "tool", "one search result", "search_source", None, None, "assistant", "complete", "2026-01-03"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db_viewer, "_resolve_session_db", lambda _sid, db: db)
    monkeypatch.setattr(db_viewer, "_open", lambda _db: (sqlite3.connect(path), "sqlite"))
    monkeypatch.setattr(db_viewer, "_session_access_ok", lambda *_args, **_kwargs: True)

    result = db_viewer.get_session_tool_detail(
        object(), "session", assistant_id="assistant", tool_index=1,
        tool_id=None, user_id="user", db="user.db",
    )

    assert result["detail"]["tool_call_id"] == "call-1"
    assert json.loads(result["detail"]["arguments"]) == {"query": "needle"}
    assert result["detail"]["content"] == "one search result"
    assert "large edit result" not in json.dumps(result)
    assert "A" * 100 not in json.dumps(result)


def test_synthetic_tool_detail_uses_stable_tool_id(monkeypatch, tmp_path):
    path = tmp_path / "synthetic-detail.db"
    conn = _database(path)
    conn.execute(
        "INSERT INTO interactions VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "vision-1", "session", "tool", "image description", "process_image",
            None, json.dumps({"args": {"path": "image.png"}, "duration_ms": 12}),
            "user-turn", "complete", "2026-01-01",
        ),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db_viewer, "_resolve_session_db", lambda _sid, db: db)
    monkeypatch.setattr(db_viewer, "_open", lambda _db: (sqlite3.connect(path), "sqlite"))
    monkeypatch.setattr(db_viewer, "_session_access_ok", lambda *_args, **_kwargs: True)

    result = db_viewer.get_session_tool_detail(
        object(), "session", assistant_id=None, tool_index=0,
        tool_id="vision-1", user_id="user", db="user.db",
    )

    assert result["detail"]["tool_id"] == "vision-1"
    assert result["detail"]["arguments"] == {"path": "image.png"}
    assert result["detail"]["content"] == "image description"

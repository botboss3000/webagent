import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_chat_ui_names_the_mixed_resource_row_session_tabs():
    config = json.loads(_source("data/config/chat_ui.json"))
    control = config["controls_library"]["controls"]["sub_agent_tabs"]

    assert control["display_name"] == "Session Tabs"
    assert "browsers" in control["description"]
    assert 'aria-label="Session tabs"' in _source("ui/chat/chat-side-panel.html")


def test_browser_session_tabs_use_short_label_and_lifecycle_classes():
    source = _source("ui/chat/elements/session-dropdown/list.js")
    css = _source("ui/shared/css/app1.css")

    assert '<span class="chip-label">Browser</span>' in source
    assert "bs.title || bs.url || 'Browser'" not in source
    assert "chip-browser-idle" in source
    assert "chip-browser-closed" in source
    assert ".chip-browser-idle" in css
    assert ".chip-browser-closed .chip-label" in css
    assert "text-decoration: line-through" in css


def test_session_tabs_share_one_updated_or_viewed_recency_order():
    source = _source("ui/chat/elements/session-dropdown/list.js")

    assert "Math.max(viewed, updated)" in source
    assert "normalizedUpdated" in source
    assert "+ 'Z'" in source
    assert "_touchSessionTab(chip);" in source
    assert "_sortSessionTabs(wrap);" in source
    for prefix in ("session:", "browser:", "genui:", "component:"):
        assert prefix in source
    # Category separators would prevent a true mixed-resource ordering.
    render = source[source.index("// Browser tabs"):source.index("const wrap = document.getElementById('chat-sub-scroll-wrap');", source.index("// Browser tabs"))]
    assert "chip-sep" not in render


def test_session_prewarm_uses_shared_activity_order_without_undefined_helper():
    source = _source("ui/chat/js/session-prewarm.js")

    assert "bumped.sort(compareSessionsByRecentActivity)" in source
    assert "_activityOf" not in source


def test_browser_tabs_are_owned_by_the_current_chat_in_schema_and_related_query():
    schema = _source("app/db/schema/tables.py")
    related = _source("app/api/db_viewer.py")
    browser_ui = _source("ui/main-panel/browser/js/browser.js")
    user_store = _source("app/db/user_store.py")

    assert 'Column("chat_session_id", "TEXT")' in schema
    assert "AND chat_session_id = ?" in related
    assert "session_id: app.currentSessionId || null" in browser_ui
    assert "ALTER TABLE browser_sessions ADD COLUMN chat_session_id TEXT" in user_store
    assert 'ensure_sqlite_plane_columns(conn, "user")' in user_store


def test_existing_user_browser_table_adds_chat_owner_before_index(monkeypatch, tmp_path):
    from app.db import user_store

    monkeypatch.setattr(user_store, "USER_DATA_DIR", str(tmp_path))
    uid = "browser-migration-test"
    db_path = tmp_path / uid / f"{uid}.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE browser_sessions (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, agent_id TEXT, title TEXT,
        url TEXT, shared INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active', storage_state TEXT,
        position INTEGER NOT NULL DEFAULT 0, created_at TEXT, updated_at TEXT
    )""")
    conn.commit()
    conn.close()

    store = user_store.UserStore(uid)
    migrated = store._get_conn()
    columns = {row[1] for row in migrated.execute(
        "PRAGMA table_info(browser_sessions)"
    ).fetchall()}
    indexes = {row[1] for row in migrated.execute(
        "PRAGMA index_list(browser_sessions)"
    ).fetchall()}
    store.close()

    assert "chat_session_id" in columns
    assert "idx_browser_sessions_chat" in indexes


def test_header_browser_close_is_durable_not_delete():
    source = _source("ui/chat/elements/session-dropdown/list.js")
    endpoint = _source("app/api/browser_stream.py")

    assert "+ '/close'" in source
    assert '@router.post("/api/v1/browser/sessions/{bs_id}/close")' in endpoint
    assert 'await _engine.close(bs_id, status="closed")' in endpoint


def test_component_listing_exposes_authoritative_update_time(monkeypatch):
    from app import chat_components
    import app.db
    from app.models.schemas import InteractionRecord

    row = InteractionRecord(
        id="i1",
        session_id="s1",
        role="tool",
        tool_name="chat_component",
        content=json.dumps({"component": {
            "id": "c1", "type": "status", "title": "Plan",
            "data": {"sections": []},
        }}),
        created_at="2026-08-24T12:34:56Z",
    )

    async def fake_fetch(_user_id, _session_id):
        return [row]

    monkeypatch.setattr(app.db, "get_db", lambda: SimpleNamespace(fetch_interactions=fake_fetch))
    components = asyncio.run(chat_components.list_components("u1", "s1"))

    assert components[0]["updated_at"].startswith("2026-08-24T12:34:56")

"""Read the WebAgent app's RENDER RECORDER output — the browser flight-recorder
that captures HTML snapshots, DOM-mutation deltas, lag / long-task metrics, JS
errors, console warnings, and failed/slow network calls from the live web UI.

This is the companion to ``read_diagnostics``: diagnostics is the *server's*
flight-recorder (``logs.db``); this is the *browser's* (``recordings.db``). They
have different shapes, so they are deliberately separate tools.

Read STRAIGHT from the linked checkout's dedicated recordings database
(``data/db/recordings.db`` — the ``render_recordings`` table), so it works even
when the server is **down**. Strictly read-only (opens the DB read-only, never
writes).

The recorder is **off by default**. Turn it on yourself with
``app_set_settings({"render_recording_enabled": true})``, reproduce the UI
behaviour in the browser (or via the connected app session), then read it back
here — and turn it off again when you're done.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .base import ToolContext

# Columns that are always cheap to return (the big `html` blob is opt-in).
_COLS = "id, ts, kind, session_id, url, level, label, value_num, detail, html_bytes"


def _rec_db(project_root: Path) -> Path:
    """The dedicated recordings DB. Runtime DBs live under data/db/ on current
    builds; the old app/db/ location is checked as a fallback."""
    for rel in ("data/db/recordings.db", "app/db/recordings.db"):
        p = project_root / rel
        if p.exists():
            return p
    return project_root / "data" / "db" / "recordings.db"


async def read_recordings(ctx: ToolContext, limit: int = 20, kind: str = "",
                          level: str = "", session_id: str = "",
                          since_minutes: int = 0, search: str = "",
                          rec_id: str = "", include_html: bool = False) -> str:
    """Most-recent render-recorder rows from the linked checkout, optionally
    filtered by ``kind`` (snapshot/mutation/lag/js_error/console/network/nav/
    meta), ``level``, ``session_id``, ``since_minutes``, or ``search``. Pass
    ``rec_id`` (or ``include_html=true``) to also return the captured HTML for
    matched rows — keep ``limit`` small when you do, the blobs are large."""
    if ctx.project_root is None:
        return "Error: no checkout linked — link one first."
    db = _rec_db(ctx.project_root)
    if not db.exists():
        return (f"No recordings database yet ({db} not found). The render recorder "
                "is OFF by default — enable it with "
                'app_set_settings({"render_recording_enabled": true}), reproduce '
                "the UI behaviour in the browser, then read again.")

    where: list[str] = []
    params: list = []
    if rec_id:
        where.append("id = ?"); params.append(rec_id)
    if kind:
        where.append("lower(kind) = ?"); params.append(kind.lower())
    if level:
        where.append("lower(level) = ?"); params.append(level.lower())
    if session_id:
        where.append("session_id = ?"); params.append(session_id)
    if since_minutes and int(since_minutes) > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=int(since_minutes))).isoformat()
        where.append("ts >= ?"); params.append(cutoff)
    if search:
        where.append("(label LIKE ? OR detail LIKE ? OR url LIKE ?)")
        like = f"%{search}%"; params.extend([like, like, like])
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(int(limit or 20), 200))
    want_html = bool(include_html or rec_id)
    cols = _COLS + (", html" if want_html else "")

    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cur = con.cursor()
        by_kind = dict(cur.execute(
            "SELECT kind, count(*) FROM render_recordings GROUP BY kind").fetchall())
        by_level = dict(cur.execute(
            "SELECT level, count(*) FROM render_recordings "
            "WHERE level IS NOT NULL AND level != '' GROUP BY level").fetchall())
        rows = cur.execute(
            f"SELECT {cols} FROM render_recordings{clause} ORDER BY ts DESC LIMIT ?",
            (*params, limit)).fetchall()
        con.close()
    except sqlite3.Error as e:
        if "no such table" in str(e).lower():
            return ("The recordings database exists but has no render_recordings "
                    "table yet — enable the recorder "
                    '(app_set_settings({"render_recording_enabled": true})) and '
                    "reproduce the UI behaviour first.")
        return f"Error reading recordings: {e}"

    if not rows:
        any_filter = any((kind, level, session_id, since_minutes, search, rec_id))
        return "No recordings match that filter." if any_filter else (
            "No render recordings captured yet. Enable the recorder "
            '(app_set_settings({"render_recording_enabled": true})) and reproduce '
            "the UI behaviour in the browser, then read again.")

    kinds = ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())) or "none"
    levels = ", ".join(f"{k}={v}" for k, v in sorted(by_level.items())) or "none"
    filt = [x for x in (kind, level, session_id, search, rec_id) if x]
    if since_minutes and int(since_minutes) > 0:
        filt.append(f"last {since_minutes}m")
    filt_s = f" (filtered: {' '.join(filt)})" if filt else ""
    out = [f"render recordings — by kind: {kinds}; by level: {levels}; "
           f"showing {len(rows)} most recent{filt_s}:"]
    for row in rows:
        rid, ts, k, sid, url, lvl, label, val, detail, hb = row[:10]
        ts = (ts or "")[:19].replace("T", " ")
        head = f"  [{ts}] {k}"
        if lvl:
            head += f"/{lvl}"
        if label:
            head += f" {str(label)[:120]}"
        if val is not None:
            head += f" ={val}"
        if url:
            head += f" <{str(url)[:80]}>"
        if hb:
            head += f" ({hb}b html)"
        head += f"  id={rid}"
        out.append(head)
        if detail:
            d = " ".join(str(detail).split())
            if d and d not in ("{}", "null"):
                out.append(f"      detail: {d[:300]}")
        if want_html and len(row) > 10 and row[10]:
            h = str(row[10])
            shown = h[:4000]
            cap = "" if len(h) <= 4000 else f" (first 4000 of {len(h)} chars)"
            out.append(f"      html{cap}:")
            out.append(shown)
    return "\n".join(out)

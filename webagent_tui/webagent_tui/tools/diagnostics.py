"""Read the webAgent app's diagnostics — its flight-recorder of server
warnings/errors (with tracebacks), agent-loop problems, run outcomes, and tool
errors.

Read STRAIGHT from the linked checkout's ``app/db/local.db`` (the ``diagnostics``
table), so it works even when the server is **down** — exactly when you need it.
Strictly read-only (opens the DB read-only and never writes).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .base import ToolContext

_COLS = "ts, level, category, source, message, detail"


async def read_diagnostics(ctx: ToolContext, limit: int = 20, level: str = "",
                           category: str = "") -> str:
    """Most-recent diagnostics from the linked checkout, optionally filtered by
    ``level`` (e.g. error/warning) or ``category`` (e.g. server/http/agent)."""
    if ctx.project_root is None:
        return "Error: no checkout linked — link one first."
    db = ctx.project_root / "app" / "db" / "local.db"
    if not db.exists():
        return f"No diagnostics database yet ({db} not found — the app builds it on first run)."

    where: list[str] = []
    params: list[str] = []
    if level:
        where.append("lower(level) = ?")
        params.append(level.lower())
    if category:
        where.append("lower(category) = ?")
        params.append(category.lower())
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(int(limit or 20), 200))

    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cur = con.cursor()
        counts = dict(cur.execute(
            "SELECT level, count(*) FROM diagnostics GROUP BY level").fetchall())
        rows = cur.execute(
            f"SELECT {_COLS} FROM diagnostics{clause} ORDER BY id DESC LIMIT ?",
            (*params, limit)).fetchall()
        con.close()
    except sqlite3.Error as e:
        return f"Error reading diagnostics: {e}"

    if not rows:
        return "No diagnostics match that filter." if (level or category) else "No diagnostics recorded."

    totals = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
    filt = f" (filtered: {' '.join(x for x in (level, category) if x)})" if (level or category) else ""
    out = [f"diagnostics — totals: {totals}; showing {len(rows)} most recent{filt}:"]
    for ts, lvl, cat, src, msg, detail in rows:
        ts = (ts or "")[:19].replace("T", " ")
        out.append(f"  [{ts}] {lvl}/{cat} {src}: {(msg or '')[:200]}")
        if detail:
            d = " ".join(str(detail).split())
            if d and d not in ("{}", "null"):
                out.append(f"      detail: {d[:220]}")
    return "\n".join(out)

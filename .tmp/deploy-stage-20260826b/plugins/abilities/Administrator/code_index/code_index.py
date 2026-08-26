"""Code Index ability — SELF-CONTAINED drop-in.

A persistent, queryable codebase index at ``data/db/index.db``. Five tools:
  index_lookup    — search the index (symbols, files, features, routes, components)
  index_store     — insert/update indexed file records (the write path)
  index_progress  — read/write indexing progress (batch tracking, resume)
  index_features  — list known feature tags, register new ones
  index_opportunity — log/query improvement observations

The DB is created lazily on first use (``CREATE TABLE IF NOT EXISTS``).
All handlers are async; the index lives at the project-root-relative path
``data/db/index.db`` resolved from the plugin file's location.

Discovered generically by core (app/tools/loader.py "Self-contained ability
tools"): build_tools() returns handlers; TOOL_SCHEMAS / DESTRUCTIVE published
after the call.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()

_SEED_TAGS = [
    ("agent_loop", "Core agent execution loop and turn management"),
    ("api", "FastAPI route handlers and endpoints"),
    ("db", "Database backends, schema, and connection management"),
    ("auth", "Authentication, identity, JWT, and middleware"),
    ("browser", "Playwright browser control and session management"),
    ("tools", "Tool system — loader, registry, schemas, permissions"),
    ("ui", "Front-end HTML/CSS/JS under ui/"),
    ("abilities", "Drop-in ability plugin system and catalog"),
    ("orchestration", "Agent spawning, delegation, and concurrency"),
    ("automation", "Scheduled tasks, reminders, and event subscriptions"),
    ("scheduler", "Cron-style job scheduler and runners"),
    ("events", "Event system — sources, channels, executors"),
    ("wiki", "Wiki storage, search, and public pages"),
    ("visualizer", "GenUI dashboard rendering and editing"),
    ("genui", "GenUI store, common utilities, and hybrid sync"),
    ("terminal", "Terminal sessions, tunneling, and control"),
    ("deploy", "Deployment providers, bootstrap, and config embedding"),
    ("email", "Email integration and delivery"),
    ("github", "GitHub OAuth, API, and repository management"),
    ("oauth", "OAuth provider framework and social auth"),
    ("billing", "Billing, pricing, payments, and platform"),
    ("encryption", "Encryption backends, vault keys, and field encryption"),
    ("diagnostics", "Flight-recorder, error classification, and diag tools"),
    ("integrations", "Third-party integrations and marketplace tools"),
    ("relay", "Cloudflare relay for external connectivity"),
    ("tui", "Terminal UI application"),
    ("webhooks", "Inbound webhook registration and delivery"),
    ("channels", "Communication channels (Telegram, Discord, etc.)"),
    ("devices", "Device identity, dispatch, and worker management"),
    ("optimizer", "Prompt optimization engine and runner"),
    ("storage", "File storage backends — S3, GCS, local, Supabase"),
    ("models", "Model catalog, switching, and OpenAI compatibility"),
]


def _project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _index_db_path() -> Path:
    return _project_root() / "data" / "db" / "index.db"


def _legacy_index_db_path() -> Path:
    """Return the accidentally plugin-relative location used before July 2026."""
    return Path(__file__).resolve().parents[3] / "data" / "db" / "index.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()


# ── Schema (lazy, idempotent) ──────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS indexed_files (
    path          TEXT PRIMARY KEY,
    language      TEXT NOT NULL,
    sha256        TEXT NOT NULL,
    size          INTEGER NOT NULL,
    line_count    INTEGER NOT NULL DEFAULT 0,
    summary       TEXT,
    last_indexed  TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS symbols (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path     TEXT NOT NULL REFERENCES indexed_files(path) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind          TEXT NOT NULL,
    start_line    INTEGER NOT NULL,
    end_line      INTEGER NOT NULL,
    signature     TEXT,
    parent_name   TEXT
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);

CREATE TABLE IF NOT EXISTS imports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path     TEXT NOT NULL REFERENCES indexed_files(path) ON DELETE CASCADE,
    module        TEXT NOT NULL,
    imported_name TEXT,
    alias         TEXT,
    line          INTEGER NOT NULL,
    resolved_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file_path);

CREATE TABLE IF NOT EXISTS api_routes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path     TEXT NOT NULL REFERENCES indexed_files(path) ON DELETE CASCADE,
    method        TEXT NOT NULL,
    route_path    TEXT NOT NULL,
    handler_name  TEXT NOT NULL,
    line          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_routes_route ON api_routes(route_path);
CREATE INDEX IF NOT EXISTS idx_api_routes_file ON api_routes(file_path);

CREATE TABLE IF NOT EXISTS ui_components (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path     TEXT NOT NULL REFERENCES indexed_files(path) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    line          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ui_components_name ON ui_components(name);

CREATE TABLE IF NOT EXISTS file_features (
    file_path     TEXT NOT NULL REFERENCES indexed_files(path) ON DELETE CASCADE,
    feature       TEXT NOT NULL,
    confidence    TEXT NOT NULL DEFAULT 'medium',
    notes         TEXT,
    PRIMARY KEY (file_path, feature)
);
CREATE INDEX IF NOT EXISTS idx_file_features_feature ON file_features(feature);

CREATE TABLE IF NOT EXISTS feature_tags (
    tag           TEXT PRIMARY KEY,
    description   TEXT,
    first_seen    TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS index_progress (
    batch_id      TEXT PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'pending',
    file_count    INTEGER NOT NULL DEFAULT 0,
    files         TEXT,
    agent_id      TEXT,
    started_at    TIMESTAMP,
    completed_at  TIMESTAMP,
    error         TEXT
);

CREATE TABLE IF NOT EXISTS opportunities (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path     TEXT NOT NULL,
    category      TEXT NOT NULL,
    severity      TEXT NOT NULL DEFAULT 'medium',
    description   TEXT NOT NULL,
    suggestion    TEXT,
    found_at      TIMESTAMP NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_opportunities_status ON opportunities(status);
CREATE INDEX IF NOT EXISTS idx_opportunities_file ON opportunities(file_path);

CREATE TABLE IF NOT EXISTS comment_audits (
    file_path       TEXT PRIMARY KEY REFERENCES indexed_files(path) ON DELETE CASCADE,
    scope           TEXT NOT NULL,
    standard_version TEXT NOT NULL,
    status          TEXT NOT NULL,
    header_style    TEXT,
    purpose_header  INTEGER NOT NULL DEFAULT 0,
    header_start_line INTEGER,
    breadcrumb_count INTEGER NOT NULL DEFAULT 0,
    valid_breadcrumb_count INTEGER NOT NULL DEFAULT 0,
    checked_at      TIMESTAMP NOT NULL,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_comment_audits_status ON comment_audits(status);

CREATE TABLE IF NOT EXISTS breadcrumb_refs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path       TEXT NOT NULL REFERENCES indexed_files(path) ON DELETE CASCADE,
    target_path     TEXT NOT NULL,
    line            INTEGER NOT NULL,
    target_exists   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_breadcrumb_refs_file ON breadcrumb_refs(file_path);
CREATE INDEX IF NOT EXISTS idx_breadcrumb_refs_target ON breadcrumb_refs(target_path);

CREATE TABLE IF NOT EXISTS comment_markers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path       TEXT NOT NULL REFERENCES indexed_files(path) ON DELETE CASCADE,
    marker          TEXT NOT NULL,
    line            INTEGER NOT NULL,
    detail          TEXT
);
CREATE INDEX IF NOT EXISTS idx_comment_markers_file ON comment_markers(file_path);
CREATE INDEX IF NOT EXISTS idx_comment_markers_marker ON comment_markers(marker);
"""

_SEED_SQL = "INSERT OR IGNORE INTO feature_tags(tag, description, first_seen) VALUES (?, ?, ?)"


def _ensure_schema() -> sqlite3.Connection:
    """Open (or create) the index DB and ensure all tables exist. Idempotent."""
    db_path = _index_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path = _legacy_index_db_path()
    if not db_path.exists() and legacy_path != db_path and legacy_path.exists():
        shutil.copy2(legacy_path, db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_DDL)
    indexed_file_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(indexed_files)").fetchall()
    }
    if "line_count" not in indexed_file_columns:
        conn.execute(
            "ALTER TABLE indexed_files ADD COLUMN line_count INTEGER NOT NULL DEFAULT 0"
        )
    now = _now_iso()
    conn.executemany(_SEED_SQL, [(t, d, now) for t, d in _SEED_TAGS])
    conn.commit()
    return conn


# ── helpers ─────────────────────────────────────────────────────────────────────

def _as_list(raw: Any) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return []
    return []


_COMMENT_STANDARD_VERSION = "ui-breadcrumb-v1"
_COMMENT_EXTENSIONS = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".html", ".css", ".py"}
_PATH_RE = re.compile(
    r"(?<![\w.-])((?:ui|app|plugins|TUI|scripts|tests|docs)/"
    r"[A-Za-z0-9_@+.,{}-]+(?:/[A-Za-z0-9_@+.,{}-]+)*\.[A-Za-z0-9]+)"
)
_MARKER_RE = re.compile(
    r"\b(REMOVE-WHEN|DEACTIVATED\s+\((?:intentional|orphaned)\)|"
    r"KEEP\s+\(intentional\)|SISTER-PANEL|COLOR SCHEME)\s*:?\s*(.*)",
    re.IGNORECASE,
)


def _comment_exemption(file_path: str) -> Optional[str]:
    path = file_path.replace("\\", "/")
    name = Path(path).name
    if "/vendor/" in f"/{path}" or name.endswith(".min.js"):
        return "vendored_or_minified"
    if path.startswith("ui/background/"):
        return "background_plugin_banner"
    if path in {
        "ui/shared/js/admin-ability-table.js",
        "ui/shared/js/agent-ability-table.js",
    }:
        return "sister_panel_banner"
    if path.startswith("ui/splash/") or path == "ui/shared/js/cursor-effects.js":
        return "subsystem_banner"
    return None


def _leading_comment(source: str, suffix: str) -> tuple[str, Optional[int], str]:
    """Return (comment text, one-based start line, style) for the opening header."""
    lines = source.lstrip("\ufeff").splitlines()
    index = 0
    while index < len(lines) and not lines[index].strip():
        index += 1
    if index < len(lines) and re.fullmatch(r"""["']use strict["'];?""", lines[index].strip()):
        index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1
    if index >= len(lines):
        return "", None, ""

    stripped = lines[index].lstrip()
    if stripped.startswith("//"):
        block = []
        start = index
        while index < len(lines) and lines[index].lstrip().startswith("//"):
            block.append(lines[index].lstrip()[2:].lstrip())
            index += 1
        return "\n".join(block), start + 1, "line"
    if stripped.startswith("/*"):
        block = []
        start = index
        while index < len(lines):
            block.append(lines[index])
            if "*/" in lines[index]:
                break
            index += 1
        return "\n".join(block), start + 1, "jsdoc" if stripped.startswith("/**") else "block"
    if stripped.startswith("<!--"):
        block = []
        start = index
        while index < len(lines):
            block.append(lines[index])
            if "-->" in lines[index]:
                break
            index += 1
        return "\n".join(block), start + 1, "html"
    if suffix == ".py" and stripped.startswith("#"):
        block = []
        start = index
        while index < len(lines) and lines[index].lstrip().startswith("#"):
            block.append(lines[index].lstrip()[1:].lstrip())
            index += 1
        return "\n".join(block), start + 1, "hash"
    return "", None, ""


def _analyze_comment_standard(file_path: str, source: Optional[str]) -> dict:
    """Deterministically audit the documented UI breadcrumb/comment standard."""
    normalized = file_path.replace("\\", "/")
    suffix = Path(normalized).suffix.lower()
    result = {
        "scope": "ui" if normalized.startswith("ui/") else "not_applicable",
        "standard_version": _COMMENT_STANDARD_VERSION,
        "status": "not_applicable",
        "header_style": "",
        "purpose_header": False,
        "header_start_line": None,
        "breadcrumbs": [],
        "markers": [],
        "notes": [],
    }
    if not normalized.startswith("ui/") or suffix not in _COMMENT_EXTENSIONS:
        return result

    exemption = _comment_exemption(normalized)
    if exemption:
        result["status"] = "exempt"
        result["notes"].append(exemption)
    elif source is None:
        result["status"] = "not_checked"
        result["notes"].append("source_not_provided")
        return result

    content = source or ""
    header, start_line, style = _leading_comment(content, suffix)
    result["header_style"] = style
    result["header_start_line"] = start_line
    result["purpose_header"] = bool(re.search(r"\S+\s+—\s+\S+", header))

    for match in _PATH_RE.finditer(header):
        target = match.group(1).rstrip(".,;:)")
        line = (start_line or 1) + header[:match.start()].count("\n")
        result["breadcrumbs"].append({
            "target_path": target,
            "line": line,
            "target_exists": (_project_root() / target).exists(),
        })

    for line_number, line in enumerate(content.splitlines(), 1):
        marker_match = _MARKER_RE.search(line)
        if marker_match:
            result["markers"].append({
                "marker": re.sub(r"\s+", " ", marker_match.group(1).upper()),
                "line": line_number,
                "detail": marker_match.group(2).strip().rstrip("*/ -->"),
            })

    if exemption:
        return result
    if not header:
        result["status"] = "missing"
        result["notes"].append("opening_comment_missing")
    elif not result["purpose_header"]:
        result["status"] = "malformed"
        result["notes"].append("purpose_line_missing")
    elif not result["breadcrumbs"]:
        result["status"] = "malformed"
        result["notes"].append("breadcrumb_path_missing")
    elif any(not crumb["target_exists"] for crumb in result["breadcrumbs"]):
        result["status"] = "stale"
        result["notes"].append("breadcrumb_target_missing")
    else:
        result["status"] = "compliant"
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# TOOL HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════


async def _index_lookup(
    action: str,
    query: Optional[str] = None,
    kind: Optional[str] = None,
    feature: Optional[str] = None,
    file_path: Optional[str] = None,
    limit: int = 50,
    category: Optional[str] = None,
    severity: Optional[str] = None,
    status: Optional[str] = None,
    comment_status: Optional[str] = None,
    marker: Optional[str] = None,
    header_style: Optional[str] = None,
) -> str:
    """Query the code index.

    Actions:
      symbol     — search symbols by name (query), optionally filtered by kind
      file       — get indexed info for a specific file_path
      feature    — list files tagged with a feature, or list all features
      route      — search API routes by path pattern (query)
      component  — search UI components by name (query)
      imports    — find files that import a module (query)
      opportunity — list logged opportunities, optionally filtered
      comments    — list breadcrumb/comment audits, optionally filtered
      summary    — return index stats
    """
    conn = _ensure_schema()
    try:
        if action == "symbol":
            if not query:
                return json.dumps({"status": "error", "message": "query required"})
            params: list = [f"%{query}%"]
            sql = "SELECT name, qualified_name, kind, file_path, start_line, end_line, signature FROM symbols WHERE name LIKE ?"
            if kind:
                sql += " AND kind = ?"
                params.append(kind)
            sql += f" ORDER BY name LIMIT {min(limit, 200)}"
            rows = conn.execute(sql, params).fetchall()
            return json.dumps({"status": "ok", "count": len(rows), "results": [dict(r) for r in rows]})

        elif action == "file":
            if not file_path:
                return json.dumps({"status": "error", "message": "file_path required"})
            frow = conn.execute("SELECT * FROM indexed_files WHERE path = ?", (file_path,)).fetchone()
            if not frow:
                return json.dumps({"status": "not_found", "file_path": file_path})
            result = dict(frow)
            result["symbols"] = [dict(s) for s in conn.execute(
                "SELECT name, kind, start_line, signature FROM symbols WHERE file_path = ? ORDER BY start_line",
                (file_path,)).fetchall()]
            result["features"] = [dict(f) for f in conn.execute(
                "SELECT feature, confidence FROM file_features WHERE file_path = ?", (file_path,)).fetchall()]
            result["imports"] = [dict(i) for i in conn.execute(
                "SELECT module, imported_name, alias, line FROM imports WHERE file_path = ?", (file_path,)).fetchall()]
            result["api_routes"] = [dict(r) for r in conn.execute(
                "SELECT method, route_path, handler_name, line FROM api_routes WHERE file_path = ?", (file_path,)).fetchall()]
            result["ui_components"] = [dict(c) for c in conn.execute(
                "SELECT name, kind, line FROM ui_components WHERE file_path = ?", (file_path,)).fetchall()]
            audit = conn.execute(
                "SELECT * FROM comment_audits WHERE file_path = ?", (file_path,)).fetchone()
            result["comment_audit"] = dict(audit) if audit else None
            result["breadcrumbs"] = [dict(b) for b in conn.execute(
                "SELECT target_path, line, target_exists FROM breadcrumb_refs "
                "WHERE file_path = ? ORDER BY line, target_path", (file_path,)).fetchall()]
            result["comment_markers"] = [dict(m) for m in conn.execute(
                "SELECT marker, line, detail FROM comment_markers "
                "WHERE file_path = ? ORDER BY line, marker", (file_path,)).fetchall()]
            return json.dumps({"status": "ok", "file": result}, default=str)

        elif action == "feature":
            if feature:
                rows = conn.execute(
                    "SELECT ff.file_path, ff.confidence, ff.notes, ifs.language, ifs.summary "
                    "FROM file_features ff JOIN indexed_files ifs ON ff.file_path = ifs.path "
                    "WHERE ff.feature = ? ORDER BY ff.file_path LIMIT ?",
                    (feature, min(limit, 500))).fetchall()
                return json.dumps({"status": "ok", "feature": feature, "count": len(rows), "files": [dict(r) for r in rows]})
            else:
                rows = conn.execute(
                    "SELECT ft.tag, ft.description, ft.first_seen, "
                    "(SELECT COUNT(*) FROM file_features ff WHERE ff.feature = ft.tag) AS file_count "
                    "FROM feature_tags ft ORDER BY file_count DESC").fetchall()
                return json.dumps({"status": "ok", "count": len(rows), "features": [dict(r) for r in rows]})

        elif action == "route":
            if not query:
                return json.dumps({"status": "error", "message": "query required"})
            rows = conn.execute(
                "SELECT method, route_path, handler_name, file_path, line FROM api_routes "
                "WHERE route_path LIKE ? ORDER BY route_path LIMIT ?",
                (f"%{query}%", min(limit, 200))).fetchall()
            return json.dumps({"status": "ok", "count": len(rows), "results": [dict(r) for r in rows]})

        elif action == "component":
            if not query:
                return json.dumps({"status": "error", "message": "query required"})
            rows = conn.execute(
                "SELECT name, kind, file_path, line FROM ui_components "
                "WHERE name LIKE ? ORDER BY name LIMIT ?",
                (f"%{query}%", min(limit, 200))).fetchall()
            return json.dumps({"status": "ok", "count": len(rows), "results": [dict(r) for r in rows]})

        elif action == "imports":
            if not query:
                return json.dumps({"status": "error", "message": "query required"})
            rows = conn.execute(
                "SELECT file_path, module, imported_name, line FROM imports "
                "WHERE module LIKE ? OR imported_name LIKE ? ORDER BY file_path LIMIT ?",
                (f"%{query}%", f"%{query}%", min(limit, 200))).fetchall()
            return json.dumps({"status": "ok", "count": len(rows), "results": [dict(r) for r in rows]})

        elif action == "opportunity":
            sql = "SELECT * FROM opportunities WHERE 1=1"
            params_opp: list = []
            if category:
                sql += " AND category = ?"
                params_opp.append(category)
            if severity:
                sql += " AND severity = ?"
                params_opp.append(severity)
            if status:
                sql += " AND status = ?"
                params_opp.append(status)
            if file_path:
                sql += " AND file_path = ?"
                params_opp.append(file_path)
            sql += f" ORDER BY found_at DESC LIMIT {min(limit, 500)}"
            rows = conn.execute(sql, params_opp).fetchall()
            return json.dumps({"status": "ok", "count": len(rows), "results": [dict(r) for r in rows]})

        elif action == "comments":
            params_comments: list = []
            if marker:
                sql = (
                    "SELECT ca.*, ifs.language, ifs.summary, "
                    "cm.marker, cm.line AS marker_line, cm.detail AS marker_detail "
                    "FROM comment_audits ca "
                    "JOIN indexed_files ifs ON ifs.path = ca.file_path "
                    "JOIN comment_markers cm ON cm.file_path = ca.file_path "
                    "WHERE cm.marker = ?"
                )
                params_comments.append(marker.strip().upper())
            else:
                sql = (
                    "SELECT ca.*, ifs.language, ifs.summary "
                    "FROM comment_audits ca "
                    "JOIN indexed_files ifs ON ifs.path = ca.file_path WHERE 1=1"
                )
            if comment_status:
                sql += " AND ca.status = ?"
                params_comments.append(comment_status)
            if header_style:
                sql += " AND ca.header_style = ?"
                params_comments.append(header_style)
            if file_path:
                sql += " AND ca.file_path = ?"
                params_comments.append(file_path)
            if query:
                sql += " AND ca.file_path LIKE ?"
                params_comments.append(f"%{query}%")
            sql += " ORDER BY ca.status, ca.file_path LIMIT ?"
            params_comments.append(min(limit, 500))
            rows = conn.execute(sql, params_comments).fetchall()
            return json.dumps({
                "status": "ok",
                "count": len(rows),
                "results": [dict(r) for r in rows],
            })

        elif action == "summary":
            fc = conn.execute("SELECT COUNT(*) FROM indexed_files").fetchone()[0]
            totals = conn.execute(
                "SELECT COALESCE(SUM(size), 0) AS size_bytes, "
                "COALESCE(SUM(line_count), 0) AS line_count FROM indexed_files"
            ).fetchone()
            sc = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            ftc = conn.execute("SELECT COUNT(*) FROM feature_tags").fetchone()[0]
            rc = conn.execute("SELECT COUNT(*) FROM api_routes").fetchone()[0]
            cc = conn.execute("SELECT COUNT(*) FROM ui_components").fetchone()[0]
            oc = conn.execute("SELECT COUNT(*) FROM opportunities WHERE status = 'open'").fetchone()[0]
            prog = conn.execute("SELECT status, COUNT(*) as cnt FROM index_progress GROUP BY status").fetchall()
            comments = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM comment_audits GROUP BY status"
            ).fetchall()
            comment_styles = conn.execute(
                "SELECT header_style, COUNT(*) AS cnt FROM comment_audits "
                "WHERE header_style != '' GROUP BY header_style"
            ).fetchall()
            return json.dumps({
                "status": "ok", "indexed_files": fc, "symbols": sc,
                "total_size_bytes": totals["size_bytes"],
                "total_lines": totals["line_count"],
                "features": ftc, "api_routes": rc, "ui_components": cc,
                "open_opportunities": oc,
                "progress": {r["status"]: r["cnt"] for r in prog},
                "comment_compliance": {r["status"]: r["cnt"] for r in comments},
                "comment_header_styles": {r["header_style"]: r["cnt"] for r in comment_styles},
            })

        else:
            return json.dumps({"status": "error", "message": f"Unknown action '{action}'."})

    except Exception as e:
        logger.error("index_lookup failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})
    finally:
        conn.close()


async def _index_store(
    action: str,
    file_path: Optional[str] = None,
    language: Optional[str] = None,
    source: Optional[str] = None,
    summary: Optional[str] = None,
    symbols: Optional[List[Dict[str, Any]]] = None,
    imports: Optional[List[Dict[str, Any]]] = None,
    api_routes: Optional[List[Dict[str, Any]]] = None,
    ui_components: Optional[List[Dict[str, Any]]] = None,
    features: Optional[List[Dict[str, Any]]] = None,
    replace: bool = False,
) -> str:
    """Write to the code index. action=file to index one file; action=delete to remove."""
    conn = _ensure_schema()
    try:
        if action == "file":
            if not file_path:
                return json.dumps({"status": "error", "message": "file_path required"})
            if replace:
                for tbl in (
                    "comment_markers", "breadcrumb_refs", "comment_audits",
                    "file_features", "ui_components", "api_routes", "imports", "symbols",
                ):
                    conn.execute(f"DELETE FROM {tbl} WHERE file_path = ?", (file_path,))

            sha256 = _file_digest(source) if source else ""
            size = len(source.encode("utf-8")) if source else 0
            line_count = len(source.splitlines()) if source else 0
            lang = language or ""
            if not lang and file_path:
                ext = Path(file_path).suffix.lower()
                lang_map = {".py": "python", ".js": "javascript", ".jsx": "javascript",
                            ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript",
                            ".tsx": "tsx", ".html": "html", ".css": "css",
                            ".json": "json", ".md": "markdown", ".sql": "sql"}
                lang = lang_map.get(ext, "")

            conn.execute(
                "INSERT OR REPLACE INTO indexed_files("
                "path, language, sha256, size, line_count, summary, last_indexed"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (file_path, lang, sha256, size, line_count, summary or "", _now_iso()))

            cs, ci, cr, cc2, cf = 0, 0, 0, 0, 0
            for s in (symbols or []):
                if isinstance(s, dict) and s.get("name"):
                    conn.execute(
                        "INSERT INTO symbols(file_path, name, qualified_name, kind, start_line, end_line, signature, parent_name) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (file_path, s["name"], s.get("qualified_name") or s["name"],
                         s.get("kind", "unknown"), s.get("start_line", 0), s.get("end_line", 0),
                         s.get("signature") or "", s.get("parent_name") or ""))
                    cs += 1
            for imp in (imports or []):
                if isinstance(imp, dict) and imp.get("module"):
                    conn.execute(
                        "INSERT INTO imports(file_path, module, imported_name, alias, line, resolved_path) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (file_path, imp["module"], imp.get("imported_name") or "",
                         imp.get("alias") or "", imp.get("line", 0), imp.get("resolved_path") or ""))
                    ci += 1
            for r in (api_routes or []):
                if isinstance(r, dict) and r.get("route_path"):
                    conn.execute(
                        "INSERT INTO api_routes(file_path, method, route_path, handler_name, line) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (file_path, r.get("method", "GET"), r["route_path"],
                         r.get("handler_name", ""), r.get("line", 0)))
                    cr += 1
            for c in (ui_components or []):
                if isinstance(c, dict) and c.get("name"):
                    conn.execute("INSERT INTO ui_components(file_path, name, kind, line) VALUES (?, ?, ?, ?)",
                                (file_path, c["name"], c.get("kind", "component"), c.get("line", 0)))
                    cc2 += 1
            for f in (features or []):
                tag = f.get("feature") if isinstance(f, dict) else str(f)
                conf = f.get("confidence", "medium") if isinstance(f, dict) else "medium"
                notes = f.get("notes", "") if isinstance(f, dict) else ""
                if tag:
                    conn.execute(
                        "INSERT OR REPLACE INTO file_features(file_path, feature, confidence, notes) VALUES (?, ?, ?, ?)",
                        (file_path, tag, conf, notes))
                    conn.execute("INSERT OR IGNORE INTO feature_tags(tag, first_seen) VALUES (?, ?)",
                                (tag, _now_iso()))
                    cf += 1

            comment_audit = _analyze_comment_standard(file_path, source)
            breadcrumbs = comment_audit["breadcrumbs"]
            markers = comment_audit["markers"]
            conn.execute(
                "INSERT OR REPLACE INTO comment_audits("
                "file_path, scope, standard_version, status, header_style, purpose_header, "
                "header_start_line, breadcrumb_count, valid_breadcrumb_count, checked_at, notes"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    file_path,
                    comment_audit["scope"],
                    comment_audit["standard_version"],
                    comment_audit["status"],
                    comment_audit["header_style"],
                    int(comment_audit["purpose_header"]),
                    comment_audit["header_start_line"],
                    len(breadcrumbs),
                    sum(int(b["target_exists"]) for b in breadcrumbs),
                    _now_iso(),
                    json.dumps(comment_audit["notes"]),
                ),
            )
            conn.executemany(
                "INSERT INTO breadcrumb_refs(file_path, target_path, line, target_exists) "
                "VALUES (?, ?, ?, ?)",
                [
                    (file_path, b["target_path"], b["line"], int(b["target_exists"]))
                    for b in breadcrumbs
                ],
            )
            conn.executemany(
                "INSERT INTO comment_markers(file_path, marker, line, detail) VALUES (?, ?, ?, ?)",
                [
                    (file_path, m["marker"], m["line"], m["detail"])
                    for m in markers
                ],
            )
            conn.commit()
            return json.dumps({"status": "ok", "file_path": file_path,
                "symbols_written": cs, "imports_written": ci,
                "routes_written": cr, "components_written": cc2, "features_written": cf,
                "size_bytes": size,
                "line_count": line_count,
                "comment_status": comment_audit["status"],
                "breadcrumbs_checked": len(breadcrumbs),
                "comment_markers_found": len(markers)})

        elif action == "delete":
            if not file_path:
                return json.dumps({"status": "error", "message": "file_path required"})
            conn.execute("DELETE FROM indexed_files WHERE path = ?", (file_path,))
            conn.commit()
            return json.dumps({"status": "ok", "file_path": file_path, "deleted": True})

        else:
            return json.dumps({"status": "error", "message": f"Unknown action '{action}'."})

    except Exception as e:
        logger.error("index_store failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})
    finally:
        conn.close()


async def _index_progress(
    action: str,
    batch_id: Optional[str] = None,
    status: Optional[str] = None,
    file_count: Optional[int] = None,
    files: Optional[List[str]] = None,
    agent_id: Optional[str] = None,
    error: Optional[str] = None,
) -> str:
    """Track indexing progress across sessions."""
    conn = _ensure_schema()
    try:
        if action == "list":
            sql = "SELECT * FROM index_progress"
            params: list = []
            if status:
                sql += " WHERE status = ?"
                params.append(status)
            sql += " ORDER BY started_at DESC"
            rows = conn.execute(sql, params).fetchall()
            return json.dumps({"status": "ok", "count": len(rows), "batches": [dict(r) for r in rows]})

        elif action == "get":
            if not batch_id:
                return json.dumps({"status": "error", "message": "batch_id required"})
            row = conn.execute("SELECT * FROM index_progress WHERE batch_id = ?", (batch_id,)).fetchone()
            if not row:
                return json.dumps({"status": "not_found", "batch_id": batch_id})
            return json.dumps({"status": "ok", "batch": dict(row)})

        elif action == "start":
            if not batch_id:
                return json.dumps({"status": "error", "message": "batch_id required"})
            files_json = json.dumps(files or [])
            conn.execute(
                "INSERT OR REPLACE INTO index_progress(batch_id, status, file_count, files, agent_id, started_at) "
                "VALUES (?, 'in_progress', ?, ?, ?, ?)",
                (batch_id, file_count or len(files or []), files_json, agent_id or "", _now_iso()))
            conn.commit()
            return json.dumps({"status": "ok", "batch_id": batch_id, "file_count": file_count or len(files or [])})

        elif action == "update":
            if not batch_id:
                return json.dumps({"status": "error", "message": "batch_id required"})
            if status == "completed":
                conn.execute("UPDATE index_progress SET status = 'completed', completed_at = ? WHERE batch_id = ?",
                            (_now_iso(), batch_id))
            elif status == "error":
                conn.execute("UPDATE index_progress SET status = 'error', error = ?, completed_at = ? WHERE batch_id = ?",
                            (error or "", _now_iso(), batch_id))
            else:
                conn.execute("UPDATE index_progress SET status = ? WHERE batch_id = ?", (status or "", batch_id))
            if file_count is not None:
                conn.execute("UPDATE index_progress SET file_count = ? WHERE batch_id = ?", (file_count, batch_id))
            conn.commit()
            return json.dumps({"status": "ok", "batch_id": batch_id})

        elif action == "pending":
            indexed = set()
            for r in conn.execute("SELECT files FROM index_progress WHERE status IN ('completed', 'in_progress')").fetchall():
                for fp in _as_list(r["files"]):
                    indexed.add(fp)
            for r in conn.execute("SELECT path FROM indexed_files").fetchall():
                indexed.add(r["path"])
            return json.dumps({"status": "ok", "count": len(indexed), "indexed_paths": sorted(indexed)})

        else:
            return json.dumps({"status": "error", "message": f"Unknown action '{action}'."})

    except Exception as e:
        logger.error("index_progress failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})
    finally:
        conn.close()


async def _index_features(
    action: str,
    tag: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """Manage the organic feature-tag vocabulary."""
    conn = _ensure_schema()
    try:
        if action == "list":
            rows = conn.execute(
                "SELECT ft.tag, ft.description, ft.first_seen, "
                "(SELECT COUNT(*) FROM file_features ff WHERE ff.feature = ft.tag) AS file_count "
                "FROM feature_tags ft ORDER BY file_count DESC").fetchall()
            return json.dumps({"status": "ok", "count": len(rows), "features": [dict(r) for r in rows]})

        elif action == "register":
            if not tag:
                return json.dumps({"status": "error", "message": "tag required"})
            tag_clean = tag.strip().lower().replace(" ", "_")[:80]
            conn.execute(
                "INSERT OR REPLACE INTO feature_tags(tag, description, first_seen) VALUES (?, ?, ?)",
                (tag_clean, description or "", _now_iso()))
            conn.commit()
            return json.dumps({"status": "ok", "tag": tag_clean})

        elif action == "delete":
            if not tag:
                return json.dumps({"status": "error", "message": "tag required"})
            conn.execute("DELETE FROM feature_tags WHERE tag = ?", (tag,))
            conn.commit()
            return json.dumps({"status": "ok", "tag": tag, "deleted": True})

        else:
            return json.dumps({"status": "error", "message": f"Unknown action '{action}'."})

    except Exception as e:
        logger.error("index_features failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})
    finally:
        conn.close()


async def _index_opportunity(
    action: str,
    file_path: Optional[str] = None,
    category: Optional[str] = None,
    severity: Optional[str] = None,
    description: Optional[str] = None,
    suggestion: Optional[str] = None,
    opp_id: Optional[int] = None,
    new_status: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> str:
    """Log and manage codebase improvement opportunities."""
    conn = _ensure_schema()
    try:
        if action == "log":
            if not file_path or not description:
                return json.dumps({"status": "error", "message": "file_path and description required"})
            conn.execute(
                "INSERT INTO opportunities(file_path, category, severity, description, suggestion, found_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'open')",
                (file_path, category or "general", severity or "medium",
                 description, suggestion or "", _now_iso()))
            conn.commit()
            oid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            return json.dumps({"status": "ok", "id": oid})

        elif action == "list":
            sql = "SELECT * FROM opportunities WHERE 1=1"
            params: list = []
            filter_status = new_status or status
            if filter_status:
                sql += " AND status = ?"
                params.append(filter_status)
            if category:
                sql += " AND category = ?"
                params.append(category)
            if severity:
                sql += " AND severity = ?"
                params.append(severity)
            if file_path:
                sql += " AND file_path = ?"
                params.append(file_path)
            sql += f" ORDER BY found_at DESC LIMIT {min(limit, 500)}"
            rows = conn.execute(sql, params).fetchall()
            return json.dumps({"status": "ok", "count": len(rows), "results": [dict(r) for r in rows]})

        elif action == "update":
            if not opp_id:
                return json.dumps({"status": "error", "message": "opp_id required"})
            conn.execute("UPDATE opportunities SET status = ? WHERE id = ?", (new_status or "open", opp_id))
            conn.commit()
            return json.dumps({"status": "ok", "id": opp_id, "new_status": new_status})

        elif action == "delete":
            if not opp_id:
                return json.dumps({"status": "error", "message": "opp_id required"})
            conn.execute("DELETE FROM opportunities WHERE id = ?", (opp_id,))
            conn.commit()
            return json.dumps({"status": "ok", "id": opp_id, "deleted": True})

        else:
            return json.dumps({"status": "error", "message": f"Unknown action '{action}'."})

    except Exception as e:
        logger.error("index_opportunity failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# BUILD_TOOLS entry point
# ═══════════════════════════════════════════════════════════════════════════════

def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the five code-index tools."""

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({
        "index_lookup": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["symbol", "file", "feature", "route", "component", "imports", "opportunity", "comments", "summary"]},
                "query": {"type": "string"},
                "kind": {"type": "string", "enum": ["function", "class", "method", "variable", "route", "component"]},
                "feature": {"type": "string"},
                "file_path": {"type": "string"},
                "limit": {"type": "integer"},
                "category": {"type": "string"},
                "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "status": {"type": "string", "enum": ["open", "resolved", "dismissed"]},
                "comment_status": {
                    "type": "string",
                    "enum": ["compliant", "missing", "malformed", "stale", "exempt", "not_checked", "not_applicable"],
                },
                "marker": {
                    "type": "string",
                    "enum": ["REMOVE-WHEN", "DEACTIVATED (INTENTIONAL)", "DEACTIVATED (ORPHANED)", "KEEP (INTENTIONAL)", "SISTER-PANEL", "COLOR SCHEME"],
                },
                "header_style": {
                    "type": "string",
                    "enum": ["line", "block", "jsdoc", "html", "hash"],
                },
            },
            "required": ["action"],
        },
        "index_store": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["file", "delete"]},
                "file_path": {"type": "string"},
                "language": {"type": "string"},
                "source": {"type": "string"},
                "summary": {"type": "string"},
                "symbols": {"type": "array", "items": {"type": "object"}},
                "imports": {"type": "array", "items": {"type": "object"}},
                "api_routes": {"type": "array", "items": {"type": "object"}},
                "ui_components": {"type": "array", "items": {"type": "object"}},
                "features": {"type": "array", "items": {"type": "object"}},
                "replace": {"type": "boolean"},
            },
            "required": ["action", "file_path"],
        },
        "index_progress": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "get", "start", "update", "pending"]},
                "batch_id": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "error"]},
                "file_count": {"type": "integer"},
                "files": {"type": "array", "items": {"type": "string"}},
                "agent_id": {"type": "string"},
                "error": {"type": "string"},
            },
            "required": ["action"],
        },
        "index_features": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "register", "delete"]},
                "tag": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["action"],
        },
        "index_opportunity": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["log", "list", "update", "delete"]},
                "file_path": {"type": "string"},
                "category": {"type": "string"},
                "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "description": {"type": "string"},
                "suggestion": {"type": "string"},
                "opp_id": {"type": "integer"},
                "new_status": {"type": "string", "enum": ["open", "resolved", "dismissed"]},
                "status": {"type": "string", "enum": ["open", "resolved", "dismissed"]},
                "limit": {"type": "integer"},
            },
            "required": ["action"],
        },
    })

    DESTRUCTIVE.clear()
    DESTRUCTIVE.update({"index_store", "index_progress", "index_features", "index_opportunity"})

    return {
        "index_lookup": _index_lookup,
        "index_store": _index_store,
        "index_progress": _index_progress,
        "index_features": _index_features,
        "index_opportunity": _index_opportunity,
    }

"""
Dialect-specific DDL renderers.

Translates the canonical Table/Column/Index/FtsTable structures into
CREATE TABLE / CREATE INDEX statements for SQLite, Postgres, or MySQL.

Type translation table (canonical → dialect):
    TEXT       → TEXT (sqlite), TEXT (postgres), TEXT (mysql)
    INTEGER    → INTEGER, INTEGER, INT
    REAL       → REAL, DOUBLE PRECISION, DOUBLE
    BLOB       → BLOB, BYTEA, BLOB
    TIMESTAMP  → TEXT, TIMESTAMPTZ, TIMESTAMP
    JSON       → TEXT, JSONB, JSON

Default translation:
    CURRENT_TIMESTAMP → datetime('now') (sqlite), now() (pg/mysql)
"""

from typing import List
from app.db.schema.tables import (
    Table, Column, Index, FtsTable, Trigger,
    TABLES, INDEXES, FTS_TABLES, TRIGGERS,
)


DIALECTS = ("sqlite", "postgres", "mysql")


_TYPE_MAP = {
    "sqlite": {
        "TEXT": "TEXT", "INTEGER": "INTEGER", "REAL": "REAL",
        "BLOB": "BLOB", "TIMESTAMP": "TEXT", "JSON": "TEXT", "VECTOR": "BLOB",
    },
    # NB: TIMESTAMP and JSON map to TEXT on Postgres (not TIMESTAMPTZ/JSONB).
    # The Postgres backend reuses the SQLite-dialect backend code, which stores
    # and reads these as ISO strings / json.dumps text. Keeping them TEXT gives
    # exact behavioural parity (psycopg would otherwise hand back datetime/dict
    # objects and break json.loads / lexicographic time comparisons). VECTOR is
    # the one native type (pgvector) — embeddings are handled by dedicated code.
    "postgres": {
        "TEXT": "TEXT", "INTEGER": "INTEGER", "REAL": "DOUBLE PRECISION",
        "BLOB": "BYTEA", "TIMESTAMP": "TEXT", "JSON": "TEXT", "VECTOR": "vector",
    },
    "mysql": {
        "TEXT": "TEXT", "INTEGER": "INT", "REAL": "DOUBLE",
        "BLOB": "BLOB", "TIMESTAMP": "TIMESTAMP", "JSON": "JSON", "VECTOR": "BLOB",
    },
}


def _render_default(default: str, dialect: str) -> str:
    if default == "CURRENT_TIMESTAMP":
        if dialect == "sqlite":
            return "(datetime('now'))"
        if dialect == "postgres":
            # Timestamp columns are TEXT on Postgres (parity with SQLite). Emit a
            # text value in the exact same format SQLite's datetime('now') uses
            # ('YYYY-MM-DD HH:MM:SS', UTC) so default-populated timestamps sort and
            # compare identically across both backends.
            return "(to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'))"
        return "CURRENT_TIMESTAMP"
    return default


def _render_column(c: Column, dialect: str) -> str:
    col_type = _TYPE_MAP[dialect][c.type]
    # pgvector: render the dimensionality, e.g. vector(1536). Other dialects
    # store the raw float32 bytes as BLOB, so no dimension is emitted.
    if c.type == "VECTOR" and dialect == "postgres" and c.vector_dim:
        col_type = f"vector({c.vector_dim})"
    parts = [c.name, col_type]
    # MySQL: TEXT columns cannot have a literal DEFAULT — strip in that case
    has_default = c.default is not None
    if dialect == "mysql" and c.type == "TEXT" and has_default:
        has_default = False  # silently drop; renderer NB: schema impacted only on mysql

    # Primary key handled inline only for single-PK case; composites via constraints
    if c.primary_key:
        parts.append("PRIMARY KEY")
    if not c.nullable and not c.primary_key:
        parts.append("NOT NULL")
    if c.unique:
        parts.append("UNIQUE")
    if has_default:
        parts.append(f"DEFAULT {_render_default(c.default, dialect)}")
    if c.references:
        ref = f"REFERENCES {c.references}"
        if c.on_delete:
            ref += f" ON DELETE {c.on_delete}"
        parts.append(ref)
    return " ".join(parts)


def _render_table(t: Table, dialect: str) -> str:
    col_lines = [f"    {_render_column(c, dialect)}" for c in t.columns]
    for con in t.constraints:
        col_lines.append(f"    {con}")
    body = ",\n".join(col_lines)
    return f"CREATE TABLE IF NOT EXISTS {t.name} (\n{body}\n);"


def _render_index(idx: Index, dialect: str) -> str:
    unique = "UNIQUE " if idx.unique else ""
    cols = idx.columns
    # SQLite tolerates COLLATE NOCASE / DESC inline; postgres/mysql do too for basic forms.
    if dialect == "mysql":
        # MySQL requires explicit length on TEXT; skip those indexes that target plain text columns
        # with no length spec. The schema doesn't use any such indexes today; if added later,
        # encode the length in the columns string (e.g. "name(64)").
        pass
    return f"CREATE {unique}INDEX IF NOT EXISTS {idx.name} ON {idx.table}({cols});"


def _render_fts_sqlite(f: FtsTable) -> str:
    cols: List[str] = []
    for c in f.unindexed_columns:
        cols.append(f"{c} UNINDEXED")
    cols.extend(f.indexed_columns)
    cols_sql = ",\n    ".join(cols)
    return (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {f.name} USING fts5(\n"
        f"    {cols_sql},\n"
        f"    content='{f.content_table}',\n"
        f"    content_rowid='rowid',\n"
        f"    tokenize='porter unicode61'\n"
        f");"
    )


def _render_fts_postgres(f: FtsTable) -> str:
    # tsvector column lives on the content table; query layer uses to_tsvector inline.
    # Render as GIN expression index instead of a separate FTS table.
    expr = " || ' ' || ".join(f"coalesce({c}, '')" for c in f.indexed_columns)
    return (
        f"-- {f.name} (postgres GIN FTS index on {f.content_table})\n"
        f"CREATE INDEX IF NOT EXISTS {f.name}_gin ON {f.content_table}\n"
        f"    USING GIN (to_tsvector('english', {expr}));"
    )


def _render_fts_mysql(f: FtsTable) -> str:
    cols = ", ".join(f.indexed_columns)
    return (
        f"-- {f.name} (mysql FULLTEXT index on {f.content_table})\n"
        f"CREATE FULLTEXT INDEX {f.name}_ft ON {f.content_table}({cols});"
    )


def _render_trigger_sqlite(t: Trigger) -> str:
    return f"CREATE TRIGGER IF NOT EXISTS {t.name}\n{t.body.strip()};"


def _sort_tables_by_deps(tables: List[Table]) -> List[Table]:
    """
    Order tables so every foreign-key target is created before the table that
    references it. SQLite tolerates any order (FK targets are resolved lazily),
    but Postgres/MySQL require the referenced table to already exist at
    CREATE TABLE time. The canonical TABLES list is authoring-ordered, not
    dependency-ordered, so we topologically sort here.

    Self-references and references to tables outside this list are ignored.
    Stable: preserves original order among independent tables. Falls back to
    original order if a cycle is detected (none exist today).
    """
    by_name = {t.name: t for t in tables}
    deps = {
        t.name: {
            c.references.split("(")[0]
            for c in t.columns
            if c.references and c.references.split("(")[0] in by_name and c.references.split("(")[0] != t.name
        }
        for t in tables
    }
    ordered: List[Table] = []
    placed = set()
    remaining = [t.name for t in tables]  # preserves authoring order
    while remaining:
        progressed = False
        next_remaining = []
        for name in remaining:
            if deps[name] <= placed:
                ordered.append(by_name[name])
                placed.add(name)
                progressed = True
            else:
                next_remaining.append(name)
        remaining = next_remaining
        if not progressed:
            # Cycle / unresolvable — emit the rest in original order.
            ordered.extend(by_name[n] for n in remaining)
            break
    return ordered


def _render_all(dialect: str) -> str:
    parts: List[str] = []
    parts.append(f"-- webAgent canonical schema — dialect: {dialect}\n")

    for t in _sort_tables_by_deps(TABLES):
        parts.append(_render_table(t, dialect))

    for idx in INDEXES:
        parts.append(_render_index(idx, dialect))

    for f in FTS_TABLES:
        if dialect == "sqlite":
            parts.append(_render_fts_sqlite(f))
        elif dialect == "postgres":
            parts.append(_render_fts_postgres(f))
        elif dialect == "mysql":
            parts.append(_render_fts_mysql(f))

    if dialect == "sqlite":
        for trig in TRIGGERS:
            parts.append(_render_trigger_sqlite(trig))

    return "\n\n".join(parts) + "\n"


def render_sqlite() -> str:
    return _render_all("sqlite")


def render_postgres() -> str:
    return _render_all("postgres")


def render_mysql() -> str:
    return _render_all("mysql")


def render(dialect: str) -> str:
    if dialect not in DIALECTS:
        raise ValueError(f"Unknown dialect: {dialect}. Must be one of {DIALECTS}")
    return _render_all(dialect)

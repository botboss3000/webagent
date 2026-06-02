the system_prompt is hardcoded to use the system_prompt.md as fallback. the priority prompt is loaded through the database.

`local.db` is SQLite and is the **zero-config default** (and the shipped seed DB). The backend is pluggable: `get_db()` returns a `LocalBackend` (SQLite), a `SupabaseBackend` (Postgres over REST), or a **`PostgresBackend`** (raw Postgres via `psycopg`). All three implement the same `StorageBackend` interface.

## Pluggable backends + the Postgres translation layer

`PostgresBackend` (in `postgres_backend.py`) is a **subclass of `LocalBackend`** that overrides only the connection and schema bootstrap. Every data method is inherited and runs through **`pg_portable.py`** — a `sqlite3`-compatible facade over a pooled `psycopg` connection that translates SQLite-dialect SQL to Postgres on the fly (`?`→`%s`, `INSERT OR IGNORE/REPLACE`→`ON CONFLICT`, `datetime('now')`→text timestamp, `IFNULL`→`COALESCE`, `json_each(col)`→`json_array_elements_text(...)`). This means **one codebase serves both stores** — when writing new backend code, keep using `self._get_conn()` + SQLite-dialect SQL and it works on both. Avoid Postgres-only or SQLite-only SQL in shared methods; if a method genuinely needs native features (FTS, embeddings), override it in `PostgresBackend` (see `_fts5_search`, `_vector_search`, `doc_chunk_upsert`).

Parity rules for the Postgres schema (`ddl_renderer.py`): `TIMESTAMP` and `JSON` columns render as **TEXT** (the app stores ISO strings / `json.dumps` text and reads them back as strings — JSONB/timestamptz would hand back parsed objects and break callers). The one native type is **`VECTOR`** (pgvector `vector(1536)`) for embeddings. Tables are emitted in **foreign-key dependency order** (Postgres enforces FK targets at CREATE time; SQLite doesn't).

`PostgresBackend._init_db()` renders the schema, then **reconciles columns** against a throwaway no-seed SQLite reference instance — any column added by a future SQLite `ALTER` migration is auto-added to Postgres, so the two never drift.

Copy data with `python -m app.db.migrate_sqlite_to_pg` (see `migrate_sqlite_to_pg.py`).

**Still SQLite-only even under Postgres:** the optimizer self-improvement subsystem (`app/optimizer/`, `app/tools/optimizer_tools.py` — temp `.db` scratch files). The admin DB Viewer (`app/api/db_viewer.py`) is now backend-aware (routes the main DB to Postgres via `_open()` + a standalone autocommit `PgPortableConnection`; temp `.db` files stay SQLite).

## Agent template + prompt tables (three-way split)

| Table | Job |
|-------|-----|
| **`agent_templates`** | Per-template config (model, temperature, loop_logic, trigger). Seeded from `app/context/agents/*.json`. |
| **`agent_prompt_templates`** | Canonical slot content per template. Each row keyed by `(template_id, slot_name)`. Carries `version` (int, declared in JSON) + `source` (`'json'` \| `'admin'`). JSON re-seed never overwrites `source='admin'` rows. |
| **`agents`** | Per-user agent instances. References a `template_id`. |
| **`agent_prompts`** | Per-agent runtime slot rows: admin-base rows cloned from `agent_prompt_templates` at agent creation, plus optional per-user overrides (`user_id` set). Each cloned row stamps `template_version`. |

## Seeder lifecycle

`_seed_agent_templates_from_json_files` runs at **app boot only** (and on admin "Re-Seed" button click). Manifest hash of the JSON files is stored in `app_meta.last_agent_manifest_hash`; the seeder short-circuits when the hash matches, so boot stays fast.

Per-slot upsert rules:
- Missing → INSERT with `source='json'`, `version=<JSON version>`.
- Existing `source='json'` AND JSON version > DB version → UPDATE + bump.
- Existing `source='admin'` → SKIP unless `force=True`.

The destructive `DELETE` from the old seeder is GONE. Admin edits in `agent_prompt_templates` survive re-seed unless the admin clicks **Force Re-Seed** in the config page.

## Schema additions for this design

- `app/db/schema/tables.py`: `agent_prompt_templates`, `app_meta`, plus `template_version` column on `agent_prompts`.
- `app/db/local.py`: SCHEMA_SQL extended; migrations 025 (column add) + 026 (one-shot data migration from `agent_prompts` admin-base rows → `agent_prompt_templates`).
- `migrations/018_agent_prompt_templates.sql`: Supabase counterpart (Postgres DDL + same one-shot copy).

## Diagnostics table

The **`diagnostics`** table backs the diagnostic flight-recorder (`app/agent/diagnostics.py`) — a rolling, auto-pruned log of server warnings/errors (with tracebacks), agent-loop pipeline problems, run outcomes, and tool errors, read back by the Admin Tools → Diagnostics page, the `GET /api/v1/diagnostics` endpoint, and the `read_diagnostics` agent tool.

- Columns: `id, ts, level, category, source, message, detail (JSON), session_id, turn_id, agent_id, user_id, created_at`. `session_id` / `agent_id` are **plain TEXT (not foreign keys)** so a record outlives the row it referenced and a delete never cascades it away.
- `LocalBackend` methods: `insert_diagnostics_batch` (INSERT OR IGNORE on id), `query_diagnostics` (filtered, newest-first), `prune_diagnostics` (row + age cap). Base-class defaults are no-ops, so a backend that hasn't ported them degrades to **RAM-only** diagnostics (the in-memory ring still serves the live feed).
- Defined in `app/db/schema/tables.py` (canonical) + `app/db/local.py` `SCHEMA_SQL` (local auto-migrate) + `migrations/026_diagnostics.sql` (Supabase).


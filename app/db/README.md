the system_prompt is hardcoded to use the system_prompt.md as fallback. the priority prompt is loaded through the database.

`local.db` is SQLite and is the **zero-config default** (and the shipped seed DB). The backend is pluggable: `get_db()` returns a `LocalBackend` (SQLite), a `SupabaseBackend` (Postgres over REST), or a **`PostgresBackend`** (raw Postgres via `psycopg`). All three implement the same `StorageBackend` interface.

## SQLite write concurrency (tuned for parallel agent writes)

SQLite allows only **one writer at a time** for the whole file, so the design serialises writes two ways and then makes each write commit as fast as possible:

1. **In-process serialisation** — every write method takes `LocalBackend._write_lock` (an `asyncio.Lock`). The app is single-process / single-event-loop, so this funnels all writes through one queue instead of letting many connections collide and throw `database is locked`.
2. **Fast commits** — `_get_conn()` opens each connection with a tuned PRAGMA set: `journal_mode=WAL` (readers never block the writer), **`synchronous=NORMAL`** (the big throughput win — no fsync per commit; crash-safe in WAL, only a hard power-loss can drop the last not-yet-checkpointed transactions), `busy_timeout=30000`, `temp_store=MEMORY`, `cache_size=-16000` (~16 MB), `mmap_size=256MB`, `wal_autocheckpoint=2000`. See the docstring on `_get_conn()` for the rationale per pragma.

Durability trade-off: with `synchronous=NORMAL`, a sudden power loss / OS crash can lose the **last few committed transactions** that hadn't been checkpointed yet — but the database is **never corrupted**. Acceptable for the agent harness (interactions / diagnostics / stream chunks); revisit if a table ever needs hard durability.

## Credentials vault — `vault.db` (always local, separate from user data)

`auth_elements` (per-user service credentials: integration **OAuth tokens** + the **inline secret values** stored by `InlineDBSecrets`, plus the LLM auth row) does **not** live in the user-data `local.db`. It lives in a dedicated sibling file **`data/db/vault.db`**, so a user-data reset never wipes credentials — the app keeps working after a reset.

How it works (SQLite only, zero call-site changes):

- `LocalBackend._get_conn()` **`ATTACH`es** `vault.db` as the `vault` schema on every connection.
- `auth_elements` is **removed from the main SQLite schema** (`SCHEMA_SQL`) and created **only in the vault** (`VAULT_SCHEMA`). With the table absent from `main`, SQLite's main-first name resolution makes every unqualified `… FROM auth_elements` query — in the backend methods, the raw-client query builder (`get_raw_client`), and the encryption migration — resolve transparently to the vault. No per-call-site edits.
- On init, a **one-time migration** copies any legacy `main.auth_elements` rows into the vault (explicit columns + `COALESCE` so NULL-default rows can't be silently dropped) and only **drops the source once every row is confirmed copied** (a partial copy leaves the main table in place — safe fallback, retried next boot).
- The vault path pairs `local.db` → `vault.db`; any other DB file → `<name>.vault.db` (so test / reference instances get isolated vaults).

**Postgres:** the vault is a SQLite-file concept. `PostgresBackend` overrides `_get_conn` (no ATTACH) and `auth_elements` stays a normal table in the Postgres schema (`tables.py` / `render_postgres`). Caveat: a Postgres **schema-drop reset wipes `auth_elements` too** (it's in the dropped schema), and `migrate_sqlite_to_pg` reads `local.db` only, so it currently won't carry `vault.db` rows across — both are follow-ups for Postgres deployments. `vault.db` is a **gitignored runtime artifact**.

## Pluggable backends + the Postgres translation layer

`PostgresBackend` (in `postgres_backend.py`) is a **subclass of `LocalBackend`** that overrides only the connection and schema bootstrap. Every data method is inherited and runs through **`pg_portable.py`** — a `sqlite3`-compatible facade over a pooled `psycopg` connection that translates SQLite-dialect SQL to Postgres on the fly (`?`→`%s`, `INSERT OR IGNORE/REPLACE`→`ON CONFLICT`, `datetime('now')`→text timestamp, `IFNULL`→`COALESCE`, `json_each(col)`→`json_array_elements_text(...)`). This means **one codebase serves both stores** — when writing new backend code, keep using `self._get_conn()` + SQLite-dialect SQL and it works on both. Avoid Postgres-only or SQLite-only SQL in shared methods; if a method genuinely needs native features (FTS, embeddings), override it in `PostgresBackend` (see `_fts5_search`, `_vector_search`, `doc_chunk_upsert`).

Parity rules for the Postgres schema (`ddl_renderer.py`): `TIMESTAMP` and `JSON` columns render as **TEXT** (the app stores ISO strings / `json.dumps` text and reads them back as strings — JSONB/timestamptz would hand back parsed objects and break callers). The one native type is **`VECTOR`** (pgvector `vector(1536)`) for embeddings. Tables are emitted in **foreign-key dependency order** (Postgres enforces FK targets at CREATE time; SQLite doesn't).

`PostgresBackend._init_db()` renders the schema, then **reconciles columns** against a throwaway no-seed SQLite reference instance — any column added by a future SQLite `ALTER` migration is auto-added to Postgres, so the two never drift.

Copy data with `python -m app.db.migrate_sqlite_to_pg` (see `migrate_sqlite_to_pg.py`).

**Still SQLite-only even under Postgres:** the optimizer self-improvement subsystem (`app/optimizer/`, `app/tools/optimizer_tools.py` — temp `.db` scratch files). The admin DB Viewer (`app/api/db_viewer.py`) is now backend-aware (routes the main DB to Postgres via `_open()` + a standalone autocommit `PgPortableConnection`; temp `.db` files stay SQLite).

## Agent template + prompt tables (three-way split)

| Table | Job |
|-------|-----|
| **`agent_templates`** | Per-template config (model, temperature, loop_logic, trigger). Seeded from `data/agents/*.json`. |
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

The `agent_templates` **config row** (not the prompt slots) is always upserted from the JSON, including the `discoverable` flag. So a template's `discoverable: true/false` in `data/agents/*.json` is authoritative and re-asserted on every re-seed — it controls whether the template appears in the Agents page **"Create a new agent"** dropdown (the dropdown queries `discoverable_only=true`). An admin's runtime discoverable toggle therefore holds only until the next re-seed of changed JSON; to permanently hide/show a template, edit its JSON.

## Schema additions for this design

- `app/db/schema/tables.py`: `agent_prompt_templates`, `app_meta`, plus `template_version` column on `agent_prompts`.
- `app/db/local.py`: SCHEMA_SQL extended; migrations 025 (column add) + 026 (one-shot data migration from `agent_prompts` admin-base rows → `agent_prompt_templates`).
- `migrations/018_agent_prompt_templates.sql`: Supabase counterpart (Postgres DDL + same one-shot copy).

## Dedicated logs store — `logs.db` + `recordings.db` (always local)

Operational logs are a high-volume, **per-machine** firehose that nobody reads across instances, so they live in their **own SQLite files** — separate from the main DB (`local.db` *or* remote Postgres) — managed by **`app/db/logs_store.py`** (`get_log_store()`). Two reasons:

1. **No writer contention with user data.** SQLite allows one writer per *file*. Keeping the log firehose out of the file that holds `interactions` means a burst of log writes can never stall a user-facing write, and vice-versa. This is the "split WAL" — each file has its own WAL and its own `asyncio` write lock.
2. **Logs stay local even when the main DB is remote.** You debug one instance at a time; logging belongs on the box that produced it. The store is SQLite-file-based regardless of the main backend.

| File | Tables | Holds |
|------|--------|-------|
| **`data/db/logs.db`** | `diagnostics`, `tool_executions` | Server-side flight recorder + structured tool metrics |
| **`data/db/recordings.db`** | `render_recordings` | Browser-side render recorder (big HTML blobs; off by default) |
| `data/db/instance_id.txt` | — | A per-box id, created once, stamped on every record |

All three are **gitignored runtime artifacts** (recreated on first run). The optimizer's temp `.db` scratch files are a third, pre-existing local split.

### `diagnostics` table (in `logs.db`)

Backs the flight-recorder (`app/agent/diagnostics.py`) — a rolling, auto-pruned log read back by the Admin Tools → Diagnostics page, `GET /api/v1/diagnostics`, and the `read_diagnostics` agent tool.

- **Categories:** `server` (stdlib logs), `http` (4xx/5xx cause), `loop` (pipeline events), `run` / `recovery` (run lifecycle + self-healing), `tool` (tool errors), **`access`** (every HTTP request), **`ws`** (WebSocket connect/subscribe/disconnect).
- **Columns:** `id, ts, level, category, source, message, detail (JSON), session_id, turn_id, agent_id, user_id, interaction_id, session_seq, turn_seq, instance_id, created_at`. The **correlation keys** (`interaction_id` / `session_seq` / `turn_seq`) join a log line to the exact `interactions` row by key (not by fuzzy timestamp); `instance_id` names the box. `session_id` / `agent_id` are **plain TEXT (not foreign keys)**.
- **Capture level:** the stdlib→recorder handler runs at **INFO** by default (`DIAGNOSTICS_CAPTURE_LEVEL`), with a per-logger policy — our `app.*` loggers at INFO+, noisy third-party libs at WARNING+. Records at/above `diagnostics_persist_level` (INFO default) persist to `logs.db`; below stays in the RAM ring + `logs/*.log` text files. Retention defaults: 200k rows / 168h.
- **Store methods:** `insert_diagnostics_batch`, `query_diagnostics`, `prune_diagnostics`, `clear_diagnostics`.

### `tool_executions` table (in `logs.db`)

Structured per-call metrics (revived from `app/tools/tracker.py`): `tool_name, success, duration_ms, error_type, error_message, input_params, output_preview` + the correlation keys. Filled automatically by the loop tap (pairs each `tool_call` with its `tool_result` and times it) or explicitly via `app.tools.tracker.track_execution(...)`. Sortable/aggregatable by duration & success — unlike the free-text `tool` diagnostics category.

### Correlating logs to interactions

Both stores are local SQLite, so the reader can **`ATTACH`** `logs.db` to the main connection and join `diagnostics`/`tool_executions` to `interactions` on `interaction_id` (or `session_seq`/`turn_seq`). On a single-box device both files share one clock, so timestamp alignment is also reliable — but the keys are the robust join.

> **Note:** the legacy `diagnostics` / `render_recordings` tables still defined in `schema/tables.py` + `local.py` `SCHEMA_SQL` are now **dormant** (the live recorders write to the dedicated store instead). They are left in place to avoid disturbing the Postgres reconciliation path; existing rows simply age out.


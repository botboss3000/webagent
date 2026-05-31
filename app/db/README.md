the system_prompt is hardcoded to use the system_prompt.md as fallback. the priority prompt is loaded through the database.

local.db is SQL_lite and used for local development. final direction is to use supabase, so all coding solutions need to be optimized for future supabase impelemntation.

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


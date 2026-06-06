# `app/admin/` — Platform administration & management

This directory holds the **admin/management surfaces** of the app: provider settings, integration/OAuth configuration, database mode, users, and the admin-config endpoints for the various subsystems. These are core management plumbing the rest of the app calls into — not droppable per-edition capabilities.

> **Moved out:** the **privileged filesystem / source-control / UI-edit tools** (the code-editing, shell, git, and UI-Admin tools) now live in **[`plugins/admin/`](../../plugins/admin/README.md)**, gated by the **Codebase Admin**, **Git Control**, and **UI Admin** abilities. If you're looking for `source_tools.py`, `source.py`, `guardrails.py`, `ui_tools.py`, or `ui_guardrails.py`, they are there.

**Removable by design.** Boot does not hard-depend on this folder — `app/main.py` imports the startup provider-config applier from `app/provider_boot.py`, and every admin router is registered behind an `ImportError` guard. Delete a file (or the folder) and the matching `/admin/*` endpoints simply disappear.

## What's here

| File | Purpose |
|------|---------|
| **`settings.py`** | Provider configuration — AI provider / API key / model selection, metadata-logging toggle, provider presets, model fetching. |
| **`integrations.py`** | Admin enable/disable of integrations + abilities, OAuth credential config and redirect handling, `gather_enabled_providers` inputs, default-ability seeding. |
| **`db_mode.py`** | Toggle between Cloud (Supabase) and Local (SQLite) database backends (`/admin/db/`). |
| **`users.py`** | User administration endpoints. |
| **`storage.py`** | Storage backend admin. |
| **`communications.py`** | Enable/disable communication plugins (Telegram, WhatsApp) and set webhook URLs. |
| **`events_admin.py`** | Admin config for the events subsystem. |
| **`scheduler_config.py`** | Admin config for the scheduler providers. |
| **`webhooks_admin.py`** | Webhook administration endpoints. |
| **`remote_access.py`** | Remote-access / tunnel configuration. |
| **`optimizer.py`** | Prompt-optimizer admin pipeline support. |
| **`review.py`** | List/view/deprecate DB-defined tools (`/admin/tools`). |

## See also

- **[`plugins/admin/`](../../plugins/admin/README.md)** — the privileged filesystem/source/git/UI-edit tools (moved here from this folder).
- The `create_tool` lockout (in `app/tools/registry.py` + `app/tools/loader.py`) — still required to prevent privileged tools being re-created at runtime; documented in `plugins/admin/README.md`.

This directory stores configurable aspects of the webAgent platform as JSON files.

Each file mirrors a section of the Admin Tools → App Configuration UI.
All settings here are readable by the platform and can be edited by any tool or agent.

## This folder is created lazily — the app ships with no `data/` folder

Nothing here is required at boot. Every file in this folder **materializes only
when a setting first changes**, written through `app/util/config_io.py` (which
creates `data/config/` on demand and writes atomically). With the folder absent,
every reader falls back to a built-in default, so the app boots clean. As a
result these files are **runtime state, not committed seeds** — most are
gitignored (see the root `.gitignore`), and an admin's live edits never collide
with `git pull`.

Read-only *defaults* that the app genuinely needs at boot are **not** here — they
ship under `app/defaults/` instead (`app-prompts.json` and the `agents/` template
seeds), resolved via `app/util/paths.py`.

## FILE CATALOG

| File | Source section | Configures |
|------|---------------|------------|
| `app-settings.json` | App Settings | Global feature flags (extend LLM to agents, stream buffer, watchdog, feedback, turnstile, run limits, global system prompt) |
| `chat_ui.json` | (no UI — edit file) | App-wide chat-panel messages plus desktop, mobile, and widget layout/presentation defaults. Per-agent overrides remain in agent metadata. |
| `debug-config.json` | (no UI — edit file) | **Single override file for every debugging knob.** Every knob defaults to `null` = "leave it to the normal source" (env var, App Settings UI, or built-in default); set a value to **force** it. Owned by `app/admin/debug_config.py`. Groups: `logging`, `run_limits`, `recorders`. |
| `provider.json` | Agent Settings > Models | Per-user LLM provider configs (provider, base_url, api_key, model, parallel providers, capabilities). The only file that may hold plaintext keys — migrate to the vault for production. |
| `model_catalog.json` | Agent Settings > Models | Saved model catalog with per-model capabilities, token pricing |
| `agent-abilities.json` | Agent Settings > Agent Tools | Admin-level **ability on/off toggles** (`abilities`), the ability table's **display order** (`order`), global per-tool defaults (`tools` — auto/ask/deny + send/discover), and non-secret ability config knobs (`ability_config`). Catalog-driven; seeded on first boot from the prior vault state. **No secrets** — those stay in the vault. Owned by `app/admin/ability_config.py` |
| `main-panel-pages.json` / `admin-panel-pages.json` | App Settings > Main Panel / Admin Panel | Per-page overrides (display order, renamed label, custom icon, 3-state visibility) for the header tabs and Admin Tools views. Owned by `app/admin/page_config.py` |
| `optimizer.json` | Optimizer Runs | Optimizer on/off (`mode`) + run condition, criteria, models, notifications. Owned by `app/optimizer/config.py` |
| `scheduler_config.json` | Automation | Scheduler backend choice + per-provider settings. Owned by `app/admin/scheduler_config.py` |
| `remote_access.json` | Remote Access | Tunnel method (ngrok/Cloudflare/Tailscale/manual), auto-start, signpost |
| `suggestions.json` | Suggestions | AI message suggestion mode, count, idle threshold |

> **Removed legacy files.** `admin-ui.json`, `users.json`, `integrations.json`,
> `channels.json`, `storage.json`, `scheduler.json`, `event-sources.json`, and
> `git-providers.json` were leftovers from before the plugin/page refactors — they
> had no code reader and were deleted. Page visibility/order moved to
> `main-panel-pages.json` / `admin-panel-pages.json`; integrations/channels/storage
> moved to the vault + drop-in plugin system; the scheduler uses
> `scheduler_config.json`.

> **Moved out of `data/`.** `app-prompts.json` (the system prompt catalog,
> served via `GET /api/v1/app-prompts[/{section}]`) now lives at
> `app/defaults/app-prompts.json` so it always ships. Its per-section meaning is
> unchanged; see `app/api/features.py` and `app/util/paths.py`.

> **Chat UI copy moved to `chat_ui.json`.** Welcome/system bubbles, composer
> placeholders, and widget presentation defaults now live with the chat layout
> they describe rather than in the system-prompt catalog.

## Naming convention
- Each file maps 1:1 to a section/tab in the Admin Configuration UI
- Secrets (API keys, passwords) are referenced by key name but stored in the secrets vault
- `provider.json` is the only file that may contain plaintext keys — migrate to vault for production

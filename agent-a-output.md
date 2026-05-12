# Agent A: Multi-Provider Config Storage — Complete

## Changes made

### 1. `app/admin/settings.py`

**New Pydantic models:**
- `MultiProviderEntry` — single parallel provider entry (provider, base_url, api_key, model)
- `MultiProvidersRequest` — request body for multi-providers endpoints (parallel_mode + list of entries)

**Updated `DEFAULT_PROVIDER`** — now includes `parallel_mode: False` and `multi_providers: []` keys.

**Updated `_apply_config_to_env()`** — now sets two additional env vars:
- `PARALLEL_MODE` → `"true"` only if `parallel_mode` is true AND there are 2+ providers
- `MULTI_PROVIDERS` → JSON string of cleaned provider entries (each with provider, base_url, api_key, model)
- Strips these from env when parallel mode is off

**New endpoints:**
- `GET /admin/settings/multi-providers` — Returns `{parallel_mode, providers[]}` for the requesting user. Reads from DB auth_elements first, then provider.json. Masks API keys in response (shows first 8 + last 4 chars).
- `POST /admin/settings/multi-providers` — Accepts `{parallel_mode, providers[]}`. Saves to both DB auth_elements and provider.json. Mirrors first provider's key up to root fields for backward compat. Returns `{status, mode (parallel/single), count, message}`.

### 2. `app/agent/loop.py`

**New function `_get_multi_clients()`:**
- Reads `PARALLEL_MODE` env var — returns `[]` if not `"true"`
- Reads `MULTI_PROVIDERS` env var — parses JSON, returns `[]` if parse fails
- Creates a fresh `AsyncOpenAI` client per provider (timeout=60.0)
- Returns list of `(provider_name, client)` tuples
- Gracefully skips entries with missing base_url or api_key
- Falls back to compat shim (`app.openai_compat.AsyncOpenAI`) if real openai not importable

### 3. `README.md`

Updated in 4 places:
- **Features** — added bullet for parallel multi-provider
- **Environment variables** — added PARALLEL_MODE and MULTI_PROVIDERS (internal, dynamic)
- **Provider config paragraph** — added description of multi-provider parallel mode
- **Admin endpoints table** — added multi-providers to settings.py row

## Design decisions

| Decision | Rationale |
|----------|-----------|
| env vars for race engine | Agent B (race logic) reads env vars → no coupling to settings module |
| env var set during `_apply_config_to_env` | Same point where all other provider config is set → consistent |
| mask API keys in GET response | Security — frontend only needs to know if key exists, not the full key |
| only 2+ providers activates parallel mode | Single-entry parallel makes no sense; falls back to single path |
| each provider gets own httpx client | Prevents cross-contamination of auth tokens, timeouts |
| backwards-compat root fields | Existing single-provider code paths still work unchanged |

## Not touched

- `stream_agent_events()` — Agent B handles race logic
- `ui/js/settings.js` — Agent C handles frontend UI
- `index.html` — Agent C handles modal HTML
- `provider.json` migration — old flat format migration already exists

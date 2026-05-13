# webAgent

A **FastAPI** service with a **tool-calling** LLM agent (OpenRouter), optional **Supabase** or **local SQLite** persistence, and a **vanilla JS** UI at **`/index.html`** (static assets under `/ui/`).

## Features

- **Chat** — `POST /api/v1/chat` (buffered) and **`POST /api/v1/chat/stream`** (SSE streaming): agent loop with tools; turns go to **`interactions`**. Prior turns for the same **`session_id`** are reloaded from the DB into the model context (browser refresh does not reset the conversation).
- **WebSocket agent (receive-only)** — `GET` upgrade to `/api/v1/agent/ws`: per-user subscriber mode. Connects once per user, receives ALL agent events (stream, response, tool_call, tool_result, pipeline, db) for all of that user's sessions. **Does not send messages** — all user messages go through HTTP POST.
- **Context** — Prompt slices from `context_type` / `doc_type`; if a user has no rows, **`context_templates`** are copied into per-user context on first chat.
- **Memory** — Hybrid search (FTS5 keyword + vector cosine similarity via embedding API) runs before each chat turn; results injected as `[BRAIN CONTEXT]` in the system prompt. Trivial messages (greetings, affirmations, commands) skip memory via regex gate. Page content auto-chunked and embedded on write. Background save of chat snippets into memory. See [`memory-upgrade.md`](memory-upgrade.md).
- **AutoAgent** — Visual creative coding tab 🎨. Send prompts to the "UI Agent" persona and get live-rendered p5.js sketches in an iframe. Supports generative art, particle systems, noise fields, interactive sketches, and more. Powered by `render_visual` tool in **`app/visualizer/`**. The p5.js skill is seeded as a `context_templates` row (`context_type="p5js"`). Output served from `/visuals/` (ephemeral, Cloud Run safe). See [`app/visualizer/SKILL.md`](app/visualizer/SKILL.md).
- **Attachments** — Image, audio, video, and file uploads. Users attach files via the UI (📎 button in footer, drag & drop onto chat messages or footer area, 🎤 voice recording). Files upload via **`POST /api/v1/upload`** and bytes are persisted through **`app/db/attachments/`** (local filesystem in dev, Supabase Storage in production — see `app/db/SUPABASE_STORAGE.md`). Metadata is stored in the **`attachments`** table (local SQLite or Supabase). The agent accesses files with the **`read_attachment`** built-in tool. Supports image preview, audio/video players, and download links inline in chat bubbles. Attachments persist per-session and survive server restarts.
- **Tools** — **Bootstrap + on-demand discovery model.** A small set of hardcoded core tools (list_tools, search_tools, get_tool_definition, web_search, http_request, browser_action, db_query, memory, session_search, get_time, get_date, get_weather, calculate, read_attachment) are always available from turn 1 via **`app/tools/loader.py`** + **`app/tools/core_tools.py`**. All other tools (user-created, admin, comm plugins, webhook management) are discovered on demand via `list_tools` / `search_tools` / `get_tool_definition`. Tool definitions no longer auto-populate the system prompt — only curated `context_type="skills"` docs provide behavioral guidance in the `# [SKILLS]` section.
- **OpenRouter** — Model from `OPENROUTER_MODEL` (see `.env.example`; e.g. `deepseek/deepseek-v4-flash`).
- **Parallel multi-provider** — Configure 2+ LLM providers in Settings. When enabled, the agent fans out each message to all providers simultaneously and uses the fastest complete response. Configured via `GET/POST /admin/settings/multi-providers`. Set `parallel_mode: true` and a list of provider entries (each with provider, base_url, api_key, model) in `provider.json` or DB `auth_elements`.
- **Dual storage** — **`cloud`** (Supabase) vs **`local`** (SQLite file **`app/db/local.db`**). Mode is stored in **`app/db_mode.json`** and switched via **`/admin/db/*`**.
- **Administrator tools** — Optional filesystem read/write/edit/delete, shell command execution, and server restart exposed as agent tools (**`read_source`**, **`write_source`**, **`edit_source`**, **`delete_source`**, **`run_command`**, **`restart_server`**). Powered by **`app/admin/source.py`** + **`app/admin/source_tools.py`**. **These are privileged debug tools — NOT available in normal user operation.** Deleting the `app/admin/` directory removes them entirely. See the [Administrator Tools](#administrator-tools) section.
- **Web UI** — Main page at **`/index.html`** (chat, DB viewer, terminal, stream/loop). **`/terminal`** redirects to **`/index.html`**.
- **Minimal tester** — **`GET /test`** serves **`ui/test_interface.html`** (same origin as the API).

## Architecture and module map

### HTTP + WebSocket Architecture

The agent loop (`app/agent/loop.py`) runs as an independent async task. All user messages enter via **HTTP POST** (no WebSocket sends). The WebSocket is a **receive-only subscriber** — connected once per user, it receives events for all of that user's sessions.

```text
       [CLIENT]
          |
          |--- (HTTP POST) ---> [ app/api/chat.py ]   send message + wait for SSE stream
          |                           |
          |                    +-- POST /api/v1/chat         (buffered, returns final reply)
          |                    +-- POST /api/v1/chat/stream  (SSE, streams tokens live)
          |                    +-- POST /api/v1/chat/interrupt  (stop generation)
          |                           |
          |                    [ app/api/uploads.py ]        POST /api/v1/upload
          |                    store_file()  → bytes saved + attachment row
          |                           |
          |                           v
          |              +--------------------------+
          |              | ONE UNIFIED ENGINE       |
          |              | (app/agent/loop.py)      |
          |              +--------------------------+
          |                           |
          |              events emitted to:
          |              ├── SSE response body     (primary chat bubble display)
          |              └── Listener registries:
          |                    ├── _visualizer_listeners[session_id]   (per-session WS)
          |                    └── _user_listeners[user_id]            (per-user WS)
          |                           |
          |                           v
          |--- (WS receive-only) -> [ app/api/agent.py ]   /api/v1/agent/ws
                                      mode: "user_subscriber"
                                      receives: stream, response, tool_call,
                                                tool_result, pipeline, db events
                                      for ALL sessions belonging to user

Events are routed on the frontend:
  - "stream" / "response" events for the CURRENT session → chat bubble
  - "tool_call" / "tool_result" / "pipeline" / "db" events → stream/loop/flow debug panels
```

### Backend (`app/`)

| Module | Role |
|--------|------|
| **`main.py`** | FastAPI app: routers, CORS, no-cache for `/ui/` and `/index.html`, **`StaticFiles`** for `/ui/` and `/screenshots`, **`GET /` → redirect to `/index.html`**, **`GET /index.html`**, **`GET /test`**, **`GET /health`**, favicon from `ui/favicon.svg`, **`POST /api/v1/restart`**, shutdown (browser + terminal). |
| **`api/chat.py`** | **`POST /api/v1/chat`** (buffered), **`POST /api/v1/chat/stream`** (SSE), **`POST /api/v1/chat/interrupt`** — context load, memory search, prompt build, attachment resolution, history rebuild, agent loop execution. Also: **listener registries** — `register_user_listener()` / `register_visualizer_listener()` for per-user and per-session WebSocket broadcasting. |
| **`api/agent.py`** | **`WebSocket /api/v1/agent/ws`** — **receive-only per-user subscriber**. Client sends `{"mode": "user_subscriber", "user_id": "..."}` to register. Server streams all agent events (stream, response, tool_call, tool_result, pipeline, db) for all of that user's sessions. No message processing — all sends go through HTTP POST. |
| **`api/uploads.py`** | **`POST /api/v1/upload`** — multipart file upload (images, audio, video, PDF, text). **`GET /api/v1/upload/{id}`** — metadata lookup. **`DELETE /api/v1/upload/{id}`** — delete. File bytes stored via `app/db/attachments/`. |
| **`api/terminal.py`** | **`WebSocket /api/v1/terminal/ws`** — browser shell (PTY / **`pywinpty`** on Windows). |
| **`api/webhooks.py`** | **`POST /api/v1/webhooks/{plugin_name}`** — communication channel webhooks (Telegram, WhatsApp, etc.). Delegates to plugins for auth and parsing. |
| **`api/webhooks_generic.py`** | **`POST /api/v1/webhooks/generic/{webhook_id}`** — generic inbound webhooks. Receives any external payload, routes to agent loop with custom instructions, returns agent reply. Logs all events for review. |
| **`api/db_viewer.py`** | **`/api/v1/db/*`** — SQLite introspection; DB files under **`app/db/`** (default filename **`local.db`** for the UI query param `db=`). **`GET /api/v1/db/session-stats`** — aggregated per-session usage stats (tokens, duration, cost, turn count). |
| **`agent/loop.py`** | Unified multi-turn loop (streaming + buffered): tool validation, parallel tool runs, pipeline events. Emits `attachment` event type for frontend file rendering. |
| **`agent/session_history.py`** | Maps **`interactions`** rows → OpenAI-style **`messages`** for the active session (excludes internal memory tools). |
| **`agent/prompts.py`** | System prompt from context, brain results, tools, attachment context. Includes **`format_attachments_for_prompt()`** helper. |
| **`agent/error_classifier.py`** | Structured tool errors (**used on the WebSocket / streaming path**). |
| **`context/agents/`** | **Agent template JSON files** — seed `agent_templates` table with full schema (id, system_prompt, max_turn_count, model, provider, temperature, max_tokens, metadata). Each `.json` file defines one agent template. Default: `default.json`, `optimizer-planner.json`, `optimizer-finalizer.json`. Scanned on first agent creation for a user. |
| **`context/context_templates/`** | **Context template .md files** — seed `context_templates` table per context_type (agent, user, skills, tools, tasks, memory, project, jobs). Copied to user context on first chat. |
| **`agent/embed.py`** | Embedding utility using same provider config as chat. Returns configurable-dimension vectors (`EMBED_DIM`, default 1536). |
| **`db/__init__.py`** | **`get_db()`** → **`SupabaseBackend`** or **`LocalBackend`** from persisted mode. |
| **`db/supabase.py`** | Cloud: **`sessions`**, **`interactions`**, **`context`**, **`context_templates`**, **`attachments`**, memories / tools / skills per shared schema. |
| **`db/local.py`** | Local SQLite — schema init, FTS5 + vector hybrid search, embed-on-write, knowledge graph, timelines, **`webhook_registrations`** and **`webhook_event_log`** tables. |
| **`db/attachments/`** | **`file_store.py`** — file byte storage abstraction. Dispatches to local filesystem (`uploads/`) or Supabase Storage based on `db_mode.json`. Exports `store_file()`, `read_file()`, `delete_file()`. See `app/db/SUPABASE_STORAGE.md` for cloud setup. |
| **`db/interface.py`** | **`StorageBackend`** protocol with session, interaction, context, memory, skills, agent, attachment, interrupt, **webhook (register/get/list/delete/log)** abstract methods. |
| **`tools/`** | **`loader`** (dynamic tool loading + built-in injection: http_request, register_webhook, list_webhooks, delete_webhook, get_webhook_log, render_visual), **`core_tools`** (bootstrap tools: list_tools, search_tools, get_tool_definition, web_search, http_request, db_query, memory, session_search, get_time, get_date, get_weather, calculate), **`registry`** (create_tool, safety scanner, rating utilities), **`tracker`** (legacy execution tracker), **`browser`** (persistent Chromium), **`read_attachment**` (read uploaded files via `app/db/attachments/`). |
| **`visualizer/`** | **`render_visual` tool** — saves p5.js HTML output to `/visuals/<session_id>/render.html` for the AutoAgent tab iframe. **`SKILL.md`** — p5.js creative coding skill (seeded as `context_templates` row). Self-contained — delete to disable. |
| **`models/schemas.py`** | Pydantic models (`ChatRequest`, etc.). |
| **`admin/`** | **`review`** (`/admin/tools` — list/deprecate DB tools), **`db_mode`** (`/admin/db/` — cloud/local switch), **`settings`** (provider config, model list, metadata toggle), **`guardrails`** (path/command deny-list for source tools), **`communications`** (Telegram/WhatsApp plugin mgmt), **`source`** + **`source_tools`** (optional privileged filesystem & shell access — delete to disable). See [Administrator Tools](#administrator-tools). |
| **`openai_compat.py`** | OpenAI-compatible client wiring for OpenRouter. |

### Frontend (`ui/`)

Single-page app: **`index.html`**, CSS (`app1.css`, `app2.css`, `app3.css`, `loop.css`, `loop-visual.css`, `autoagent.css`), ES modules under **`js/`** — e.g. **`main.js`**, **`chat.js`** (sends messages via HTTP POST + SSE), **`agentWs.js`** (per-user receive-only WebSocket subscriber), **`stream.js`**, **`loop.js`**, **`tabs.js`**, **`toolLog.js`**, **`terminal.js`**, **`dbMode.js`**, **`sessions.js`**, **`attachments.js`** (file upload, voice recording, drag & drop, preview chips), **`autoagent.js`** (visualizer tab: iframe renderer, prompt bar, render_visual event listener), **`optimizer.js`** (session manager + optimizer config UI), **`js/db/`** (data browser). **`test_interface.html`** is also here and is served at **`GET /test`**.

### Directory tree (abbreviated)

```
webAgent/
├── app/                    # Python package (see table above)
│   ├── visualizer/         # AutoAgent p5.js creative coding (render_visual tool + SKILL.md)
│   └── db/
│       └── attachments/    # File storage abstraction (store_file / read_file / delete_file)
├── tests/                  # e.g. test_session_history.py (unittest)
├── ui/                     # Static UI + test_interface.html
├── uploads/                # User-uploaded files (images, voice, docs; mounted at /uploads)
├── scripts/
│   ├── start_webAgent.sh            # Unix: cd to repo root, background uvicorn (default :8080, PORT= overrides)
│   ├── backfill_embeddings.py       # One-off: embed existing memory pages
│   └── seed_tools.py                # Optional tool DB seeding
├── migrations/             # Ad-hoc SQL snapshots (includes 007_channel_identities, 008_linking_codes); see migrations/README.md
├── supabase/migrations/    # e.g. 005_memory_system.sql (Supabase CLI / team workflow)
├── screenshots/            # Mounted at /screenshots
├── android/                # Optional Android wrapper (Java + embedded Python)
├── tasks/                  # Small Node helper (package.json, run-all.ts)
├── temp/                   # Scratch files incl. Markdown drafts (see agent.md); roadmap: temp/FUTURE_PLANS.md
├── .github/workflows/      # CI (e.g. APK build)
├── webAgent.bat            # Windows: uvicorn loop + restart support (uses run.py)
├── run.py                  # Pre-opens port with SO_REUSEADDR for zombie-port resilience
├── uploads/                # Uploaded attachments directory (auto-created, gitignored)
├── Dockerfile
├── requirements.txt
├── pyproject.toml
├── uv.lock
├── .env.example
├── agent.md                # Instructions for assistants working in this repo
└── README.md
```

## Environment variables

Copy **`.env.example`** to **`.env`**:

```bash
cp .env.example .env          # Windows (cmd): copy .env.example .env
```

| Variable | Purpose |
|----------|---------|
| `OPENROUTER_API_KEY` | OpenRouter API key |
| `OPENROUTER_MODEL` | Model id (default in `.env.example`: `deepseek/deepseek-v4-flash`) |
| `PARALLEL_MODE` | Internal: set to `"true"` by settings module when parallel multi-provider is active (dynamic, not in `.env`) |
| `MULTI_PROVIDERS` | Internal: JSON array of provider configs set by settings module when parallel mode is active (dynamic, not in `.env`) |
| `OPENROUTER_REFERER` | Optional Referer header for OpenRouter |
| `OPENROUTER_TITLE` | Optional app title for OpenRouter |
| `SUPABASE_URL` | Supabase URL (**required in cloud mode**) |
| `SUPABASE_SERVICE_ROLE_KEY` | Service role key (**required in cloud mode**) |
| `ENVIRONMENT` | e.g. `development` |
| `LOG_LEVEL` | e.g. `INFO` |
| `EMBED_MODEL` | Embedding model (default: `text-embedding-3-small`) |
| `EMBED_DIM` | Embedding vector dimension (default: `1536`) |
| `MAX_UPLOAD_SIZE_MB` | Max file upload size in MB (default: 25) |
| `UPLOAD_DIR` | Directory for uploaded files (default: `uploads`) |

In **local** mode, Supabase vars are not required for storage; you still need **`OPENROUTER_API_KEY`** (and usually **`OPENROUTER_MODEL`**) for LLM calls.

Provider, API key, and model can **also** be configured at runtime via the ⚙️ **Settings** modal in the UI (gear icon next to Cloud/Local toggle). Changes are saved to **`provider.json`** in the project root and applied on next server start. The API key is masked in the UI after saving.

**Multi-provider parallel mode:** When `parallel_mode: true` and 2+ entries in `multi_providers`, the agent fans out each LLM call to all configured providers in parallel and uses the first complete response. Configured via `POST /admin/settings/multi-providers` or directly in `provider.json`. Fallback: single-provider path is unchanged when parallel mode is off or < 2 providers.

## Installation

1. Clone the repository and enter the directory.

2. Create a virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

3. **Cloud (Supabase)** — If you use **`cloud`** mode, apply your team’s canonical schema (often the **Web Portal** monorepo migration, e.g. `Web Portal/supabase/migrations/20260130120000_webagent_complete_schema.sql`, when that sibling repo exists). The **`migrations/`** folder in *this* repo holds extra or historical SQL; read **`migrations/README.md`** for how it is being used.

  > **New (v0.3+):** `007_create_channel_identities.sql` and `008_create_linking_codes.sql` add tables for the communication plugin system (Telegram, WhatsApp, SMS). The **local** backend auto-creates these; on **Supabase**, apply via the SQL editor.

4. Run the server:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

**Windows users:** Use **`webAgent.bat`** (double-click) for auto-restart support. It uses `run.py` which pre-opens port 8080 with `SO_REUSEADDR` to work around orphaned TCP entries (process dead but port still bound).

**Unix:** `bash scripts/start_webAgent.sh` (background + logs).

**Useful URLs**

| URL | Purpose |
|-----|---------|
| `http://localhost:8080/` | Redirects to **`/index.html`** (main UI) |
| `http://localhost:8080/docs` | Swagger |
| `http://localhost:8080/index.html` | Full UI |
| `http://localhost:8080/test` | Minimal HTML chat (`ui/test_interface.html`) |
| `http://localhost:8080/uploads/` | Served uploaded files directory |
| `http://localhost:8080/visuals/` | Served AutoAgent rendered sketches (ephemeral) |
| `http://localhost:8080/docs` | Swagger UI (includes upload endpoint docs) |

**Unix quick start (background + logs):** `bash scripts/start_webAgent.sh` (works from any cwd; script `cd`s to repo root).

## Context defaults (no `/seed-docs` route)

There is **no** `POST /api/v1/seed-docs` in this codebase. When a user has no context rows, the first chat path copies **`context_templates`** into that user’s store (**`public.context`** on Supabase, **`context_documents`** in local SQLite).

## Using the chat API

**`POST /api/v1/chat`**

Minimal JSON body:

```json
{
  "user_id": "auth-user-uuid",
  "session_id": "session-uuid",
  "message": "What can you help me with?"
}
```

**`session_id`** is auto-created if it doesn't exist (first message in a new session creates the row automatically).

Optional fields include **`documents`**, legacy **`history`**, and **`attachment_ids`** (list of UUIDs from prior uploads) — see **`ChatRequest`** in **`app/models/schemas.py`**. When `attachment_ids` are provided, the agent sees a `[USER ATTACHMENTS]` section in its system prompt and can use the **`read_attachment`** tool to inspect file contents. **Model context is rebuilt from `interactions` in the DB** for that `session_id`; you do not need to resend **`history`** after a refresh (it is ignored for the transcript).

Example:

```bash
curl -X POST "http://localhost:8080/api/v1/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user_123",
    "session_id": "session_456",
    "message": "What can you help me with?"
  }'
```

Response includes **`reply`**, **`response`** (duplicate for different clients), and **`session_id`**. Turns are stored in **`interactions`**.

## Adding custom context

- **Supabase:** insert into **`public.context`** with the desired **`context_type`**.
- **Local:** insert into **`context_documents`**.

Common types include `agent`, `user`, `skills`, `tools`, `tasks`, and optionally `memory`, `project`, `jobs` (see **`app/api/chat.py`**).

## Quick test

1. Start **`uvicorn`** as above.
2. Open **`/index.html`** or **`/test`**.
3. Ensure a **session** row exists for your **`user_id`** / **`session_id`**.
4. Chat; defaults apply on first use when the DB has **`context_templates`**.

## Deployment

Use any Python-capable host (Railway, Render, Fly.io, Docker, etc.). Set the same env vars; use **cloud** + Supabase when you run multiple app instances.

### Google Cloud Run

webAgent is designed to run on Cloud Run. The `Dockerfile` is Cloud Run-ready (`PORT` env var, health check, CORS).

**Cloud Run compatibility checklist (maintain when changing code):**

| Requirement | Why |
|-------------|-----|
| All new Python deps added to `requirements.txt` | Cloud Run builds from `pip install -r requirements.txt` — missing deps crash at runtime |
| No dependency on persistent local filesystem | Container filesystem is **ephemeral** — use Supabase (cloud mode) for DB, Supabase Storage for uploads |
| WebSocket endpoints accept remote clients | Both `/api/v1/agent/ws` and `/api/v1/terminal/ws` accept connections from any origin (no loopback guard) |
| File writes backed by `git push` | Code changes on ephemeral disk are lost on container recycle — commit and push to persist |

**Deploy (manual):**
```bash
gcloud run deploy webagent \
  --source . \
  --region europe-west1 \
  --allow-unauthenticated
```

**Deploy (continuous deployment):** Connect your git repo in Cloud Run UI → push triggers auto-build + deploy.

## Administrator Tools

The **`app/admin/`** directory provides **privileged debug and management capabilities** that are **not part of normal user-facing operation**. These give the agent broad filesystem access and shell execution on the server.

### Source management stack (optional)

| File | What it provides |
|------|------------------|
| **`app/admin/source.py`** | FastAPI router at `/admin/source/` — **REST endpoints** for reading, writing, deleting files and running shell commands. Syntax-validates Python/JSON before writes. Backs up overwritten files to `.source-backups/`. |
| **`app/admin/source_tools.py`** | **Agent tool wrappers** — injects `read_source`, `write_source`, `edit_source`, `delete_source`, `run_command`, and `restart_server` into the agent's tool list. Mutating tools (`write`, `edit`, `delete`, `run`, `restart`) **require user confirmation** before the agent may call them. |
| **`app/admin/guardrails.py`** | **Optional security deny-list** — blocks access to `.env`, `.bash_history`, `.ssh/*`, and dangerous commands like `rm -rf /`. Delete this file to remove all restrictions. |

**How the agent sees them:**

| Tool | What it does | User confirmation? |
|------|-------------|-------------------|
| `read_source` | Read any file on the system | ❌ No (read-only) |
| `write_source` | Create/overwrite files (backed up) | ✅ Yes |
| `edit_source` | Replace exact text in a file | ✅ Yes |
| `delete_source` | Delete files or directories | ✅ Yes |
| `run_command` | Execute arbitrary shell commands | ✅ Yes |
| `restart_server` | Kill and restart the webAgent server process | ✅ Yes |

### Other admin endpoints

| Router | Endpoints | Purpose |
|--------|-----------|---------|
| `review.py` | `GET /admin/tools`, `GET /admin/tools/{name}`, `DELETE /admin/tools/{id}` | List/get/deprecate tools in the DB |
| `settings.py` | `GET/POST /admin/settings/provider`, `GET/POST /admin/settings/multi-providers`, `GET/POST /admin/settings/metadata`, `GET /admin/settings/models` | Switch AI provider, API key, model; toggle metadata logging; configure parallel multi-provider list |
| `db_mode.py` | `GET /admin/db/mode`, `POST /admin/db/mode` | Toggle between Cloud (Supabase) and Local (SQLite) |
| `communications.py` | `GET /admin/communications/plugins`, enable/disable, set webhook URL | Manage Telegram, WhatsApp plugins |

### Disabling administrator tools

**To remove all privileged filesystem and shell access, simply delete the `app/admin/` directory (or just `source.py` + `source_tools.py`).**

The import in `app/tools/loader.py` is guarded by `try/except ImportError` — if the files don't exist, the agent never gets the tools. The same guarded import pattern in `app/main.py` prevents the REST endpoints from being mounted.

```bash
# Fastest lockdown — remove the source management files:
rm -rf app/admin/source.py app/admin/source_tools.py

# Full lockdown — remove the entire admin module:
rm -rf app/admin/
```

No code changes are needed. The server continues running; the next agent turn will simply lack all admin tools.

## Assistants and scratch files

**`agent.md`** defines how coding assistants should treat this repo (terminology, **`temp/`** for scratch artifacts, etc.) and **requires updating this README** when edits change layout, config, APIs, or features so the tree and sections stay accurate. Roadmap notes live in **`temp/FUTURE_PLANS.md`** (not treated as product spec unless you say otherwise).

## License

MIT

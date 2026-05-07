# webAgent

A **FastAPI** service with a **tool-calling** LLM agent (OpenRouter), optional **Supabase** or **local SQLite** persistence, and a **vanilla JS** UI at **`/index.html`** (static assets under `/ui/`).

## Features

- **Chat** — `POST /api/v1/chat` (and **`POST /api/v1/chat/stream`**): agent loop with tools; turns go to **`interactions`**. Prior turns for the same **`session_id`** are reloaded from the DB into the model context (browser refresh does not reset the conversation).
- **WebSocket agent** — `GET` upgrade to `/api/v1/agent/ws`: streaming tokens, tool events, pipeline steps (**loopback clients only**).
- **Context** — Prompt slices from `context_type` / `doc_type`; if a user has no rows, **`context_templates`** are copied into per-user context on first chat.
- **Memory** — Brain-style lookup before each chat turn; optional background save of chat snippets into memory.
- **Attachments** — Image, audio, video, and file uploads. Users attach files via the UI (📎 button in footer, drag & drop onto chat messages or footer area, 🎤 voice recording). Files upload via **`POST /api/v1/upload`** and bytes are persisted through **`app/db/attachments/`** (local filesystem in dev, Supabase Storage in production — see `app/db/SUPABASE_STORAGE.md`). Metadata is stored in the **`attachments`** table (local SQLite or Supabase). The agent accesses files with the **`read_attachment`** built-in tool. Supports image preview, audio/video players, and download links inline in chat bubbles. Attachments persist per-session and survive server restarts.
- **Tools** — Dynamic tools from the DB (JSON schemas in **`app/tools/loader.py`**), including Playwright **`browser.py`** and built-in **`read_attachment`**, **`create_tool`**, **`rate_skill`**.
- **OpenRouter** — Model from `OPENROUTER_MODEL` (see `.env.example`; e.g. `deepseek/deepseek-v4-flash`).
- **Dual storage** — **`cloud`** (Supabase) vs **`local`** (SQLite file **`app/db/local.db`**). Mode is stored in **`app/db_mode.json`** and switched via **`/admin/db/*`**.
- **Administrator tools** — Optional filesystem read/write/edit/delete, shell command execution, and server restart exposed as agent tools (**`read_source`**, **`write_source`**, **`edit_source`**, **`delete_source`**, **`run_command`**, **`restart_server`**). Powered by **`app/admin/source.py`** + **`app/admin/source_tools.py`**. **These are privileged debug tools — NOT available in normal user operation.** Deleting the `app/admin/` directory removes them entirely. See the [Administrator Tools](#administrator-tools) section.
- **Web UI** — Main page at **`/index.html`** (chat, DB viewer, terminal, stream/loop). **`/terminal`** redirects to **`/index.html`**.
- **Minimal tester** — **`GET /test`** serves **`ui/test_interface.html`** (same origin as the API).

## Architecture and module map

### Unified Agent Engine Workflow

The agent uses a single unified execution engine (`app/agent/loop.py`) that serves both streaming (WebSocket/SSE) and buffered (HTTP POST) requests:

```text
       [CLIENT]
          |
          | (Chooses Transport Protocol)
          |
          +-- (WebSocket) ----> [ app/api/agent.py ] (WS Route: live bidirectional)
          |
          +-- (HTTP SSE) -----> [ app/api/chat.py ]  (SSE Route: live unidirectional)
          |
          +-- (HTTP POST) ----> [ app/api/chat.py ]  (Sync Route: buffers until end)
          |
          +-- (HTTP Upload) --> [ app/api/uploads.py ]  (Multipart POST async from UI) <<<UPLOAD SHOULD BE AVAILABLE FROM ALL SOURCES, LIKE UI, TELEGRAM, AND OTHER CONNECTIONS (ONLY UI AND TELEGRAM FOR NOW, OTHERS TO COME)>>>
          |                             |
          |                     [ app/db/attachments/file_store.py ]
          |                     store_file()  → bytes saved + DB row
          |                             |
          |                     Returns { attachment_id, url } to client
          |                             |
          +<--- attachment_ids included in next WS message
                                        |
                          <<<DOES IT GO TO DB FIRST? IF SO, DOE THE ENGINE GET NOTIFIED? THAT'S HOW IT WOULD WORK WITH FUTURE SUPABASE IMPLEMENTATION>>>
                                        v
                            +--------------------------+
                            | ONE UNIFIED ENGINE       |
                            | (app/agent/loop.py)      |
                            +--------------------------+
                                        |
                                        +--> 0. Resolve attachment_ids → inject [USER ATTACHMENTS] into system prompt
                                        |
                                        +--> 1. Build System Prompt <<<NEED TO ELABORATE WHERE SYSTEM PROMPT IS COMING FROM. WHICH FILES, WHICH LOGIC, DB CELLS, ETC>>>
                                        |
                                        +--> 1. Fetch Memory <<<WHAT IS LOGIC FOR MEMORY FETCH? SEPERATE PY SCRIPT? HOW DOES IT PARSE, ETC>>>
                                        |
+------------------------------------+  +--> 2. While turn_count < max_turns:
| INTERRUPT DB/CACHE                 |  |       |
| Tracks flags for session_id        |  |       +-> Check Client Disconnect OR Interrupt Flag <<<CLIENT DISCONNECT SHOULD NOT BE A CONCERN. need to remove the disconnect logic.. AGENT SHOLD WORK OFFLINE AND SEND OUTPUT TO DB. INTERRUPT FLAG FROM WHICH CLIENT SOURCE?>>>
+------------------------------------+  |       |    (If true: Break & Emit Interrupted)
             ^                          |       |
             |  (Sets Flag)             |       +-> Stream LLM Call (Tools = Auto) <<<what does tool=auto mean? where does it get the library of available tools and how to use them?>>>
 [ HTTP POST /api/v1/chat/interrupt ]   |       |
             ^                          |       +-> Validate Tool Calls & Check Guardrails <<<need to show the guardrails>>>
             |                          |       |
          [CLIENT]                      |       +-> Execute Tools in Parallel <<<does this mean agent can call multiple tools?
                                        |       |    read_attachment(attachment_id)
                                        |       |      → read_file() from storage
                                        |       |      → return content to agent
                                        |       |
                                        |       +-> Track Skill Execution & Save to DB <<<show logic>>>
                                        |
                                        +--> 3. Return Final Response (attachments rendered inline)
                                        |
                                        +--> 4. Async Background Memory Save <<<does it go to db, and then to user (ui or telegram)?>>>
```

### Backend (`app/`)

| Module | Role |
|--------|------|
| **`main.py`** | FastAPI app: routers, CORS, no-cache for `/ui/` and `/index.html`, **`StaticFiles`** for `/ui/` and `/screenshots`, **`GET /` → redirect to `/index.html`**, **`GET /index.html`**, **`GET /test`**, **`GET /health`**, favicon from `ui/favicon.svg`, **`POST /api/v1/restart`**, shutdown (browser + terminal). |
| **`api/chat.py`** | **`POST /api/v1/chat`**, **`POST /api/v1/chat/stream`**, **`POST /api/v1/chat/interrupt`** — context load, memory search, prompt build, attachment resolution, **`session_history`** → **`loop.stream_agent_events`** / **`run_agent_loop_buffered`**, **`interactions`**; pipeline events for visualizers. |
| **`api/agent.py`** | **`WebSocket /api/v1/agent/ws`** — **`loop.stream_agent_events`**; reloads session from **`interactions`** each message; resolves attachment references from WS message. |
| **`api/uploads.py`** | **`POST /api/v1/upload`** — multipart file upload (images, audio, video, PDF, text). **`GET /api/v1/upload/{id}`** — metadata lookup. **`DELETE /api/v1/upload/{id}`** — delete. File bytes stored via `app/db/attachments/`. |
| **`api/terminal.py`** | **`WebSocket /api/v1/terminal/ws`** — browser shell (PTY / **`pywinpty`** on Windows). |
| **`api/db_viewer.py`** | **`/api/v1/db/*`** — SQLite introspection; DB files under **`app/db/`** (default filename **`local.db`** for the UI query param `db=`). |
| **`agent/loop.py`** | Unified multi-turn loop (streaming + buffered): tool validation, parallel tool runs, pipeline events. Emits `attachment` event type for frontend file rendering. |
| **`agent/session_history.py`** | Maps **`interactions`** rows → OpenAI-style **`messages`** for the active session (excludes internal memory tools). |
| **`agent/prompts.py`** | System prompt from context, brain results, tools, attachment context. Includes **`format_attachments_for_prompt()`** helper. |
| **`agent/error_classifier.py`** | Structured tool errors (**used on the WebSocket / streaming path**). |
| **`db/__init__.py`** | **`get_db()`** → **`SupabaseBackend`** or **`LocalBackend`** from persisted mode. |
| **`db/supabase.py`** | Cloud: **`sessions`**, **`interactions`**, **`context`**, **`context_templates`**, **`attachments`**, memories / tools / skills per shared schema. |
| **`db/local.py`** | Local SQLite (e.g. **`context_documents`**, **`attachments`**) and related tables beside **`local.db`**. |
| **`db/attachments/`** | **`file_store.py`** — file byte storage abstraction. Dispatches to local filesystem (`uploads/`) or Supabase Storage based on `db_mode.json`. Exports `store_file()`, `read_file()`, `delete_file()`. See `app/db/SUPABASE_STORAGE.md` for cloud setup. |
| **`db/interface.py`** | **`StorageBackend`** protocol with **`insert_attachment`**, **`get_attachment`**, **`get_session_attachments`**, **`delete_attachment`**. |
| **`tools/`** | **`loader`**, **`registry`**, **`tracker`**, **`browser`**, **`read_attachment`** (built-in tool for reading uploaded files via `app/db/attachments/`). |
| **`models/schemas.py`** | Pydantic models (`ChatRequest`, etc.). |
| **`admin/`** | **`review`** (`/admin/tools` — list/deprecate DB tools), **`db_mode`** (`/admin/db/` — cloud/local switch), **`settings`** (provider config, model list, metadata toggle), **`guardrails`** (path/command deny-list for source tools), **`communications`** (Telegram/WhatsApp plugin mgmt), **`source`** + **`source_tools`** (optional privileged filesystem & shell access — delete to disable). See [Administrator Tools](#administrator-tools). |
| **`openai_compat.py`** | OpenAI-compatible client wiring for OpenRouter. |

### Frontend (`ui/`)

Single-page app: **`index.html`**, CSS (`app1.css`, `app2.css`, `app3.css`, `loop.css`), ES modules under **`js/`** — e.g. **`main.js`**, **`chat.js`**, **`agentWs.js`**, **`stream.js`**, **`loop.js`**, **`tabs.js`**, **`toolLog.js`**, **`terminal.js`**, **`dbMode.js`**, **`sessions.js`**, **`attachments.js`** (file upload, voice recording, drag & drop, preview chips), **`js/db/`** (data browser). **`test_interface.html`** is also here and is served at **`GET /test`**.

### Directory tree (abbreviated)

```
webAgent/
├── app/                    # Python package (see table above)
│   └── db/
│       └── attachments/    # File storage abstraction (store_file / read_file / delete_file)
├── tests/                  # e.g. test_session_history.py (unittest)
├── ui/                     # Static UI + test_interface.html
├── uploads/                # User-uploaded files (images, voice, docs; mounted at /uploads)
├── scripts/
│   ├── start_webAgent.sh   # Unix: cd to repo root, background uvicorn (default :8080, PORT= overrides)
│   └── seed_tools.py       # Optional tool DB seeding
├── migrations/             # Ad-hoc SQL snapshots (includes 007_channel_identities, 008_linking_codes); see migrations/README.md
├── supabase/migrations/    # e.g. 005_memory_system.sql (Supabase CLI / team workflow)
├── screenshots/            # Mounted at /screenshots
├── android/                # Optional Android wrapper (Java + embedded Python)
├── tasks/                  # Small Node helper (package.json, run-all.ts)
├── temp/                   # Scratch files incl. Markdown drafts (see agent.md); roadmap: temp/FUTURE_PLANS.md
├── .github/workflows/      # CI (e.g. APK build)
├── webAgent.bat            # Windows: uvicorn loop + restart support
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
| `OPENROUTER_REFERER` | Optional Referer header for OpenRouter |
| `OPENROUTER_TITLE` | Optional app title for OpenRouter |
| `SUPABASE_URL` | Supabase URL (**required in cloud mode**) |
| `SUPABASE_SERVICE_ROLE_KEY` | Service role key (**required in cloud mode**) |
| `ENVIRONMENT` | e.g. `development` |
| `LOG_LEVEL` | e.g. `INFO` |
| `MAX_UPLOAD_SIZE_MB` | Max file upload size in MB (default: 25) |
| `UPLOAD_DIR` | Directory for uploaded files (default: `uploads`) |

In **local** mode, Supabase vars are not required for storage; you still need **`OPENROUTER_API_KEY`** (and usually **`OPENROUTER_MODEL`**) for LLM calls.

Provider, API key, and model can **also** be configured at runtime via the ⚙️ **Settings** modal in the UI (gear icon next to Cloud/Local toggle). Changes are saved to **`provider.json`** in the project root and applied on next server start. The API key is masked in the UI after saving.

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

**Useful URLs**

| URL | Purpose |
|-----|---------|
| `http://localhost:8080/` | Redirects to **`/index.html`** (main UI) |
| `http://localhost:8080/docs` | Swagger |
| `http://localhost:8080/index.html` | Full UI |
| `http://localhost:8080/test` | Minimal HTML chat (`ui/test_interface.html`) |
| `http://localhost:8080/uploads/` | Served uploaded files directory |
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

**`session_id`** must exist in **`sessions`** for that **`user_id`**.

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
| `settings.py` | `GET/POST /admin/settings/provider`, `GET/POST /admin/settings/metadata`, `GET /admin/settings/models` | Switch AI provider, API key, model; toggle metadata logging |
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

# webAgent

A **FastAPI** service with a **tool-calling** LLM agent (OpenRouter), optional **Supabase** or **local SQLite** persistence, and a **vanilla JS** UI under `/ui/`.

## Features

- **Chat** — `POST /api/v1/chat`: non-streaming agent loop with tools; persists turns to **`interactions`** (not a separate `messages` table).
- **WebSocket agent** — `GET` upgrade to `/api/v1/agent/ws`: streaming tokens, tool events, pipeline steps (**loopback clients only**).
- **Context** — Prompt slices from `context_type` / `doc_type`; if a user has no rows, **`context_templates`** are copied into per-user context on first chat.
- **Memory** — Brain-style lookup before each chat turn; optional background save of chat snippets into memory.
- **Tools** — Dynamic tools from the DB (JSON schemas in **`app/tools/loader.py`**), including Playwright **`browser.py`**.
- **OpenRouter** — Model from `OPENROUTER_MODEL` (see `.env.example`; e.g. `deepseek/deepseek-v4-flash`).
- **Dual storage** — **`cloud`** (Supabase) vs **`local`** (SQLite file **`app/db/local.db`**). Mode is stored in **`app/db_mode.json`** and switched via **`/admin/db/*`**.
- **Web UI** — Static app at **`/ui/`** (chat, DB viewer, terminal, stream/loop). **`/terminal`** redirects to **`/ui/`**.
- **Minimal tester** — **`GET /test`** serves **`ui/test_interface.html`** (same origin as the API).

## Architecture and module map

### Backend (`app/`)

| Module | Role |
|--------|------|
| **`main.py`** | FastAPI app: routers, CORS, no-cache for `/ui/`, **`StaticFiles`** for `/ui/` and `/screenshots`, **`GET /test`**, **`GET /health`**, favicon from `ui/favicon.svg`, **`POST /api/v1/restart`**, shutdown (browser + terminal). |
| **`api/chat.py`** | **`POST /api/v1/chat`** — context load, memory search, prompt build, **`loop.run_agent_loop`**, **`interactions`**, optional memory persistence; pipeline events for visualizers. |
| **`api/agent.py`** | **`WebSocket /api/v1/agent/ws`** — **`streaming_loop.stream_agent_events`**. |
| **`api/terminal.py`** | **`WebSocket /api/v1/terminal/ws`** — browser shell (PTY / **`pywinpty`** on Windows). |
| **`api/db_viewer.py`** | **`/api/v1/db/*`** — SQLite introspection; DB files under **`app/db/`** (default filename **`local.db`** for the UI query param `db=`). |
| **`agent/loop.py`** | HTTP multi-turn loop: tool validation, parallel tool runs where applicable. |
| **`agent/streaming_loop.py`** | WebSocket streaming loop; structured tool/pipeline events. |
| **`agent/prompts.py`** | System prompt from context, brain results, tools. |
| **`agent/error_classifier.py`** | Structured tool errors (**used on the WebSocket / streaming path**). |
| **`db/__init__.py`** | **`get_db()`** → **`SupabaseBackend`** or **`LocalBackend`** from persisted mode. |
| **`db/supabase.py`** | Cloud: **`sessions`**, **`interactions`**, **`context`**, **`context_templates`**, memories / tools / skills per shared schema. |
| **`db/local.py`** | Local SQLite (e.g. **`context_documents`**) and related tables beside **`local.db`**. |
| **`db/interface.py`** | **`StorageBackend`** protocol. |
| **`tools/`** | **`loader`**, **`registry`**, **`tracker`**, **`browser`**. |
| **`models/schemas.py`** | Pydantic models (`ChatRequest`, etc.). |
| **`admin/`** | **`review`** (`/admin/...`), **`db_mode`** (`/admin/db/...`), **`settings`**, **`guardrails`**, optional **`source`** / **`source_tools`**. |
| **`openai_compat.py`** | OpenAI-compatible client wiring for OpenRouter. |

### Frontend (`ui/`)

Single-page app: **`index.html`**, CSS (`app1.css`, `app2.css`, `app3.css`, `loop.css`), ES modules under **`js/`** — e.g. **`main.js`**, **`chat.js`**, **`agentWs.js`**, **`stream.js`**, **`loop.js`**, **`tabs.js`**, **`toolLog.js`**, **`terminal.js`**, **`dbMode.js`**, **`sessions.js`**, **`js/db/`** (data browser). **`test_interface.html`** is also here and is served at **`GET /test`**.

### Directory tree (abbreviated)

```
webAgent/
├── app/                    # Python package (see table above)
├── ui/                     # Static UI + test_interface.html
├── scripts/
│   ├── start_webAgent.sh   # Unix: cd to repo root, background uvicorn :8000
│   └── seed_tools.py       # Optional tool DB seeding
├── migrations/             # Ad-hoc SQL snapshots; see migrations/README.md
├── supabase/migrations/    # e.g. 005_memory_system.sql (Supabase CLI / team workflow)
├── screenshots/            # Mounted at /screenshots
├── android/                # Optional Android wrapper (Java + embedded Python)
├── tasks/                  # Small Node helper (package.json, run-all.ts)
├── temp/                   # Scratch non-Markdown files (see agent.md)
├── temp-md-files/          # Scratch Markdown (see agent.md)
├── .github/workflows/      # CI (e.g. APK build)
├── webAgent.bat            # Windows: uvicorn loop + restart support
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

In **local** mode, Supabase vars are not required for storage; you still need **`OPENROUTER_API_KEY`** (and usually **`OPENROUTER_MODEL`**) for LLM calls.

## Installation

1. Clone the repository and enter the directory.

2. Create a virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

3. **Cloud (Supabase)** — If you use **`cloud`** mode, apply your team’s canonical schema (often the **Web Portal** monorepo migration, e.g. `Web Portal/supabase/migrations/20260130120000_webagent_complete_schema.sql`, when that sibling repo exists). The **`migrations/`** folder in *this* repo holds extra or historical SQL; read **`migrations/README.md`** for how it is being used.

4. Run the server:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Useful URLs**

| URL | Purpose |
|-----|---------|
| `http://localhost:8000/` | API root JSON |
| `http://localhost:8000/docs` | Swagger |
| `http://localhost:8000/ui/` | Full UI |
| `http://localhost:8000/test` | Minimal HTML chat (`ui/test_interface.html`) |

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

Optional fields include **`documents`** and **`history`** — see **`ChatRequest`** in **`app/models/schemas.py`**.

Example:

```bash
curl -X POST "http://localhost:8000/api/v1/chat" \
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
2. Open **`/ui/`** or **`/test`**.
3. Ensure a **session** row exists for your **`user_id`** / **`session_id`**.
4. Chat; defaults apply on first use when the DB has **`context_templates`**.

## Deployment

Use any Python-capable host (Railway, Render, Fly.io, Docker, etc.). Set the same env vars; use **cloud** + Supabase when you run multiple app instances.

## Assistants and scratch files

**`agent.md`** defines how coding assistants should treat this repo (terminology, **`temp/`** vs **`temp-md-files/`**, etc.) and **requires updating this README** when edits change layout, config, APIs, or features so the tree and sections stay accurate. Roadmap notes may live in **`temp-md-files/FUTURE_PLANS.md`** (not treated as product spec unless you say otherwise).

## License

MIT

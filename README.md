# webAgent

A **FastAPI** service with a **tool-calling** LLM agent (OpenRouter), optional **Supabase** or **local SQLite** persistence, and a **vanilla JS** UI at **`/index.html`** (static assets under `/ui/`).

## Features

- **Chat** — `POST /api/v1/chat` (buffered) and **`POST /api/v1/chat/stream`** (SSE streaming): agent loop with tools; turns go to **`interactions`**. Prior turns for the same **`session_id`** are reloaded from the DB into the model context (browser refresh does not reset the conversation).
- **WebSocket agent (receive-only)** — `GET` upgrade to `/api/v1/agent/ws`: per-user subscriber mode. Connects once per user, receives ALL agent events (stream, response, tool_call, tool_result, pipeline, db) for all of that user's sessions. **Does not send messages** — all user messages go through HTTP POST.
- **Context** — Prompt slices from `context_type` / `doc_type`; if a user has no rows, **`context_templates`** are copied into per-user context on first chat.
- **Memory** — Hybrid search (FTS5 keyword + vector cosine similarity via embedding API) runs before each chat turn; results injected as `[BRAIN CONTEXT]` in the system prompt. Trivial messages (greetings, affirmations, commands) skip memory via regex gate. Page content auto-chunked and embedded on write. Background save of chat snippets into memory. See [`memory-upgrade.md`](memory-upgrade.md).
- **AutoAgent** — Multi-page workspace tab 🎨. Each user has a persistent set of named pages (home, dashboard, notes, custom). Pages stored per-user under `visuals/users/<user_id>/` with a `pages.json` manifest. The **home** page is auto-seeded with a webAgent onboarding/info page. Users add pages via the **+** dropdown nav button; each page gets its own dedicated agent persona (`agent_context` field in manifest). Prompts are tagged `[User → UI Agent → Page: "<title>" | Context: "..."]` so the agent knows its role and writes to the right page. Powered by `render_visual`, `list_pages`, `create_page`, `delete_page` tools in **`app/visualizer/`**. REST API at **`/api/v1/pages`**. Visuals served from `/visuals/users/` (ephemeral, Cloud Run safe). See [`app/visualizer/SKILL.md`](app/visualizer/SKILL.md).
- **Attachments** — Image, audio, video, and file uploads. Users attach files via the UI (📎 button in footer, drag & drop onto chat messages or footer area, 🎤 voice recording). Files upload via **`POST /api/v1/upload`** and bytes are persisted through **`app/db/attachments/`** (local filesystem in dev, Supabase Storage in production — see `app/db/SUPABASE_STORAGE.md`). Metadata is stored in the **`attachments`** table (local SQLite or Supabase). The agent accesses files with the **`read_attachment`** built-in tool. Supports image preview, audio/video players, and download links inline in chat bubbles. Attachments persist per-session and survive server restarts.
- **Tools** — **Bootstrap + on-demand discovery model.** A small set of hardcoded core tools (list_tools, search_tools, get_tool_definition, web_search, http_request, browser_action, db_query, memory, session_search, get_time, get_date, get_weather, calculate, read_attachment) are always available from turn 1 via **`app/tools/loader.py`** + **`app/tools/core_tools.py`**. All other tools (user-created, admin, comm plugins, webhook management) are discovered on demand via `list_tools` / `search_tools` / `get_tool_definition`. Tool definitions no longer auto-populate the system prompt — only curated `context_type="skills"` docs provide behavioral guidance in the `# [SKILLS]` section.
- **OpenRouter** — Model from `OPENROUTER_MODEL` (see `.env.example`; e.g. `deepseek/deepseek-v4-flash`).
- **Parallel multi-provider** — Configure 2+ LLM providers in Settings. When enabled, the agent fans out each message to all providers simultaneously and uses the fastest complete response. Configured via `GET/POST /admin/settings/multi-providers`. Set `parallel_mode: true` and a list of provider entries (each with provider, base_url, api_key, model) in `provider.json` or DB `auth_elements`.
- **Dual storage** — **`cloud`** (Supabase) vs **`local`** (SQLite file **`app/db/local.db`**). Mode is stored in **`app/db_mode.json`** and switched via **`/admin/db/*`**.
- **Administrator tools** — Optional filesystem read/write/edit/delete, shell command execution, and server restart exposed as agent tools (**`read_source`**, **`write_source`**, **`edit_source`**, **`delete_source`**, **`run_command`**, **`restart_server`**). Powered by **`app/admin/source.py`** + **`app/admin/source_tools.py`**. **These are privileged debug tools — NOT available in normal user operation.** Deleting the `app/admin/` directory removes them entirely. See the [Administrator Tools](#administrator-tools) section.
- **Multi-agent system** — Multiple agent templates can be defined in `context/agents/`. Each user can have their own default agent. The agent loop supports **mid-turn delegation**: the `delegate_to_agent` tool lets the active agent hand off to another agent within the same session (rebinds session, reloads tools, injects a system-prompt switch message). Pipeline events (`agent_delegation`) are emitted to the Loop/Flow panel for visibility. Non-pipeline agents always receive the `delegate_to_agent` and `list_delegatable_agents` tools.
- **Agent Management UI** — **Agents tab** in the main UI (🤖) for browsing, creating, editing, and deleting agent templates. Each agent card expands inline into its own detail panel (Config / Tools / Agent Loop tabs); multiple rows can be open simultaneously. Supports all template fields (name, description, icon, system prompt, model, temperature, max tokens, trigger description, access level, pipeline flag). Light mode aware. **Interactive Agent Loop diagram** — clicking any node on a custom agent's loop diagram opens an in-place edit panel: prompt sections (Load Context / Build Prompt nodes), memory search toggle, LLM model/temperature/max-tokens, per-tool Tier-2 toggles (Execute Tools node), category-level guardrails, max-turn-count slider, and memory-save toggle. Changes are saved via `PUT /api/v1/agents/{id}` and enforced at runtime via `allowed_tools` and `custom_tool_ids` columns on the `agents` table. **Per-node loop gating** — five optional steps (`interrupt_chk`, `permission_chk`, `guardrails`, `delegation_chk`, `skill_track`) can be individually toggled on/off per agent via the loop diagram UI; the state is stored in the `loop_logic` JSON column as an object array and respected by `LoopConfig` inside `app/agent/loop_executor.py` at runtime. Disabled nodes render muted with a strikethrough label in the diagram.
- **Admin users** — User admin flag stored in `user_profiles.is_admin`. The first admin is bootstrapped via `BOOTSTRAP_ADMIN_ID` env var. Admin users have access to the `admin-agent` template (a privileged agent with filesystem/shell tools). `GET /admin/users` and `POST /admin/users/{user_id}/set-admin` manage the admin list (admin-only endpoints).
- **Login tracking** — User login activity is tracked in `user_profiles` table. On first login (via `/api/v1/auth/login`, `/api/v1/auth/register`, or `/api/v1/auth/recall`), a `user_profiles` row is created with `created_at` (first login timestamp) and `display_name` from `users.json`. On each subsequent login, `last_login_at` is updated to the current timestamp. Historical login data is not persisted; only the most recent login time is recorded. **To reset all local users:** delete **`app/auth/users.json`** and restart the server — it will recreate with default admin:admin.
- **Web UI** — Main page at **`/index.html`** (chat, DB viewer, terminal, stream/loop, agents). **`/terminal`** redirects to **`/index.html`**.
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
| **`agent/loop.py`** | Unified multi-turn loop (streaming + buffered): tool validation, parallel tool runs, pipeline events. Emits `attachment` event type for frontend file rendering. Reads `LoopConfig` from the agent record to gate optional steps at runtime. Builds the effective destructive-tool set from `DESTRUCTIVE_TOOLS` baseline ∪ `agents.safety_policy.destructive_tools` ∪ per-tool `requires_confirmation` flags; supports `auto_confirm` and `max_concurrent_tools` from safety policy. |
| **`agent/loop_executor.py`** | **`LoopConfig`** — parses the `loop_logic` JSON column from an agent record and exposes `is_enabled(node_id, context=)`. Supports two formats: flat string array (legacy, all nodes enabled) and object array (`[{"node": "skill_track", "enabled": false, "run_if": "expr"}, ...]`). Defines `LOCKED_NODES` (steps that can never be disabled: `user_input`, `load_tools`, `llm_call`, `execute_tools`, `check_continue`, `final_response`) and `GATED_NODES` (9 steps with runtime gating: `interrupt_chk`, `permission_chk`, `guardrails`, `delegation_chk`, `skill_track`, `memory_search`, `memory_save`, `fire_optimizer`, `copy_defaults`). Pre-loop nodes (`memory_search`, `memory_save`, `copy_defaults`) are gated in `chat.py`; in-loop nodes in `loop.py`. Includes `_evaluate_run_if()` for conditional node execution (supports `==`, `!=`, `>`, `<`, `>=`, `<=`, `!key`, `key in [a,b,c]`). |
| **`agent/session_history.py`** | Maps **`interactions`** rows → OpenAI-style **`messages`** for the active session (excludes internal memory tools). |
| **`agent/prompts.py`** | System prompt from context, brain results, tools, attachment context. Includes **`format_attachments_for_prompt()`** helper. |
| **`agent/error_classifier.py`** | Structured tool errors (**used on the WebSocket / streaming path**). |
| **`context/agents/`** | **Agent template JSON files** — seed `agent_templates` table with full schema (id, system_prompt, max_turn_count, model, provider, temperature, max_tokens, metadata, plus new fields: `name`, `description`, `icon`, `trigger_description`, `can_be_default`, `is_system`, `is_pipeline`, `access_level`). Each `.json` file defines one agent template. Included: `default.json`, `optimizer-planner.json`, `optimizer-finalizer.json`, `admin-agent.json`. Scanned on first agent creation for a user. |
| **`context/context_templates/`** | **Context template .md files** — seed `context_templates` table per context_type (agent, user, skills, tools, tasks, memory, project, jobs). Copied to user context on first chat. |
| **`agent/embed.py`** | Embedding utility using same provider config as chat. Returns configurable-dimension vectors (`EMBED_DIM`, default 1536). |
| **`db/__init__.py`** | **`get_db()`** → **`SupabaseBackend`** or **`LocalBackend`** from persisted mode. |
| **`db/supabase.py`** | Cloud: **`sessions`**, **`interactions`**, **`context`**, **`context_templates`**, **`attachments`**, memories / tools / skills per shared schema. |
| **`db/local.py`** | Local SQLite — schema init, FTS5 + vector hybrid search, embed-on-write, knowledge graph, timelines, **`user_profiles`** (tracks `created_at` and `last_login_at`), **`webhook_registrations`** and **`webhook_event_log`** tables. |
| **`db/attachments/`** | **`file_store.py`** — file byte storage abstraction. Dispatches to local filesystem (`uploads/`) or Supabase Storage based on `db_mode.json`. Exports `store_file()`, `read_file()`, `delete_file()`. See `app/db/SUPABASE_STORAGE.md` for cloud setup. |
| **`db/interface.py`** | **`StorageBackend`** protocol with session, interaction, context, memory, skills, agent, attachment, interrupt, **webhook (register/get/list/delete/log)** abstract methods. |
| **`tools/`** | **`loader`** (dynamic tool loading + built-in injection: http_request, register_webhook, list_webhooks, delete_webhook, get_webhook_log, render_visual, plus **`delegate_to_agent` / `list_delegatable_agents`** for non-pipeline agents), **`core_tools`** (bootstrap tools: list_tools, search_tools, get_tool_definition, web_search, http_request, db_query, memory, session_search, get_time, get_date, get_weather, calculate), **`registry`** (create_tool, safety scanner, rating utilities), **`tracker`** (legacy execution tracker), **`browser`** (persistent Chromium), **`read_attachment`** (read uploaded files via `app/db/attachments/`), **`delegation.py`** (builds `delegate_to_agent` + `list_delegatable_agents` handlers; returns delegation sentinel JSON detected by the loop). |
| **`visualizer/`** | **Multi-page workspace tools** — `render_visual`, `list_pages`, `create_page`, `delete_page`. Pages stored per-user at `visuals/users/<user_id>/<slug>.html` with a `pages.json` manifest. `pages.py` handles all page CRUD; `tool.py` implements `render_visual`. **`SKILL.md`** — agent guide for page building. Self-contained — delete to disable. |
| **`models/schemas.py`** | Pydantic models (`ChatRequest`, etc.). |
| **`admin/`** | **`review`** (`/admin/tools` — list/deprecate DB tools), **`db_mode`** (`/admin/db/` — cloud/local switch), **`settings`** (provider config, model list, metadata toggle), **`guardrails`** (path/command deny-list for source tools), **`communications`** (Telegram/WhatsApp plugin mgmt), **`source`** + **`source_tools`** (optional privileged filesystem & shell access — delete to disable), **`users.py`** (`GET /admin/users`, `POST /admin/users/{user_id}/set-admin` — admin user management). See [Administrator Tools](#administrator-tools). |
| **`api/agents.py`** | **`GET /api/v1/agents/templates`** — list all agent templates. **`POST /api/v1/agents/templates`** — create a template (admin only). **`PUT /api/v1/agents/templates/{id}`** — update a template (admin only). **`DELETE /api/v1/agents/templates/{id}`** — delete a template (admin only). **`GET /api/v1/agents/my-agent`** — get the current user's active agent. **`POST /api/v1/agents/set-default`** — set the user's default agent template. |
| **`openai_compat.py`** | OpenAI-compatible client wiring for OpenRouter. |

### Frontend (`ui/`)

Single-page app: **`index.html`**, CSS (`app1.css`, `app2.css`, `app3.css`, `loop.css`, `loop-visual.css`, `autoagent.css`, `agents.css`), ES modules under **`js/`** — e.g. **`main.js`**, **`chat.js`** (sends messages via HTTP POST + SSE), **`agentWs.js`** (per-user receive-only WebSocket subscriber), **`stream.js`**, **`loop.js`**, **`tabs.js`**, **`toolLog.js`**, **`terminal.js`**, **`dbMode.js`**, **`sessions.js`**, **`attachments.js`** (file upload, voice recording, drag & drop, preview chips), **`autoagent.js`** (visualizer tab: iframe renderer, prompt bar, render_visual event listener), **`optimizer.js`** (session manager + optimizer config UI), **`agents.js`** (Agent Management tab: list/create/edit/delete agent templates, calls `/api/v1/agents/templates`), **`js/db/`** (data browser). **`test_interface.html`** is also here and is served at **`GET /test`**.

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
│   ├── seed_tools.py                # Optional tool DB seeding
│   └── export_agent_templates.py    # Reverse-seed: DB → app/context/agents/*.json
├── export_agent_templates.bat  # Double-click: runs export_agent_templates.py (--dry-run supported)
├── migrations/             # Ad-hoc SQL snapshots (includes 007_channel_identities, 008_linking_codes, 009_multi_agent_system, 010_agent_name_backfill, 011_add_login_tracking); see migrations/README.md
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
| `BOOTSTRAP_ADMIN_ID` | User ID to auto-promote to admin on server start (first admin bootstrapping). Once an admin exists in `user_profiles`, this var has no further effect. |

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

  > **New (v0.4+):** `009_multi_agent_system.sql` adds the `user_profiles` table (admin flag, default agent), extends `agent_templates` with `name`, `description`, `icon`, `trigger_description`, `can_be_default`, `is_system`, `is_pipeline`, `access_level` columns, extends `agents` with `template_id`, `owner_user_id`, `is_user_default`, `is_pipeline` columns, and seeds the default/optimizer/admin-agent templates. The **local** SQLite backend auto-applies these columns; on **Supabase**, run via SQL editor.

  > **New (v0.5+):** The **local** SQLite backend auto-applies two new columns to the `agents` table: `allowed_tools` (JSON array of disabled Tier-2 tool names — empty = all enabled) and `custom_tool_ids` (JSON array of opted-in DB tool IDs). These drive the interactive Agent Loop diagram editor in the UI. The `agents.py` API exposes `model`, `temperature`, and `max_tokens` as user-editable fields via `PUT /api/v1/agents/{id}`. Tool filtering is enforced at runtime in `app/tools/loader.py` so the LLM never sees disabled tools in its schema.

  > **New (v0.6+):** **Loop Logic Engine** — the `loop_logic` column (already present as `TEXT NOT NULL DEFAULT '[]'`) now drives runtime gating of five optional agent loop steps: `interrupt_chk`, `permission_chk`, `guardrails`, `delegation_chk`, and `skill_track`. Store a JSON object array (e.g. `[{"node": "skill_track", "enabled": false}]`) to selectively disable steps; a flat string array or empty array means all steps run (backward-compatible with existing agents). `LoopConfig` in `app/agent/loop_executor.py` parses this column and is called by `loop.py` before each gated step. The Agent Loop diagram in the UI lets users toggle these nodes per-agent; disabled nodes appear muted with a strikethrough label. No schema migration is required — the column already exists.

  > **New (v0.7+):** **Configurable Guardrails** — two new columns drive per-agent safety policies. `agents.safety_policy` (JSON) stores `destructive_tools` (tool names added on top of the hardcoded baseline), `auto_confirm` (skip the confirmation gate for automation agents), `blocked_imports` (extra modules forbidden in `create_tool`), and `max_concurrent_tools` (cap parallel tool execution). `tools.requires_confirmation` (boolean) marks individual user-created tools as destructive. At runtime `loop.py` merges all three sources into a single effective set — the hardcoded baseline (`DESTRUCTIVE_TOOLS`) ∪ `safety_policy.destructive_tools` ∪ any loaded tool with `requires_confirmation=True`. A **Safety & Guardrails** section in the agent Config tab lets users manage these settings with no code changes. The local SQLite backend auto-applies the schema via ALTER TABLE migrations; on **Supabase**, run `migrations/012_safety_policy.sql` via the SQL editor.

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
| `http://localhost:8080/visuals/users/<uid>/<slug>.html` | Served AutoAgent page output (ephemeral) |
| `http://localhost:8080/api/v1/pages?user_id=...` | AutoAgent pages REST API |
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

**D
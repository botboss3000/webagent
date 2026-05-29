# webAgent

A **FastAPI** service with a **tool-calling** LLM agent (OpenRouter), optional **Supabase** or **local SQLite** persistence, and a **vanilla JS** UI at **`/index.html`** (static assets under `/ui/`).

## Features

- **Chat (connection-independent runs)** — **`POST /api/v1/chat/send`** is the primary path: it saves the user message, starts the agent turn as a **supervised background run**, and returns immediately (`{status: "running", turn_id}`). The run is owned by the **Run Manager** (`app/agent/run_manager.py`) and is **not tied to any client connection** — leaving the page, closing the browser, switching sessions, or switching devices does **not** stop it. Only finishing, an explicit interrupt, or a server restart ends a run. **`POST /api/v1/chat/stream`** (SSE) is a thin fallback that starts the same run and tails its events; a disconnect there never cancels the run. `POST /api/v1/chat` (buffered, inline) is retained for non-UI callers. All turns go to **`interactions`**; prior turns for the same **`session_id`** are reloaded from the DB into the model context.
- **DB is the source of truth; the web chat is a viewer** — The frontend renders a session from the DB (**`GET /api/v1/db/session-messages`**, which now also returns each message's **`status`** and a **`run`** object describing any in-flight turn) and uses the WebSocket purely as a live accelerant. The assistant's answer **streams into the DB as it is generated** (the assistant row is created `status='streaming'` and updated as tokens arrive, then finalized `complete` / `interrupted` / `error`), so a cold device, a second device, or a tab that just regained focus can render the in-progress answer from a plain DB read. Both **user and agent messages** are broadcast over the per-user WebSocket so every device viewing a session sees them live (deduped by interaction id), and **every step of a multi-step turn renders as its own bubble** (each assistant message before a tool call, not just the final answer). A new message sent while the agent is still working **interrupts** the current run and starts a fresh one that includes the interrupted partial + the new message — the agent then reads it and decides whether to **stop, steer, or continue** (no keyword/intent guessing). The explicit Stop button is a pure interrupt. Mid-stream interrupt is honored within a long single completion, not only at turn boundaries.
- **Chat pill "thinking" indicator** — While the agent is working on the **current session's** turn, the chat input pill **glows** and a small bar — sharing the pill's faded glass surface and glowing in step with it — floats just above it, ticking through each interaction as a short note. Tool-call notes are prefixed with the inference-turn number (e.g. *"Turn 2: Toolcall get_time"*) — a single user message can drive several LLM turns (call → tools → call → …), tracked from the backend's `turn_start` pipeline events. The bar is **clickable**: it expands a panel listing **every tool call made this exchange**, each row tagged with its **turn number** and independently expandable to show that call's full **arguments + result** (with duration / error status; screenshot results render inline). Tool calls accumulate across all turns of the exchange. When the turn ends the glow/pulse stop; if the turn made any tool calls the bar **stays in a resting state** (*"N tool calls"*) so you can still open it and inspect — until the next turn resets it or you switch sessions. A turn with no tool calls just fades out. Driven entirely by the per-user WebSocket event stream (`tool_call` / `tool_result` / `stream` / `pipeline` / `user_message` / `response` events) for `app.currentSessionId`, in **`ui/js/chat-activity.js`** (handler wired into `ui/js/agentWs.js`; `chat.js` lights it instantly on send). Theme-aware (dark + light) via design tokens; respects `prefers-reduced-motion`.
- **WebSocket agent (receive-only)** — `GET` upgrade to `/api/v1/agent/ws`: per-user subscriber mode. Connects once per user, receives ALL agent events (user_message, stream, response, tool_call, tool_result, pipeline, db) for all of that user's sessions. **Does not send messages** — all user messages go through HTTP POST. **Resume after refresh / session switch:** every event is stamped with a monotonic `session_seq` (and `turn_id` / `turn_seq`). The handshake accepts an optional `resume: { session_id: last_session_seq }` map; the server replays any events newer than the client's last-seen seq from the in-memory `RunBuffer` for that session before going live. Mid-session resume is also supported by sending `{type: "resume", session_id, last_session_seq}` over the open socket. See **Stream buffer** below.
- **Run state + clean recovery** — A durable **`session_runs`** table (one row per session) records the in-flight turn — status (`running`/`complete`/`interrupted`/`error`), the turn id, the in-progress assistant row, and `latest_session_seq`. This lets a cold device (even after a server restart) discover an active run from the DB alone and know where to resume the live stream. On startup, **orphan cleanup** flips any run the previous process left `running` (and any assistant row left `streaming`) to `interrupted`, so no device hangs waiting on an answer that will never finish. Run lifecycle methods live on `StorageBackend` (`run_state_begin` / `run_state_set_assistant` / `run_state_update_seq` / `run_state_finish` / `run_state_get` / `run_state_list_active` / `cleanup_orphaned_runs`) — fully implemented for LocalBackend; the Supabase path degrades to RAM-only live replay until ported. Migration: **`migrations/023_run_persistence.sql`** (adds `interactions.status` + `session_runs`); local SQLite auto-migrates.
- **Stream buffer (per-turn, in-memory)** — Each active or recently-completed chat turn is held in `app/agent/run_buffer.py` as a `RunBuffer` keyed by `session_id`. It is now a low-latency replay cache **in front of** the DB (the source of truth), not the only copy. Every event emitted via `_emit_to_visualizers()` is stamped (session_seq / turn_id / turn_seq / emit_time) and appended. After the turn ends, the buffer stays in memory for `stream_buffer_retention_seconds` (default **60s**, configurable in **App Config → Storage → Stream Buffer**, clamped to 0–3600) so a refresh or session-switch right after completion still gets a RAM replay. A background sweeper drops stale buffers. The `interactions` table gained 3 columns — `session_seq`, `turn_id`, `turn_seq` (all nullable; legacy rows have NULL `session_seq` and fall back to `created_at` for ordering). Bulk-insert support via `StorageBackend.insert_interactions_batch()`. The `interactions` table also carries a `status` column (default `complete`). Migration: **`migrations/017_stream_persistence.sql`** + **`migrations/023_run_persistence.sql`** for Supabase; local SQLite auto-migrates.
- **Context** — Prompt slices from `context_type` / `doc_type`; if a user has no rows, **`context_templates`** are copied into per-user context on first chat.
- **Memory** — Hybrid search (FTS5 keyword + vector cosine similarity via embedding API) runs before each chat turn; results injected as `[BRAIN CONTEXT]` in the system prompt. Trivial messages (greetings, affirmations, commands) skip memory via regex gate. Page content auto-chunked and embedded on write. Background save of chat snippets into memory.
- **AutoAgent** — Multi-page workspace tab 🎨 (labeled **Dashboard** in the tab selector). Each user has a persistent set of named pages (home, dashboard, notes, custom). Pages stored per-user under `visuals/users/<user_id>/` with a `pages.json` manifest. The **home** page is auto-seeded with a webAgent onboarding/info page. Users add pages via the **+** dropdown nav button; each page gets its own dedicated agent persona (`agent_context` field in manifest). Prompts typed into the Dashboard's prompt bar are **handed off to the right-side web chat**: the UI lazily finds (or creates) a per-user **Visualizer** agent (cloned from the `visualizer` system template — see `app/context/agents/visualizer.json`), switches the chat to it, starts a fresh session, and submits the user's prompt — tagged `[User → UI Agent → Page: "<slug>" | Context: "..."]` — as the first message. From there the conversation continues in the right-side chat like any other agent session, while the iframe below the prompt bar re-renders whenever the agent calls `render_visual`. Powered by `render_visual`, `list_pages`, `create_page`, `delete_page` tools in **`app/visualizer/`**, plus `web_search` and `browser_action` for references/research. REST API at **`/api/v1/pages`**. Visuals served from `/visuals/users/` (ephemeral, Cloud Run safe). See [`app/visualizer/SKILL.md`](app/visualizer/SKILL.md).
- **Attachments** — Image, audio, video, and file uploads. Users attach files via the UI (📎 button in the chat pill, drag & drop or paste onto the pill). The 🎤 button on the same pill is now **voice dictation**, not file recording — it drives the browser's Web Speech API (`SpeechRecognition` / `webkitSpeechRecognition`) and streams transcribed text straight into the textarea so the user can edit before sending; the mic is hidden via the `.no-voice` class on browsers without `SpeechRecognition` (e.g. Firefox desktop). Files upload via **`POST /api/v1/upload`** and bytes are persisted through **`app/db/attachments/`** (local filesystem in dev, Supabase Storage in production — see `app/db/SUPABASE_STORAGE.md`). Metadata is stored in the **`attachments`** table (local SQLite or Supabase). The agent accesses files with the **`read_attachment`** built-in tool. Supports image preview, audio/video players, and download links inline in chat bubbles. Attachments persist per-session and survive server restarts. **Attachment Description (vision fallback):** when an attached image is handled by a model that can't see images, a separately-configured vision model (chosen per-model via the Image-in column in the **App Config → Models** grid) describes the image once; the description is folded into the user message as text and persisted to the turn, so even text-only models can "see" it. Surfaced as the **Attachment Description** node in the loop diagram (Context stage, per-agent on/off toggle).
- **Tools** — **Bootstrap + on-demand discovery model.** A small set of hardcoded core tools (list_tools, search_tools, get_tool_definition, memory, session_search, get_time, get_date, calculate, read_attachment) are always available from turn 1 via **`app/tools/loader.py`** + **`app/tools/core_tools.py`**. Heavier tools are gated behind admin-enabled **abilities** (see *Agent abilities* below): `web_search` / `get_weather` / `maps_geocode` (Web Access), `browser_action` / `http_request` (Browser Control), `generate_image` (Image Generation), `db_query` / source-edit suite (Codebase Admin), `create_tool` (Create Tools), `event_subscribe` family (Automation). All other tools (user-created, admin, comm plugins, webhook management) are discovered on demand via `list_tools` / `search_tools` / `get_tool_definition`. Tool definitions no longer auto-populate the system prompt — only curated `context_type="skills"` docs provide behavioral guidance in the `# [SKILLS]` section.
- **Agent abilities** — App Config → **Agent Abilities** (the same tab as integrations) bundles related tools into per-agent toggles. Built-in ability cards:
  - **Codebase Admin** — `read_source` / `write_source` / `edit_source` / `delete_source` / `run_command` / `restart_server` + `db_query` (edit own prompt slots). Privileged; destructive ops still need user confirmation at chat time.
  - **Create Tools** — `create_tool` (define new DB-persisted tools at runtime). Useful for ephemeral cloud deployments.
  - **Automation** — exposes the per-agent **Automation** tab and the `event_subscribe` / `list_event_sources` / `list_delivery_channels` / `list_event_subscriptions` / `event_unsubscribe` tools.
  - **Web Access** — `web_search` (DuckDuckGo), `get_weather` (Open-Meteo), `maps_geocode` (Nominatim — forward/reverse geocode + great-circle distance). All free-public, no platform credentials.
  - **Browser Control** — `browser_action` (persistent Playwright Chromium — headless by default, or it **attaches to the launcher's visible "App Window"** over CDP when one is open, so you can watch the agent drive the window) + `http_request` (arbitrary outbound HTTP).
  - **Image Generation** — `generate_image`. The image model is whichever saved model is ticked for **Image-out** in **App Config → Models** — image generation is no longer a separate provider panel. `pick_image_generator` (in `app/admin/settings.py`) selects that model from the unified LLM config (`auth_elements` `service="llm"`); a legacy `service="image_gen"` row is still honored as a fallback for older setups. The dispatch API style (OpenAI-compatible `/images/generations`, Stability, or Gemini/Imagen) is inferred from the model's base URL; image-out capability is auto-detected from the provider's `/models` metadata plus a model-id name heuristic (dall-e / gpt-image / flux / sdxl / imagen …). Dispatch happens in **`app/tools/image_generation.py`**; generated images persist under **`visuals/users/<user_id>/`** so they survive provider-URL expiry. Admin enable/disable of the ability goes through **`POST/DELETE /admin/integrations/abilities/{ability}`** (same pattern as the other abilities).
  - **Visualizer** — `render_visual`, `list_pages`, `get_page`, `create_page`, `delete_page`, `rename_page`. Page-authoring tools for the Pages workspace (`app/visualizer/`). Typically only the Visualizer / Page Builder agent has this on — without it, an agent cannot see the page-writing tools at all and will not opportunistically render conversational replies as HTML to the user's home page.
  - **Agent Orchestration** — `delegate_to_agent` / `list_delegatable_agents` (hand this session off to another agent mid-conversation) + `run_optimizer` (kick off the prompt optimizer). **Off by default, opt-in per agent.** Without it an agent cannot reach other agents or the optimizer — this is what stops an agent (e.g. the admin agent) from discovering those tools via `list_tools`/`search_tools` and looping on them. Unlike the other ability cards this one needs no App Config credential — it is a pure behavioral toggle (always "configured"; the per-agent switch is the only gate). The optimizer's **internal** pipeline tools (`run_worker_trials`, `handoff_to_closer`, `deploy_optimization`) are never exposed by this card — they load **only** for the optimizer's own Planner/Closer sub-agents (`opt_planner` / `opt_closer`).
- **Integration tools (OAuth-backed)** — Curated tools live under **`app/integrations/`** in a flat layout, grouped by capability rather than by provider so one file can host equivalents across providers side by side: **`email_tools.py`** (Gmail, Outlook, Yahoo identity), **`calendar_tools.py`** (Google Calendar, Outlook Calendar), **`files_tools.py`** (Google Drive, OneDrive, Dropbox), **`social_tools.py`** (Twitter/X, LinkedIn, Meta = Facebook + Instagram, Reddit, Pinterest, Snapchat identity), **`media_tools.py`** (TikTok, Twitch). Each tool dict declares `provider` so the loader only registers it when the active agent has the matching `agent_connections` row enabled — delete a file (or a single TOOLS entry) to remove that capability. Shared OAuth plumbing — token read, expiry-driven auto-refresh (Google / Microsoft / Dropbox / Yahoo / Reddit / Twitter / LinkedIn / TikTok / Twitch), bearer injection, single 401 retry — lives in **`app/integrations/oauth_helper.py`**. A generic **`oauth_api_call(provider, method, url, ...)`** tool is always registered as a fallback for endpoints we haven't curated. ~42 curated tools across all 12 supported providers. Two providers have curated coverage limited by the platform itself: Yahoo Mail (no public REST mail API; only `yahoo_userinfo`) and Snapchat (Snap Kit limited to identity; only `snapchat_userinfo`). See **`app/context/context_templates/skills.md`** for the agent-facing recipe doc.
- **OpenRouter** — Model from `OPENROUTER_MODEL` (see `.env.example`; e.g. `deepseek/deepseek-v4-flash`).
- **Parallel multi-provider** — Configure 2+ LLM providers in Settings. When enabled, the agent fans out each message to all providers simultaneously and uses the fastest complete response. Configured via `GET/POST /admin/settings/multi-providers`. Set `parallel_mode: true` and a list of provider entries (each with provider, base_url, api_key, model) in `provider.json` or DB `auth_elements`. Each entry also carries detected media **capabilities** (`text_capable` / `image_capable` / `image_out_capable`) and usage roles (`use_for_image` = read attached images; `use_for_image_out` = the image generator); when an image is attached the race includes **only image-capable providers** (a single image-capable provider still uses the race path), and if none can see images the Attachment Description step describes the image first so the text-only providers can race on the description. Per-model capabilities are detected on save (from the provider's `/models` metadata) and overridable via the **Text / Image-in / Image-out** columns in the **App Config → Models** grid.
- **Dual storage** — **`cloud`** (Supabase) vs **`local`** (SQLite file **`app/db/local.db`**). Mode is stored in **`app/db_mode.json`** and switched via **`/admin/db/*`**.
- **Pluggable storage** — Admin Storage modal (Config → Storage) extends the legacy cloud/local toggle into a three-section panel: **Application Data** (SQLite, Supabase, raw Postgres, GCP Cloud SQL, Neon, MySQL), **Secrets Vault** (App DB, env vars, OS Keyring, GCP Secret Manager, AWS Secrets Manager), and **Data Migration** (export/import JSON). Provider connection details are persisted to **`app/db_connection.json`**; secrets-provider choice is in **`app/secrets_mode.json`**; passwords and service tokens are stored via the active vault (never written to the JSON config). Canonical schema lives in **`app/db/schema/`** with dialect-specific DDL renderers (sqlite, postgres, mysql) and a "Show Schema SQL" / "Auto-Create Tables" button per provider. New endpoints under **`/admin/storage/*`** drive the modal; **`asyncpg`** + **`keyring`** are required in `requirements.txt`. Raw Postgres / MySQL providers currently support **Test Connection** and **Auto-Create Tables**; full-runtime activation for those backends is staged behind a `501` response until the SQLAlchemy data-layer port lands.
- **Administrator tools** — Optional filesystem read/write/edit/delete, shell command execution, and server restart exposed as agent tools (**`read_source`**, **`write_source`**, **`edit_source`**, **`delete_source`**, **`run_command`**, **`restart_server`**). Powered by **`app/admin/source.py`** + **`app/admin/source_tools.py`**. **These are privileged debug tools — NOT available in normal user operation.** Deleting the `app/admin/` directory removes them entirely. See the [Administrator Tools](#administrator-tools) section.
- **Per-agent external data sources** — Each agent can attach external data sources (`POST /api/v1/data-sources`, `POST /api/v1/agents/{agent_id}/data-sources`) that the agent can query at runtime: read-only Postgres / MySQL DBs (live queries with parameterized SQL + statement allowlist), document folders (`doc_store`: chunked + embedded into the `doc_chunks` table with FTS5 + vector hybrid search), generic REST APIs, and **domain-restricted website search** (`web_search_domain` — every query is forced through `site:<domain>`, the LLM cannot escape to the wider web). Each attachment becomes a synthetic tool at agent load time and contributes a snippet to the system prompt's `# [DATA SOURCES]` block. Tools are NOT persisted in the `tools` table — they regenerate from the connector registry on every request so config edits apply immediately. UI lives on the agent card → **Agent Config** tab → **External Data Sources** section. The agent loop diagram gains two nodes: `data_src_load` (LOOP INIT, registers connector tools) and `data_src_exec` (EXECUTION, fires when a connector tool runs). See migration **`migrations/014_data_sources.sql`** and the connector implementations under **`app/connectors/`**.
- **Multi-agent system** — Multiple agent templates can be defined in `context/agents/`. Each user can have their own default agent. The agent loop supports **mid-turn delegation**: the `delegate_to_agent` tool lets the active agent hand off to another agent within the same session (rebinds session, reloads tools, injects a system-prompt switch message). Pipeline events (`agent_delegation`) are emitted to the Loop/Flow panel for visibility. The `delegate_to_agent` / `list_delegatable_agents` tools are **opt-in via the Agent Orchestration ability** (off by default) — they are no longer auto-injected into every non-pipeline agent.
- **Loop / runaway guard** — `app/agent/loop.py` protects every agent from going "rogue". If the agent calls the **same tool with identical arguments** too many times it is blocked and told to change approach; repeated strikes (or a wall-clock cap) end the turn-loop **gracefully** with a clear message instead of spinning until the request times out. Tunable via `AGENT_MAX_IDENTICAL_TOOL_CALLS` / `AGENT_MAX_STALL_STRIKES` / `AGENT_MAX_WALL_SECONDS` (see *Environment variables*). The optimizer also never auto-fires on the **admin agent**.
- **Agent Management UI** — **Agents tab** in the main UI (🤖) for browsing, creating, editing, and deleting agent templates. Each agent card expands inline into its own detail panel (Config / Tools / Agent Loop tabs); multiple rows can be open simultaneously. Supports all template fields (name, description, icon, system prompt, model, temperature, max tokens, trigger description, access level, pipeline flag). Light mode aware. **Interactive Agent Loop diagram** — clicking any node on a custom agent's loop diagram opens an in-place edit panel: prompt sections (Load Context / Build Prompt nodes), memory search toggle, LLM model/temperature/max-tokens, per-tool Tier-2 toggles (Execute Tools node), category-level guardrails, max-turn-count slider, and memory-save toggle. Changes are saved via `PUT /api/v1/agents/{id}` and enforced at runtime via `allowed_tools` and `custom_tool_ids` columns on the `agents` table. **Per-node loop gating** — five optional steps (`interrupt_chk`, `permission_chk`, `guardrails`, `delegation_chk`, `skill_track`) can be individually toggled on/off per agent via the loop diagram UI; the state is stored in the `loop_logic` JSON column as an object array and respected by `LoopConfig` inside `app/agent/loop_executor.py` at runtime. Disabled nodes render muted with a strikethrough label in the diagram. **Save as Template** — admin users see a **Save as Template** button on the Config tab of any custom agent; it captures the agent's current config + admin-base prompt slots into a new `agent_templates` + `agent_prompt_templates` pair (slots saved with `source='admin'` so JSON re-seed leaves them alone). The new template appears alongside built-in templates in the "New Agent" picker.
- **Admin users** — User admin flag stored in `user_profiles.is_admin`. The first admin is bootstrapped via `BOOTSTRAP_ADMIN_ID` env var. Admin users have access to the `admin-agent` template (a privileged agent with filesystem/shell tools). `GET /admin/users` and `POST /admin/users/{user_id}/set-admin` manage the admin list (admin-only endpoints).
- **Login tracking** — User login activity is tracked in `user_profiles` table. On first login (via `/api/v1/auth/login`, `/api/v1/auth/register`, or `/api/v1/auth/recall`), a `user_profiles` row is created with `created_at` (first login timestamp) and `display_name` from `users.json`. On each subsequent login, `last_login_at` is updated to the current timestamp. Historical login data is not persisted; only the most recent login time is recorded. **To reset all local users:** delete **`app/auth/users.json`** and restart the server — it will recreate with default admin:admin.
- **Account menu & multi-account switcher** — The header right-side shows a circular **letter-icon avatar** (first letter of the active user's display name / email). Clicking it opens a Google-style dropdown with a large current-user row + **Manage Account** button, a list of other signed-in accounts the user can switch to instantly (no password prompt — uses each account's stored remember-token via `/api/v1/auth/recall` if the cached JWT is expired), an **Add account** row that opens the sign-in modal, the existing Theme toggle, and Sign Out. The set of signed-in accounts lives in browser `localStorage` under `webagent_accounts` (active id under `webagent_active_user_id`); the legacy single-account keys (`auth_token`, `auth_username`, `auth_user_id`, `auth_display_name`, `remember_token`) are kept in sync as mirrors of the active account so all existing code keeps working.
- **Manage Account tab** — `Manage Account` is a main-panel tab (`#tab-account`, hidden behind the avatar dropdown's "Manage Account" button). Lets the authenticated user change their email/username and display name (**`PATCH /api/v1/auth/me`**, returns a fresh JWT), change their password with current-password verification (**`POST /api/v1/auth/change-password`**, also rotates the remember token), and delete their own account with password confirmation (**`DELETE /api/v1/auth/me`** — the bootstrap `admin_default` user is protected and returns 403). `GET /api/v1/auth/me` returns the active user's profile. All four endpoints require Bearer-token auth.
- **Access modes & User Management** — App Config → **User Management** (admin only) controls who can join. Four modes stored in `app-settings.json` as `access_mode`:
  - `public_anonymous` *(default)* — anyone can join; anonymous visitors can chat without signing in.
  - `public_registered` — anyone can register, but chat is gated until a user signs in.
  - `admin_approval` — registration creates an account flagged `is_approved=false`; login is blocked with 403 until an admin approves them from the User Management table.
  - `private` — registration is disabled. The sign-in modal hides the "register" link and `/api/v1/auth/register` returns 403.
  Public endpoint **`GET /api/v1/auth/access-mode`** lets the UI (sign-in modal + chat gate) read the policy without admin auth. Admin-only endpoints: **`GET /admin/users/stats`** (users + session/interaction counts + approval state), **`POST /admin/users/{user_id}/approve`**, **`POST /admin/users/{user_id}/revoke`**, **`DELETE /admin/users/{user_id}`**. `User.is_approved` is persisted in `app/auth/users.json`.
- **Agent selector** — Dropdown in the chat header (next to session selector) lets users pick which agent to chat with. Shows the user's custom agents. Switching agent auto-creates a new session (sessions are bound to a single agent). Selection persists in `localStorage`. Each row carries a **Config button** (opens the agent on the Agents page) and a **Delete button** (confirm prompt, then `DELETE /api/v1/agents/{id}`) on the right; **press-and-hold the row to rename** it inline (`PUT /api/v1/agents/{id}` with `name`), and **press-and-hold the grip handle on the left to pin/unpin**. **Drag the grip handle on any row to reorder agents** — the order is saved per-account in the `agents.sort_order` column via **`POST /api/v1/agents/reorder`**, so it follows the user across devices, and the **Agents page (🤖) renders in that same order**. Pins stay a per-user `localStorage` flag and still float to the top in both the dropdown and the Agents page.
- **Session selector** — Custom dropdown in the chat header. Each row carries a **delete button on the right** (fires a confirm prompt, then `DELETE`). **Press-and-hold the row to rename** it (inline edit), and **press-and-hold the grip handle on the left to pin/unpin** (sticks the session to the top of the list, persisted in `sessions.pinned`). A `+` button next to the dropdown starts a new session. **Drag the grip handle on a row to reorder sessions** — the order is saved per-account in the `sessions.sort_order` column via **`POST /api/v1/db/sessions/reorder`**. Pinned sessions sort above the rest, then by manual `sort_order` (NULLS LAST), then `created_at DESC`. (Long-press + drag-to-reorder helpers live in `ui/js/ordering.js`; the dropdown wiring is in `ui/js/sessions.js`.)
- **Agent automations (scheduled tasks)** — Each agent card has an **Automation** tab where the user writes free-form English describing scheduled work (e.g. *"every weekday at 9am, summarize my unread Telegram messages and send the summary via Telegram"*). On save, the LLM parses the file into structured task rows stored in the **`agent_automations`** table (label, cron expression, prompt, channel, recipient, silent flag, next_run_at). A scheduler backend (default: **`LocalScheduler`** — in-process asyncio poll loop, every 30s) fires due rows by creating a fresh session under the agent owner and calling **`run_agent_loop_buffered()`**, then dispatches the reply via the chosen channel plugin (`telegram`, `sms`, `whatsapp`, `slack`, `discord`, `email`) or runs silently. **Remote backends** push each automation to an external cron service whose target is the public webhook **`POST /api/v1/automations/fire/{automation_id}?token=<fire_token>`**; supported providers: **`google_cloud`** (Cloud Scheduler via service-account JSON + JWT auth), **`cronjob_org`** (cron-job.org REST API), **`generic_webhook`** (POSTs job specs to an admin-supplied URL — wire it to AWS EventBridge, n8n, self-hosted cron, etc.). Backend choice is configured in **App Config → Automation**, persisted to **`scheduler_config.json`** under a per-provider settings map. Endpoints: **`GET/POST /api/v1/agents/{id}/automations`**, **`POST /api/v1/agents/{id}/automations/parse`**, **`PATCH /api/v1/agents/{id}/automations/{task_id}`**, **`POST /api/v1/agents/{id}/automations/{task_id}/run-now`**, **`POST /api/v1/automations/fire/{id}?token=...`** (remote webhook target), **`GET/POST /admin/settings/scheduler`**, **`GET /admin/settings/scheduler/providers`** (field schema for the dynamic config form), **`POST /admin/settings/scheduler/test`** (provider Test Connection), **`POST /admin/settings/scheduler/sync`** (re-push all jobs), **`GET /admin/scheduler/status`**. Cron expressions require **`croniter`**; Google Cloud Scheduler integration uses **`httpx`** + the standard-library + the existing **`cryptography`** dep for RS256 JWTs (no extra `google-cloud-*` packages). Local scheduler keeps state in the DB so it resumes after restart; for stateless Cloud Run deployments switch to a remote provider so jobs survive container recycles.
- **Agent event triggers (push & poll)** — A general "when X happens, fire the agent" abstraction that lives alongside the cron-based automations. Each connected external service is an **`EventSource`** plugin under **`app/events/sources/`**; a source declares its event types (`message_received`, `event_added`, `file_modified`, `mention`, ...), implements either **push** (real-time provider webhook / watch) or **poll** (cadence-based fetch), and the central router (**`app/events/router.py`**) turns each normalized event into one or more agent runs via the same **`run_agent_loop_buffered()`** entrypoint the scheduler uses. Subscriptions are written by the same automation-file LLM parser — lines like *"when an email arrives from any airline, summarize it"* land in **`agent_event_subscriptions`** with a per-source filter (Gmail search syntax for Gmail, channel id for Slack, etc.). The agent itself is told which delivery channels are available and **asks the user where to send the message** rather than guessing. Push sources are real: **Gmail** (`users.watch` → Cloud Pub/Sub), **Outlook Mail / Calendar / OneDrive** (MS Graph `/subscriptions`), **Google Calendar / Drive** (channels API), **Dropbox** (app-level webhook + per-user cursor), **Shopify** (per-shop webhooks), plus a **comms bridge** that emits events from inbound **Telegram / Slack / Discord / SMS / WhatsApp** messages. Poll sources: **Twitter** (mentions), **Reddit** (inbox / mentions). Two background loops in **`app/events/`** keep things running: a **poller** (every 15s, calls `source.poll`) and a **renewer** (hourly, re-registers push subs before their provider-side TTL expires). HTTP intake at **`POST /api/v1/events/{source}`** verifies + normalizes each provider's payload (Pub/Sub OIDC JWT for Google sources, `validationToken` handshake + `clientState` for Graph, `X-Goog-Channel-Token` for Drive/Calendar, HMAC for Shopify, etc.). Manifest + subscription introspection: **`GET /api/v1/events/sources`**, **`GET /api/v1/events/subscriptions`**. Per-source Pub/Sub / Graph / webhook setup is a **one-time admin task** — see **[`docs/events-setup.md`](docs/events-setup.md)** for the exact GCP / Azure / Dropbox / Shopify steps and the env vars (`EVENTS_GMAIL_PUBSUB_TOPIC`, `EVENTS_PUBSUB_AUDIENCE`, `EVENTS_GRAPH_NOTIFICATION_BASE`, `EVENTS_GCAL_NOTIFICATION_BASE`, `EVENTS_GDRIVE_NOTIFICATION_BASE`, `EVENTS_DROPBOX_WEBHOOK_BASE`, `EVENTS_SHOPIFY_WEBHOOK_BASE`). Schema lives in migration **`migrations/016_event_triggers.sql`** (`agent_event_subscriptions` + `event_deliveries` audit/dedup log). Adding a new source is one new file under `app/events/sources/`; no router / executor / parser / scheduler changes needed.
- **Admin Tools tab (file editor + in-page terminal)** — Replaces the old "File Manager" and standalone "Terminal" tabs with a single admin workspace (sidebar + main tab strip). The sidebar still toggles between **Explorer** (file tree) and **Source Control** (GitHub manager); a third button next to those — and a hotkey **`Ctrl+\``** anywhere in the app — opens a fresh terminal as a tab next to the open files. Multiple terminals run side by side as independent shells.
  - **Admin-only access (three layers).** Non-admins never see Admin Tools, and the sub-pages inside (Admin Configuration / Database / File Manager / Terminal / Source Control / Interactions / Runtime Loop) are gated together: (1) `_applyAdminToolsVisibility` in `ui/js/main.js` hides the top-tab button + dropdown option and bounces non-admins off the tab if it was their saved `lastActiveTab`; (2) `startAdminTools` in `ui/js/files.js` calls **`GET /check-access`** on activation and, on `is_admin = false`, reveals **`#files-restricted-overlay`** while hiding `#admin-tools`; (3) sub-page switching in `applySidebarView` + the strip click handler skip per-view side effects (polling, lazy panel loads) when the local `isAdmin` flag is false, and a strip click for a non-admin re-shows the overlay instead of activating the sub-view. The **`showRestrictedModal()`** helper in `ui/js/left-login.js` is the single source of truth for the restricted panel (also reused by the action-level write guards inside `app-config.js`).
  - **Independent PTYs per tab.** Each terminal tab generates a client-side UUID (`terminal:<uuid>`) used as the backend session_id. The server keeps a `_sessions: Dict[str, TerminalSession]` map, so opening N tabs spawns N separate PTYs that don't share output. See `app/api/terminal.py`.
  - **Sessions survive refresh / browser close.** WebSocket disconnect doesn't reap the PTY — it's only killed when the user clicks the tab's **X**, types `exit` in the shell, or the server shuts down. The browser persists the tab list (with session_ids) to `localStorage`, and on reload reconnects with the same id, reattaching to the running shell.
  - **Scrollback replay on reattach.** Each `TerminalSession` keeps a 64 KB ring buffer of recent PTY output. On WS attach it's sent before live streaming so a refreshed/restored tab shows the recent context instead of an empty xterm.
  - **Confirmed close.** Clicking **X** dims the tab, swaps the X for a spinner, and fires `DELETE /api/v1/terminal/sessions/{id}` with a 10s timeout. The tab is only removed from the UI after the backend confirms the PTY is reaped; on failure the tab snaps back and the user can retry — a still-running shell can never be silently closed.
  - **Per-tab status dot.** A small dot bottom-right of the tab icon is green when the WS is connected, amber and pulsing while connecting/reconnecting, red on auth failure or hard stop.
  - **Inline rename.** Double-click a terminal tab's label to rename it. The name persists with the session id, so a refreshed tab keeps its label.
  - **Auth on the WebSocket.** `/api/v1/terminal/ws` decodes the JWT from `?token=` and rejects non-admins with close code 4401 *before* `accept()`. The client doesn't auto-reconnect on 4401. The `DELETE` endpoint also requires an admin Bearer token.
  - **Auto-reconnect with exponential backoff** (500 ms → 30 s) on transient WS drops, plus a 25 s heartbeat ping to keep proxies from killing idle sockets.
  - **Mobile.** On mobile, opening a new terminal automatically collapses a maximized sidebar to the icon strip so the main panel — and the new terminal — is visible.
  - **Shortcut keybar.** A sticky chip bar at the bottom of the terminal pane carries Esc / Tab / arrows / Shift+Enter / Ctrl chord / tmux prefix / common Ctrl combos (^C ^D ^L ^R ^Z) / hard-to-reach chars (`|` `~` `/` `\` `` ` ``) / Copy / Paste / mic. The keyboard-icon button in the tab bar toggles it; default ON for touch/`max-width: 800px`, OFF for desktop, user preference persisted to `files.terminalKeybarVisible`. **Ctrl / tmux chips:** tap = arm one-shot, long-press = lock until tapped again. While armed, the next chip OR the next soft-keyboard letter sends as `Ctrl+<key>` (via `window.__termInputTransform` wired into `terminal.js`'s `term.onData`). tmux arm/lock sends the `Ctrl+B` prefix (`\x02`) so the next key is the tmux command.
  - **Mobile gestures.** Two-finger pinch on the terminal pane scales the font (shared across all open terminals via `setTerminalFontSize`). Horizontal swipe on the tab strip switches between open terminal tabs. `visualViewport.resize` refits the active terminal whenever the soft keyboard opens/closes so the prompt isn't hidden.
  - **Mic dictation.** The keybar's mic chip uses the Web Speech API (`SpeechRecognition` / `webkitSpeechRecognition`) and pastes recognised text into the active PTY without a trailing newline so the user can review before submitting.
  - **Cross-device session resume.** The terminal launcher sidebar's **Your sessions** section calls `GET /api/v1/terminal/sessions` and shows live PTYs the caller owns. Each row carries the friendly name supplied by the originating client (`?name=` on WS open, or `{"type":"set_name"}` for live updates). Clicking a row opens a tab against that session_id; the WS handler reattaches to the running shell and replays the scrollback ring buffer.
- **Progressive Web App (PWA)** — webAgent is installable on desktop and mobile via the browser's **Add to Home Screen** prompt. A **Web App Manifest** (`ui/manifest.json`) declares the app name, icons, display mode (`standalone`), and theme colour. A **Service Worker** (`sw.js`) precaches the app shell (HTML, CSS, JS, icons) for instant loading, uses stale-while-revalidate for CDN assets, and network-first fallback-to-cache for navigation. The theme-color meta tag syncs with the user's light/dark theme choice. App icons (192×192 and 512×512, with maskable variants) live in `ui/icons/`.
- **Web UI** — Main page at **`/`** (also reachable at **`/index.html`**): chat, DB viewer, Admin Tools (file editor + multi-tab terminal), stream/loop, agents.
- **Minimal tester** — **`GET /test`** serves **`ui/test_interface.html`** (same origin as the API).

## Architecture and module map

### HTTP + WebSocket Architecture

The agent loop (`app/agent/loop.py`) runs as a **supervised background task owned by the Run Manager** (`app/agent/run_manager.py`), independent of any client connection. All user messages enter via **HTTP POST** (no WebSocket sends): `POST /api/v1/chat/send` saves the message + starts the run and returns immediately. The WebSocket is a **receive-only subscriber** — connected once per user, it receives events for all of that user's sessions. The DB is the source of truth (assistant answers stream into `interactions`; `session_runs` tracks the live turn); the WebSocket is a live accelerant.

```text
       [CLIENT]
          |
          |--- (HTTP POST) ---> [ app/api/chat.py ]   send message; run starts server-side
          |                           |
          |                    +-- POST /api/v1/chat/send    (fire-and-forget; primary)
          |                    +-- POST /api/v1/chat/stream  (SSE fallback; tails the run)
          |                    +-- POST /api/v1/chat         (buffered, inline)
          |                    +-- POST /api/v1/chat/interrupt  (graceful stop)
          |                           |
          |                    [ app/api/uploads.py ]        POST /api/v1/upload
          |                    store_file()  → bytes saved + attachment row
          |                           |
          |                           v
          |              +--------------------------+
          |              | SUPERVISED BACKGROUND RUN|
          |              | run_manager -> loop.py   |
          |              | (survives disconnects)   |
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
                                      optional resume: { session_id: last_session_seq }
                                      receives: stream, response, tool_call,
                                                tool_result, pipeline, db events
                                      for ALL sessions belonging to user
                                      On (re)connect, replays any buffered
                                      events with session_seq > last seen
                                      from app/agent/run_buffer.py.

Events are routed on the frontend:
  - "stream" / "response" events for the CURRENT session → chat bubble
       (SSE drives the bubble for the in-flight POST; WS takes over for
        replays after refresh / session-switch via
        `appendStreamToActiveBubble` and `finalizeAgentResponse`)
  - "tool_call" / "tool_result" / "pipeline" / "db" events → stream/loop/flow debug panels
  - Every event carries `session_seq`; the client tracks the highest seq
    per session in `app.lastSessionSeq` so the next WS handshake can
    request replay of only-newer events.
```

### Backend (`app/`)

| Module | Role |
|--------|------|
| **`main.py`** | FastAPI app: routers, CORS, no-cache for `/`, `/ui/`, and `/index.html`, **`StaticFiles`** for `/ui/` and `/screenshots`, **`GET /`** serves the main UI (`index.html`), **`GET /index.html`** (alias for legacy bookmarks), **`GET /test`**, **`GET /privacy`** (serves `ui/privacy.html`), **`GET /tos`** (serves `ui/tos.html`), **`GET /health`**, **`GET /favicon.ico`** + **`GET /favicon.svg`**, **`GET /sw.js`** (PWA service worker at root scope), **`GET /ui/manifest.json`** (PWA manifest with correct `application/manifest+json` MIME type), **`POST /api/v1/restart`**, shutdown (browser + terminal). |
| **`api/chat.py`** | **`POST /api/v1/chat/send`** (fire-and-forget; primary), **`POST /api/v1/chat/stream`** (SSE fallback that tails the run), **`POST /api/v1/chat`** (buffered, inline), **`POST /api/v1/chat/interrupt`** (graceful stop via the Run Manager). `_prepare_send()` does the synchronous prep (auth, agent + access/billing gating, user-message persist, RunBuffer + `session_runs` start, user_message broadcast); `_run_turn_background()` is the connection-independent turn executor (memory search → prompt build → attachment/vision fallback → history → agent loop → emit/persist → run-state finalize) handed to the Run Manager via `RunManager.start_or_replace` (a new message interrupts any active run and replaces it); `_sse_tail_run()` backs the SSE fallback. Also: **listener registries** — `register_user_listener()` / `register_visualizer_listener()` for per-user and per-session WebSocket broadcasting. |
| **`api/agent.py`** | **`WebSocket /api/v1/agent/ws`** — **receive-only per-user subscriber**. Client sends `{"mode": "user_subscriber", "user_id": "..."}` to register. Server streams all agent events (user_message, stream, response, tool_call, tool_result, pipeline, db) for all of that user's sessions. No message processing — all sends go through HTTP POST. |
| **`api/uploads.py`** | **`POST /api/v1/upload`** — multipart file upload (images, audio, video, PDF, text). **`GET /api/v1/upload/{id}`** — metadata lookup. **`DELETE /api/v1/upload/{id}`** — delete. File bytes stored via `app/db/attachments/`. |
| **`api/terminal.py`** | **`WebSocket /api/v1/terminal/ws?session_id=<uuid>&token=<jwt>&name=<label>`** + **`DELETE /api/v1/terminal/sessions/{session_id}`** + **`GET /api/v1/terminal/sessions`** (caller's own live PTYs, used by the **Your sessions** sidebar so the user can reattach to shells started on another device) + **`GET /api/v1/terminal/quick-launches`** + **`GET /api/v1/terminal/tmux-sessions`**. Multi-session browser shell (PTY / **`pywinpty`** on Windows). One `TerminalSession` per client-supplied `session_id`, kept alive across WS reconnects so refresh / browser-close doesn't kill a running shell. Each session stores a 64 KB scrollback ring buffer (replayed on every attach) and a friendly `name` supplied via the `?name=` query param or a `{"type":"set_name"}` control message, surfaced on the list endpoint. The WS handler decodes the JWT and requires `user_profiles.is_admin = 1`; unauthorized connects close with code 4401. Dead PTYs are reaped lazily on each new session lookup. |
| **`api/webhooks.py`** | **`POST /api/v1/webhooks/{plugin_name}`** — communication channel webhooks (Telegram, WhatsApp, etc.). Delegates to plugins for auth and parsing. |
| **`api/webhooks_generic.py`** | **`POST /api/v1/webhooks/generic/{webhook_id}`** — generic inbound webhooks. Receives any external payload, routes to agent loop with custom instructions, returns agent reply. Logs all events for review. |
| **`api/events.py`** | **`POST /api/v1/events/{source}`** — push intake for event sources (Gmail Pub/Sub, Graph subscriptions, Drive/Calendar channels, Dropbox/Shopify webhooks). Source plugin owns verification + normalization; the router fans out to matching subscriptions. **`GET /api/v1/events/sources`** returns the source manifest (used by the automation parser). **`GET /api/v1/events/subscriptions`** lists subscriptions. |
| **`events/`** | Generalized event-trigger subsystem. **`base.py`** = `EventSource` abstract; **`manager.py`** = auto-discovers sources under `sources/`; **`router.py`** routes a `NormalizedEvent` to all matching `agent_event_subscriptions` rows; **`executor.py`** creates a session + runs the agent loop with the event payload injected and a `[DELIVERY CHANNELS]` overlay so the agent can ask the user where to send the result; **`poller.py`** (15s tick) drives poll-only sources; **`renewer.py`** (hourly) refreshes push subs before TTL; **`providers/google_pubsub.py`** + **`providers/graph_subscription.py`** are shared verification/subscription helpers. **`sources/`** has one file per integration: `gmail_source.py` (push via Pub/Sub when `EVENTS_GMAIL_PUBSUB_TOPIC` is set, else automatic 60s poll fallback via `users.history.list`), `outlook_mail_source.py`, `google_calendar_source.py`, `outlook_calendar_source.py`, `google_drive_source.py`, `onedrive_source.py`, `dropbox_source.py`, `shopify_source.py`, `twitter_source.py`, `reddit_source.py`, plus comms-bridge shims `telegram_source.py` / `slack_source.py` / `discord_source.py` / `twilio_sms_source.py` / `twilio_whatsapp_source.py`. |
| **`api/db_viewer.py`** | **`/api/v1/db/*`** — SQLite introspection; DB files under **`app/db/`** (default filename **`local.db`** for the UI query param `db=`). **`GET /api/v1/db/session-stats`** — aggregated per-session usage stats (tokens, duration, cost, turn count). **`GET /api/v1/db/session-messages`** — a session's messages for the chat viewer, each with its lifecycle `status`, plus a `run` object describing any in-flight turn (status + `latest_session_seq`) so a cold/second device can show the live indicator. **`PATCH /api/v1/db/sessions/{id}`** — update a session's `title` and/or `pinned` flag. **`POST /api/v1/db/sessions/reorder`** — persist the manual drag order (writes `sessions.sort_order` by index, scoped to the requesting user). **`DELETE /api/v1/db/sessions/{id}`** — delete a session and its interactions. |
| **`agent/loop.py`** | Unified multi-turn loop (streaming + buffered): tool validation, parallel tool runs, pipeline events. **Streams the assistant answer into the DB**: the assistant row is inserted `status='streaming'` on first token, throttle-updated as text arrives (`AGENT_STREAM_PERSIST_INTERVAL`, default 0.6s), and finalized `complete` / `interrupted` / `error`; mid-stream interrupt is polled at the persist cadence. Emits per-step assistant events (`stream`/`response`/`agent_step_end` carry the `asst_id` of that step so the UI shows every step as its own bubble) and `attachment` events for frontend file rendering. Reads `LoopConfig` from the agent record to gate optional steps at runtime. Builds the effective destructive-tool set from `DESTRUCTIVE_TOOLS` baseline ∪ `agents.safety_policy.destructive_tools` ∪ per-tool `requires_confirmation` flags; supports `auto_confirm` and `max_concurrent_tools` from safety policy. **`run_command` per-arg exemption:** read-only inspect commands matching `SAFE_RUN_COMMAND_PREFIXES` (e.g. `git status`, `git log`, `git diff`, `ls`, `cat`, `grep`, version probes) bypass the guardrail confirmation gate; commands containing `;`, `&&`, `||`, `>`, `<`, `` ` ``, or `$(` are always treated as mutating. Emits `guardrail_skip` pipeline event when exempted. |
| **`agent/loop_executor.py`** | **`LoopConfig`** — parses the `loop_logic` JSON column from an agent record and exposes `is_enabled(node_id, context=)`. Supports two formats: flat string array (legacy, all nodes enabled) and object array (`[{"node": "skill_track", "enabled": false, "run_if": "expr"}, ...]`). Defines `LOCKED_NODES` (steps that can never be disabled: `user_input`, `load_tools`, `llm_call`, `execute_tools`, `check_continue`, `final_response`) and `GATED_NODES` (11 steps with runtime gating: `interrupt_chk`, `permission_chk`, `guardrails`, `delegation_chk`, `skill_track`, `memory_search`, `memory_save`, `fire_optimizer`, `copy_defaults`, `data_src_load`, `attachment_describe`). Pre-loop nodes (`memory_search`, `memory_save`, `copy_defaults`, `attachment_describe`) are gated in `chat.py`; in-loop nodes in `loop.py`. Includes `_evaluate_run_if()` for conditional node execution (supports `==`, `!=`, `>`, `<`, `>=`, `<=`, `!key`, `key in [a,b,c]`). |
| **`agent/session_history.py`** | Maps **`interactions`** rows → OpenAI-style **`messages`** for the active session (excludes internal memory tools). |
| **`agent/run_manager.py`** | **Run Manager** — server-side owner of every agent turn. Holds a strong, supervised reference to each run (keyed by `session_id`) so it survives client disconnects and cannot be garbage-collected mid-run; `start_run`, `start_or_replace` (a new user message interrupts the active run — graceful then hard-cancel — and starts the replacement once the prior run has finalized), `is_running` / `active_sessions`, and `interrupt` (sets the DB flag the loop polls). |
| **`agent/run_buffer.py`** | In-memory per-session `RunBuffer` registry for live WS replay — a low-latency cache in front of the DB (the source of truth). Stamps every emitted event with monotonic `session_seq` / `turn_id` / `turn_seq` / `emit_time`. Buffers persist `stream_buffer_retention_seconds` after a turn completes so a refresh/session-switch right after the turn ends still replays from RAM. Background sweeper drops stale buffers every 30s. Used by `app/api/chat.py` (`_emit_to_visualizers` stamps; `chat` / `chat_stream` call `start_turn` and `end_turn`) and `app/api/agent.py` (WS handshake + mid-session `resume`). |
| **`agent/prompts.py`** | Assembles the system prompt from the resolved per-caller slot list plus brain context and attachments. Slot resolution (admin base + user overrides, lock + replace/append merge mode) lives in `app/db/local.py`. Includes **`format_attachments_for_prompt()`**, **`build_user_message_content()`** (inlines images for vision models), and **`describe_image_attachment()`** (single-call vision fallback for non-multimodal models) helpers. |
| **`agent/error_classifier.py`** | Structured tool errors (**used on the WebSocket / streaming path**). |
| **`context/agents/`** | **Agent template JSON files** — seed `agent_templates` table (model/temperature/etc.) and the template's admin-base prompt slot rows in `agent_prompts`. A JSON file may declare slots explicitly via a `slots` array, or use the legacy flat keys (`system_prompt`, `agent_prompt`, `user_prompt`, `skills_prompt`, `tasks_prompt`, `misc_prompt`, `bootstrap_tools`) which the seeder converts into slots automatically. Included: `default.json`, `store-support.json`, `personal-assistant.json`, `enterprise-db-agent.json`, `opt_planner.json`, `opt_closer.json`, `admin-agent.json`, `integration-admin-agent.json`, `visualizer.json` (Dashboard page-builder — see the **AutoAgent** feature bullet), `source-controller.json` (the **⭐ Source Controller** — a git-only agent the Source Control view's star button hands off to; reviews the diff, writes a commit note, runs safety checks, then commits + pushes). |
| **`context/context_templates/`** | **Context template .md files** — seed `context_templates` table per context_type (agent, user, skills, tools, tasks, memory, project, jobs). Copied to user context on first chat. |
| **`agent/embed.py`** | Embedding utility using same provider config as chat. Returns configurable-dimension vectors (`EMBED_DIM`, default 1536). |
| **`db/__init__.py`** | **`get_db()`** → **`SupabaseBackend`** or **`LocalBackend`** from persisted mode. Honors `WEBAGENT_CONFIG_SOURCE=env` for Cloud Run (reads `WEBAGENT_DB_MODE` env var instead of `db_mode.json`). |
| **`db/schema/`** | Canonical Python schema definitions (`tables.py`) + dialect renderers (`ddl_renderer.py`) producing SQLite / Postgres / MySQL CREATE TABLE statements from one source of truth. Used by the Storage modal's "Show Schema SQL" and "Auto-Create Tables" buttons. |
| **`db/connection_config.py`** | `DBConnectionConfig` dataclass — provider, host/port/database/username, ssl_mode, schema, supabase_url, password_secret_key. Persists to **`app/db_connection.json`** (env-locked in Cloud Run). Builds SQLAlchemy-style URLs for asyncpg / aiomysql. |
| **`db/postgres_backend.py`** | Raw asyncpg helpers for **Test Connection** and **Auto-Create Tables**. Full StorageBackend port pending — runtime activation responds `501` until ported. |
| **`db/migration.py`** | Data migration — streams the current backend's tables as a JSON document (export) and bulk-inserts a previously-exported document into the active backend (import). Used by **`POST /admin/storage/migrate/{export,import}`**. |
| **`secrets/`** | `SecretsBackend` interface + impls: **InlineDBSecrets** (default; stores in `auth_elements.secret_ref`), **EnvSecrets** (read-only `WEBAGENT_SECRET_*` env vars), **OSKeyringSecrets** (`keyring` package — Windows Credential Manager / macOS Keychain / Linux Secret Service), **GCPSecretManager**, **AWSSecretsManager**. Factory at **`app.secrets.get_secrets()`** mirrors `app.db.get_db()` pattern. Provider choice persisted to **`app/secrets_mode.json`** (env-locked in Cloud Run). |
| **`db/supabase.py`** | Cloud: **`sessions`**, **`interactions`**, **`context`**, **`context_templates`**, **`attachments`**, memories / tools / skills per shared schema. |
| **`db/local.py`** | Local SQLite — schema init, FTS5 + vector hybrid search, embed-on-write, knowledge graph, timelines, **`user_profiles`** (tracks `created_at` and `last_login_at`), **`webhook_registrations`** and **`webhook_event_log`** tables. **`agents`** table includes **`user_mode`** (`anonymous` \| `register`) — controls whether channel users stay anonymous or are guided through registration/account-linking across channels. Prompt content lives in **three tables**: **`agent_templates`** (per-template config), **`agent_prompt_templates`** (canonical slot defaults, versioned + `source = 'json' \| 'admin'`), **`agent_prompts`** (per-agent runtime rows: admin-base cloned from templates + per-user overrides). See [Prompt slots and overrides](#prompt-slots-and-overrides). |
| **`db/attachments/`** | **`file_store.py`** — file byte storage abstraction. Dispatches to local filesystem (`uploads/`) or Supabase Storage based on `db_mode.json`. Exports `store_file()`, `read_file()`, `delete_file()`. See `app/db/SUPABASE_STORAGE.md` for cloud setup. |
| **`db/interface.py`** | **`StorageBackend`** protocol with session, interaction (incl. `update_interaction` + `status`), context, memory, skills, agent, attachment, interrupt, **run-state (`run_state_*` / `cleanup_orphaned_runs`)**, **webhook (register/get/list/delete/log)** methods. |
| **`tools/`** | **`loader`** (dynamic tool loading + built-in injection: register_webhook, list_webhooks, delete_webhook, get_webhook_log, **`list_event_sources` / `list_delivery_channels` / `event_subscribe` / `list_event_subscriptions` / `event_unsubscribe`** (real-time event triggers — gated by the **`automation`** ability; defaults to channel `webchat`+current session_id so the reply lands in the user's open chat), render_visual, plus **`delegate_to_agent` / `list_delegatable_agents`** for non-pipeline agents). **Ability-gated injections:** `web_search` / `get_weather` / `maps_geocode` (web_access), `browser_action` / `http_request` (browser_control), `generate_image` (image_generation), `db_query` + source-edit suite (codebase_admin), `create_tool` (create_tools). Modules: **`core_tools`** (list_tools, search_tools, get_tool_definition, web_search, http_request, db_query, memory, session_search, get_time, get_date, get_weather, calculate), **`registry`** (create_tool, safety scanner, rating utilities), **`tracker`** (legacy execution tracker), **`browser`** (persistent Chromium), **`maps`** (Nominatim geocode + haversine distance), **`image_generation`** (multi-provider — OpenAI-compatible, Stability, Gemini/Imagen — saves to `visuals/users/`), **`read_attachment`** (read uploaded files via `app/db/attachments/`), **`delegation.py`** (builds `delegate_to_agent` + `list_delegatable_agents` handlers; returns delegation sentinel JSON detected by the loop). |
| **`visualizer/`** | **Multi-page workspace tools** — `render_visual`, `list_pages`, `create_page`, `delete_page`. Pages stored per-user at `visuals/users/<user_id>/<slug>.html` with a `pages.json` manifest. `pages.py` handles all page CRUD; `tool.py` implements `render_visual`. **`SKILL.md`** — agent guide for page building. Self-contained — delete to disable. |
| **`models/schemas.py`** | Pydantic models (`ChatRequest`, etc.). |
| **`admin/`** | **`review`** (`/admin/tools` — list/deprecate DB tools), **`db_mode`** (`/admin/db/` — legacy cloud/local switch), **`storage`** (`/admin/storage/*` — provider dropdown, test/bootstrap/activate, secrets vault, data migration export/import), **`settings`** (provider config, model list, metadata toggle), **`integrations`** (`/admin/integrations` — OAuth integration management: configure and revoke Google, Microsoft, Yahoo, Dropbox credentials; `GET /admin/integrations` returns status for all four providers), **`guardrails`** (path/command deny-list for source tools), **`communications`** (multi-channel plugin mgmt: Telegram, Twilio SMS, Twilio WhatsApp, Discord, Slack — `GET /admin/communications/plugins`, `POST /admin/communications/plugins/{name}/credentials`, `POST /admin/communications/plugins/{name}/enable|disable|token`), **`source`** + **`source_tools`** (optional privileged filesystem & shell access — delete to disable), **`users.py`** (`GET /admin/users`, `POST /admin/users/{user_id}/set-admin` — admin user management). See [Administrator Tools](#administrator-tools). |
| **`api/oauth.py`** | OAuth callbacks for all supported providers. **`GET /api/v1/oauth/callback/{provider}`** — providers: `google`, `microsoft`, `yahoo`, `dropbox`, `meta` (Facebook+Instagram), `twitter`, `linkedin`, `tiktok`, `pinterest`, `reddit`, `snapchat`, `twitch`. Each callback exchanges the authorization code for tokens, stores credentials in `auth_elements`, signals the opener popup, and closes the window. Twitter and TikTok use PKCE. Meta stores under `service="meta"` and aliases to `facebook` and `instagram`. |
| **`api/data_sources.py`** | **`GET/POST /api/v1/data-sources`**, **`GET/PUT/DELETE /api/v1/data-sources/{id}`**, **`POST /api/v1/data-sources/{id}/test`**, **`POST /api/v1/data-sources/{id}/introspect`**, **`POST /api/v1/data-sources/{id}/ingest`** (doc_store), **`GET /api/v1/data-sources/types`** — per-user external data source registry. **Attachments:** **`GET /api/v1/agents/{agent_id}/data-sources`**, **`POST /api/v1/agents/{agent_id}/data-sources`** (attach), **`PUT/DELETE /api/v1/agents/{agent_id}/data-sources/{ds_id}`** — per-agent attachment management. Each attached source contributes a synthetic tool at agent load time (`app/tools/loader.py`) and a snippet to the system prompt's **`[DATA SOURCES]`** block (`app/agent/prompts.py`). Connector implementations live in **`app/connectors/`** — v1: `sql_postgres`, `doc_store`, `web_search_domain`. |
| **`automation/`** | Agent automation file parsing + sync. **`parser.py`** calls the configured LLM to convert the free-form `automation` slot into a JSON **object** with two arrays: `tasks` (cron-style, validated via `croniter`) and `event_subscriptions` (push/poll triggers, validated against the live event-source manifest). **`sync.py`** hashes each parsed entry, upserts/deletes rows in **`agent_automations`** (cron) and **`agent_event_subscriptions`** (events) for the `(agent_id, owner_user_id)` pair, recomputes `next_run_at` for cron rows, and for newly-added event subs calls the source plugin's `register_subscription` to wire up the provider-side watch (and `unregister_subscription` for removed rows). Notifies the scheduler. Called from `update_my_prompts` whenever the `automation` slot is saved. The chat agent can also create event subs directly via the built-in **`event_subscribe`** tool (see `app/tools/loader.py`), which reuses the same `_event_sub_hash` + `_register_event_sub` helpers from `sync.py`. |
| **`scheduler/`** | Pluggable scheduler backends behind a `SchedulerBackend` ABC. **`local.py`** = in-process asyncio loop polling `agent_automations` every 30s. **`remote_base.py`** = `BaseRemoteScheduler` shared sync_tasks loop that generates per-row `fire_token`, builds the public webhook URL, and delegates `_push_job` / `_cancel_job` / `test_connection` to provider subclasses. **`providers/`** = registry + concrete providers: **`google_cloud.py`** (Cloud Scheduler REST + RS256 service-account JWT), **`cronjob_org.py`** (cron-job.org REST), **`generic_webhook.py`** (POSTs job specs to an admin-supplied URL). **`executor.py`** creates a fresh session, runs `run_agent_loop_buffered()` with the task's prompt, dispatches via the channel plugin or runs silently, then updates `last_run_at` / `last_status` / `next_run_at`. **`__init__.py`** exposes `get_scheduler()` / `start_scheduler()` / `stop_scheduler()` / `reset_scheduler()` (mirrors `app.db.get_db`). Provider chosen via `scheduler_config.json` (see **`app/admin/scheduler_config.py`** for the admin endpoints + dynamic-form field schemas). |
| **`connectors/`** | Per-type external data source implementations. Each module defines a `Connector` subclass implementing `test_connection`, `introspect`, `generated_tools`, `prompt_snippet`, `safety_validate`. Registry in **`app/connectors/__init__.py`**. SQL connectors enforce a statement-type allowlist and optional `allowed_tables` set, parse queries via `sqlglot` (when installed), and inject a default `LIMIT`. Document-store connector chunks + embeds files via `app/agent/embed.py` into the **`doc_chunks`** table. Web-search-domain connector wraps the existing web-search tool with hard server-side `site:<domain>` filtering. |
| **`api/agents.py`** | **`GET /api/v1/agents/templates`** — list all agent templates. **`POST /api/v1/agents/templates`** — create a template (admin only). **`PUT /api/v1/agents/templates/{id}`** — update a template (admin only). **`DELETE /api/v1/agents/templates/{id}`** — delete a template (admin only). **`POST /api/v1/agents/{agent_id}/save-as-template`** (admin only) — snapshot a custom agent's config + admin-base prompt slots into a new `agent_templates` row plus `agent_prompt_templates` rows (`source='admin'` so JSON re-seed will not overwrite). Body: `{user_id, template_id, name, description?, icon?, discoverable?, access_level?}`. Surfaced in the UI as the **Save as Template** button next to **Save Changes** on a custom agent's Config tab (admin-only). **`GET /api/v1/agents/my-agent`** — get the current user's active agent. **`POST /api/v1/agents/set-default`** — set the user's default agent template. **Agent connections** — `GET /api/v1/agents/{id}/connections` returns all connections + `user_role` (`"admin"` or `"member"`). `PUT` requires agent-admin (global admin or in `admin_users`). OAuth `/authorize` endpoints require the connection to be enabled. `/disconnect` revokes the user's OAuth tokens without changing the admin toggle. **Agent members** — `GET /api/v1/agents/{id}/members?user_id=...` (agent-admin only) returns the agent's `admins` and `members` lists joined with profile + per-agent session/interaction counts, plus the agent's current `user_mode` and per-member `is_authorized` flag. **Access policy** — `POST /api/v1/agents/{id}/user-mode` (agent-admin only; body `{user_id, user_mode}`) sets the policy to `anonymous` (default — anyone with the link can chat), `register` (must have a registered account), or `authorized` (registered + must be in `authorized_users`). `POST /api/v1/agents/{id}/members/{target}/authorize` and `/restrict` add/remove the target from `authorized_users`. The chat endpoints enforce the policy at the start of each request; `/anon-session` is refused for `register` and `authorized` agents. Powers the **Members** tab on the agent card (admin-only UI) — top-level policy radio + per-row Authorize/Restrict buttons. **Row ordering** — `POST /api/v1/agents/reorder` (body `{user_id, order: [agent_id, …]}`) writes `agents.sort_order` by index for the agents the caller administers; `list_agents_for_user` returns customs ordered by `sort_order` (NULLS LAST, then `created_at`), so the chat-header agent dropdown (drag-to-reorder) and the Agents page share one synced order. |
| **`openai_compat.py`** | OpenAI-compatible client wiring for OpenRouter. |

### Frontend (`ui/`)

Single-page app: **`index.html`**, CSS (`app1.css`, `app2.css`, `app3.css`, `files.css`, `loop.css`, `loop-visual.css`, `autoagent.css`, `agents.css`), ES modules under **`js/`** — e.g. **`main.js`**, **`chat.js`** (sends messages via HTTP POST + SSE), **`agentWs.js`** (per-user receive-only WebSocket subscriber), **`chat-activity.js`** (chat pill "thinking" glow + activity-note ticker, driven off the WS event stream), **`stream.js`**, **`loop.js`**, **`tabs.js`**, **`toolLog.js`**, **`terminal.js`** (`createTerminalInstance(container, sessionId)` factory — one xterm + WebSocket per call, exposes `fit / focus / reconnect / dispose / closeBackendSession / onStateChange`), **`files.js`** (Admin Tools page: file editor, terminal tabs, sidebar view switcher, rename, Ctrl+\` hotkey, status-dot rendering, persistence), **`files-git.js`** (Source Control sidebar view: commit/push/pull, branch graph), **`sessions.js`**, **`attachments.js`** (file upload, voice recording, drag & drop, preview chips), **`autoagent.js`** (visualizer tab: iframe renderer, prompt bar, render_visual event listener), **`agents.js`** (Agent Management tab: list/create/edit/delete agent templates, calls `/api/v1/agents/templates`), **`app-config.js`** (App Config tab — single home for LLM provider, Integrations, Database/Storage, Optimizer Stats, and Git Providers, each in its own sidebar section), **`storage.js`** (drives the App Config → Database section against `/admin/storage/*`), **`js/db/`** (data browser). **`test_interface.html`** is also here and is served at **`GET /test`**.

**Boot sequence.** On first paint the chat panel fills the entire stage (`body.boot-chat-only`, set by a pre-paint inline script in `index.html`) and a centered CSS spinner (`#boot-spinner`, driven by `body.is-booting`) shows as a loading indicator. Once `initTabs()` has activated the user's saved tab from `localStorage.lastActiveTab`, `ui/js/tabs.js` swaps `boot-chat-only` for `boot-revealing` and the saved tab animates into its column, then drops `is-booting` so the spinner fades out — this prevents the wrong tab from flashing while its partial mounts. Two settings under **App Config → App Settings**, persisted per browser in `localStorage`:
- `bootAnimation` (values: `chat-slide-right` (default), `page-slide-in`, `crossfade`) — picks the reveal transition.
- `bootMobileMode` (values: `chat-first` (default), `memory`) — mobile only. `chat-first` always runs the chat-first boot then animates to the saved view; `memory` skips the boot animation entirely when the user last had a Page open (saved `chatPanelVisibleMobile=false`) and restores that page directly.

### Directory tree (abbreviated)

```
webAgent/
├── app/                    # Python package (see table above)
│   ├── visualizer/         # AutoAgent p5.js creative coding (render_visual tool + SKILL.md)
│   ├── automation/         # Free-form Automation file → structured cron + event-trigger rows
│   ├── scheduler/          # Scheduler backends: local (in-process) + google (stub) + executor
│   ├── events/             # Event-trigger subsystem: router, poller, renewer, sources/, providers/
│   ├── integrations/       # OAuth-backed integration tools (Gmail, Calendar, Drive, …); one subdir per provider — delete to remove
│   └── db/
│       └── attachments/    # File storage abstraction (store_file / read_file / delete_file)
├── docs/                   # Operator docs: events-setup.md (Pub/Sub, Graph, Dropbox, Shopify)
├── tests/                  # e.g. test_session_history.py (unittest)
├── sw.js                   # PWA service worker (must be at root scope for coverage)
├── ui/                     # Static UI + test_interface.html
│   ├── manifest.json       # PWA web app manifest
│   ├── icons/              # PWA app icons (192×192, 512×512, maskable variants)
│   └── generate_icons.py   # Script to re-generate PWA icons
├── uploads/                # User-uploaded files (images, voice, docs; mounted at /uploads)
├── scripts/
│   ├── start_webAgent.sh            # Unix: cd to repo root, background uvicorn (default :8080, PORT= overrides)
│   ├── backfill_embeddings.py       # One-off: embed existing memory pages
│   ├── seed_tools.py                # Optional tool DB seeding
│   └── export_agent_templates.py    # Reverse-seed: DB → app/context/agents/*.json
├── export_agent_templates.bat  # Double-click: runs export_agent_templates.py (--dry-run supported)
├── migrations/             # Ad-hoc SQL snapshots (includes 007_channel_identities, 008_linking_codes, 009_multi_agent_system, 010_agent_name_backfill, 011_add_login_tracking, 014_data_sources, 018_agent_prompt_templates); see migrations/README.md
├── supabase/migrations/    # e.g. 005_memory_system.sql (Supabase CLI / team workflow)
├── screenshots/            # Mounted at /screenshots
├── android/                # Optional Android wrapper (Java + embedded Python)
├── tasks/                  # Small Node helper (package.json, run-all.ts)
├── temp/                   # Scratch files incl. Markdown drafts (see agent.md); roadmap: temp/FUTURE_PLANS.md
├── .github/workflows/      # CI (e.g. APK build)
├── kill_webagent.bat       # Windows: show + kill all webAgent server processes on port 8080
├── reset_webagent.bat      # Windows: guided clean-slate reset (DB, visual pages, optional auth/secrets/templates); see Maintenance
├── webAgent.bat            # Windows: uvicorn loop + restart support (uses run.py)
├── webagent.exe            # Built launcher + self-bootstrapping installer (gitignored). Produced at the project ROOT by launcher/scripts/build_exe.py.
├── launcher/               # Polished Textual TUI launcher + self-bootstrapping installer; builds to a single portable webagent.exe at the PROJECT ROOT (../webagent.exe). FIRST RUN can install webAgent from nothing — downloads the public repo (git if present, else ZIP), fetches the self-contained uv toolchain (which installs its own private Python), uv-syncs all deps, and downloads the Playwright Chromium browser (~150MB) for browser_action; the end user needs only the .exe (or it can point at an existing folder). Server controls (Launch / Restart / Stop / Browser / Clear DB / Reset Python / Full Reset / Update = re-pull code + re-sync deps) + watchdogs that relaunch the server if it crashes (auto_restart_server) OR goes unresponsive on a health probe (health_check_restart) — shared exponential backoff + 5-strike crash-loop guard; both default ON, toggle in launcher.json + animated procedural ASCII background, AND an embedded keyboard-driven chat client (default view): agent/session scrolling pickers, live token streaming, expandable tool-call blocks, per-turn token/cost stats, image drag-to-attach, talks to the local server's chat/stream + db APIs (can drive the admin coding agent — shell/grep/file-edit). Emoji when the terminal supports it (auto-relaunches into Windows Terminal). See launcher/README.md.
├── launcher_android/       # Termux + proot-distro Ubuntu TUI launcher for running webAgent on an Android phone. Launch / Restart / Kill / Browser + dependency Doctor with one-tap fixes for the common Android-ARM issues (Playwright Chromium, build-essential, missing .env). See launcher_android/README.md.
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
| `AGENT_MAX_IDENTICAL_TOOL_CALLS` | Stall guard: how many times the agent may call the **same tool with identical arguments** before the loop is blocked and the agent is told to change approach (default `3`, min `2`). |
| `AGENT_MAX_STALL_STRIKES` | Stall guard: how many blocked-loop strikes accumulate before the agent stops cleanly with a "I was repeating myself" message (default `4`). |
| `AGENT_MAX_WALL_SECONDS` | Stall guard: hard wall-clock cap on a single agent turn-loop; on exceed it ends gracefully instead of timing out (default `300`; set `0` to disable). |
| `WEBAGENT_BROWSER_CDP_PORT` | Remote-debugging port the `browser_action` tool looks for a visible **App Window** on to attach to (default `9222`). When nothing is listening there (no window, or a headless server), the browser stays headless. Set automatically by the launcher's App Window button; rarely needs changing. |
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
| `GOOGLE_CLIENT_ID` | Google OAuth client ID (Integrations page — Gmail, Drive, Docs, Calendar) |
| `GOOGLE_CLIENT_SECRET` | Google OAuth client secret |
| `MICROSOFT_CLIENT_ID` | Microsoft Azure App registration client ID (Integrations page — Outlook, OneDrive, SharePoint) |
| `MICROSOFT_CLIENT_SECRET` | Microsoft client secret value |
| `YAHOO_CLIENT_ID` | Yahoo Developer app consumer key (Integrations page — Yahoo Mail) |
| `YAHOO_CLIENT_SECRET` | Yahoo consumer secret |
| `DROPBOX_APP_KEY` | Dropbox App Console app key (Integrations page — file storage) |
| `DROPBOX_APP_SECRET` | Dropbox app secret |
| `META_APP_ID` | Meta App ID (Integrations page — Facebook + Instagram via one Meta app) |
| `META_APP_SECRET` | Meta App Secret |
| `TWITTER_CLIENT_ID` | Twitter/X OAuth 2.0 Client ID (Integrations page — X/Twitter; uses PKCE) |
| `TWITTER_CLIENT_SECRET` | Twitter/X Client Secret |
| `LINKEDIN_CLIENT_ID` | LinkedIn App Client ID (Integrations page — LinkedIn) |
| `LINKEDIN_CLIENT_SECRET` | LinkedIn Client Secret |
| `TIKTOK_CLIENT_KEY` | TikTok for Developers Client Key (Integrations page — TikTok; uses PKCE) |
| `TIKTOK_CLIENT_SECRET` | TikTok Client Secret |
| `PINTEREST_APP_ID` | Pinterest Developer App ID (Integrations page — Pinterest) |
| `PINTEREST_APP_SECRET` | Pinterest App Secret |
| `REDDIT_CLIENT_ID` | Reddit App Client ID (Integrations page — Reddit) |
| `REDDIT_CLIENT_SECRET` | Reddit Client Secret |
| `SNAPCHAT_CLIENT_ID` | Snap Kit App Client ID (Integrations page — Snapchat) |
| `SNAPCHAT_CLIENT_SECRET` | Snapchat Client Secret |
| `TWITCH_CLIENT_ID` | Twitch Developer App Client ID (Integrations page — Twitch) |
| `TWITCH_CLIENT_SECRET` | Twitch Client Secret |
| `BOOTSTRAP_ADMIN_ID` | User ID to auto-promote to admin on server start (first admin bootstrapping). Once an admin exists in `user_profiles`, this var has no further effect. |
| `WEBHOOK_BASE_URL` | Public base URL of this server (e.g. `https://myservice.run.app`). Set this on Cloud Run or any hosted environment — the app uses it at startup to register webhooks instead of falling back to polling. Auto-detected from the incoming request when first connecting a bot via the UI if not set. |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token (env-var fallback; UI-set value in `registry.json` takes priority). |
| `TWILIO_ACCOUNT_SID` | Twilio Account SID (env-var fallback for SMS and WhatsApp plugins). |
| `TWILIO_AUTH_TOKEN` | Twilio Auth Token (env-var fallback for SMS and WhatsApp plugins). |
| `TWILIO_FROM_NUMBER` | Twilio phone number for outbound SMS in E.164 format (e.g. `+15551234567`). |
| `TWILIO_WHATSAPP_FROM` | Twilio WhatsApp-enabled number (e.g. `whatsapp:+14155238886`; sandbox or approved). |
| `DISCORD_BOT_TOKEN` | Discord bot token (env-var fallback; from discord.com/developers/applications → Bot). |
| `DISCORD_APPLICATION_ID` | Discord Application ID (for interaction endpoint registration). |
| `DISCORD_PUBLIC_KEY` | Discord application public key for Ed25519 webhook signature verification. |
| `SLACK_BOT_TOKEN` | Slack bot token (env-var fallback; `xoxb-…` from api.slack.com/apps → OAuth & Permissions). |
| `SLACK_SIGNING_SECRET` | Slack signing secret for request verification (api.slack.com/apps → Basic Information). |
| `EVENTS_GMAIL_PUBSUB_TOPIC` | Full Cloud Pub/Sub topic name that Gmail `users.watch` should publish to (e.g. `projects/webagent-495517/topics/webagent-gmail-watch`). Enables the Gmail event source. See **[`docs/events-setup.md`](docs/events-setup.md)**. |
| `EVENTS_PUBSUB_AUDIENCE` | OIDC audience set on the Pub/Sub push subscription; webAgent validates inbound JWTs against this. Typically the public webhook URL (e.g. `https://your.host/api/v1/events/gmail`). Required when any Google push event source is enabled. |
| `EVENTS_PUBSUB_SA_EMAIL` | Optional. If set, inbound Pub/Sub JWTs must additionally carry this service-account email in the `email` claim. |
| `EVENTS_GRAPH_NOTIFICATION_BASE` | Public base URL Microsoft Graph posts subscription notifications to (e.g. `https://your.host`). Combined with `/api/v1/events/outlook_mail`, `/outlook_calendar`, `/onedrive` per source. Enables all MS Graph event sources. |
| `EVENTS_GCAL_NOTIFICATION_BASE` | Public base URL Google Calendar channels post to. Combined with `/api/v1/events/google_calendar`. Enables Google Calendar event source. |
| `EVENTS_GDRIVE_NOTIFICATION_BASE` | Public base URL Google Drive channels post to. Combined with `/api/v1/events/google_drive`. Enables Google Drive event source. |
| `EVENTS_DROPBOX_WEBHOOK_BASE` | Public base URL Dropbox posts webhook notifications to. Combined with `/api/v1/events/dropbox`. Enables Dropbox event source. |
| `EVENTS_SHOPIFY_WEBHOOK_BASE` | Public base URL Shopify posts webhook notifications to. Combined with `/api/v1/events/shopify`. Enables Shopify event source. |
| `WEBAGENT_CONFIG_SOURCE` | Set to `env` to lock all storage configuration to environment variables (no JSON file writes). Used on Cloud Run / containerized deploys where the local filesystem is ephemeral. UI shows the storage panel as read-only and the config endpoints return `403` on writes. |
| `WEBAGENT_DB_MODE` | When config is env-locked, selects `cloud` or `local` for the legacy backend toggle (replaces `db_mode.json`). |
| `WEBAGENT_DB_PROVIDER` | When env-locked, the active DB provider (`sqlite`, `supabase`, `postgres`, `gcp_cloud_sql`, `neon`, `mysql`). Companions: `WEBAGENT_DB_HOST`, `WEBAGENT_DB_PORT`, `WEBAGENT_DB_NAME`, `WEBAGENT_DB_USER`, `WEBAGENT_DB_PASSWORD_KEY` (key into the secrets vault). |
| `WEBAGENT_SECRETS_PROVIDER` | Selects the secrets vault: `inline_db`, `env`, `os_keyring`, `gcp_secret_manager`, `aws_secrets_manager`. Defaults to `inline_db`. |
| `WEBAGENT_SECRET_*` | When the secrets provider is `env`, secret keys map to env vars with this prefix (e.g. key `db_password_postgres` → `WEBAGENT_SECRET_DB_PASSWORD_POSTGRES`). |
| `GCP_PROJECT` / `GOOGLE_CLOUD_PROJECT` | GCP project ID — required by the `gcp_secret_manager` secrets provider. |
| `AWS_REGION` | AWS region — used by the `aws_secrets_manager` secrets provider. |

In **local** mode, Supabase vars are not required for storage; you still need **`OPENROUTER_API_KEY`** (and usually **`OPENROUTER_MODEL`**) for LLM calls.

Provider, API key, and model can **also** be configured at runtime via the ⚙️ **Settings** modal in the UI (gear icon next to Cloud/Local toggle). Changes are saved to **`provider.json`** in the project root and applied on next server start. The API key is masked in the UI after saving.

**Multi-provider parallel mode:** When `parallel_mode: true` and 2+ entries in `multi_providers`, the agent fans out each LLM call to all configured providers in parallel and uses the first complete response. Configured via `POST /admin/settings/multi-providers` or directly in `provider.json`. Fallback: single-provider path is unchanged when parallel mode is off or < 2 providers.

## Installation

**Supported Python:** 3.11 or 3.12 (pinned via `pyproject.toml` `requires-python = ">=3.11,<3.13"`). Newer versions may drop stdlib modules or lack wheels for pinned deps.

1. Clone the repository and enter the directory.

2. **Windows quick path:** double-click **`webAgent.bat`**. It will:
   - Install **uv** (Astral's Python + venv manager) if missing — via the official PowerShell installer.
   - Run `uv sync`, which downloads a matching Python (3.11 or 3.12) into uv's cache if you don't have one, creates `.venv` in the project root, and installs all deps from `pyproject.toml` / `uv.lock`.
   - If the existing `.venv` was built with an unsupported Python, the script removes it so uv can rebuild cleanly.
   - Start the server with auto-restart on exit.

   On subsequent boots, `uv sync` is idempotent and near-instant when deps haven't changed.

   **Manual / non-Windows (uv path, recommended):**

```bash
# Install uv if not already present
curl -LsSf https://astral.sh/uv/install.sh | sh

# Sync deps + Python (idempotent)
uv sync

# Run the server
uv run python run.py
```

   **Manual / non-Windows (legacy pip path):** still works — `requirements.txt` is maintained alongside `pyproject.toml`.

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

  > **New (v0.8+):** **Row ordering** — `agents.sort_order` and `sessions.sort_order` (both nullable `INTEGER`) persist the user's manual **drag-to-reorder** order for the chat-header agent and session dropdowns (each row has a grip handle). The agent order is shared by the dropdown **and** the Agents page (🤖); the session order applies in the session dropdown. Order is written by index via `POST /api/v1/agents/reorder` and `POST /api/v1/db/sessions/reorder` (account-level, so it follows the user across devices), and read back via the `(sort_order IS NULL), sort_order ASC, …` clauses in `list_agents_for_user` and the sessions list query (pinned rows still float to the top). The shared frontend helper (pin state, display sort, and the pointer-based drag that works on mouse + touch) lives in `ui/js/ordering.js`, imported by both `ui/js/sessions.js` (the dropdowns) and `ui/js/agents.js` (the Agents page). The local SQLite backend auto-applies the columns via ALTER TABLE migrations; on **Supabase**, run `migrations/024_row_ordering.sql` via the SQL editor.

4. Run the server:

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
```

**Windows users:** `webAgent.bat` (described above) is the recommended path — it handles venv creation, dependency install/update, and auto-restart. Internally it invokes `run.py` which pre-opens port 8080 with `SO_REUSEADDR` to work around orphaned TCP entries (process dead but port still bound).

**TUI launcher (optional, prettier than .bat):** the `launcher/` directory builds a portable `webagent.exe` (Textual-based TUI, Lucide bot icon) with buttons for Launch / Restart / Stop / Open Browser / Clear DB / Reset Python / Full Reset, plus an animated procedural ASCII background with cycleable color presets (phosphor, amber, cyan, vaporwave, neon tide, rainbow, fire, ice, …). Config (project path, theme) persists at `%APPDATA%\webAgent\launcher.json` so the .exe can sit anywhere. To build: `cd launcher && uv sync --extra build && uv run python scripts/build_exe.py` → `launcher/webagent.exe` (single file, no scratch folders left behind). To run from source: `cd launcher && uv sync && uv run python -m webagent_launcher`. Keyboard shortcuts and feature list in `launcher/README.md`.

**Unix:** `bash scripts/start_webAgent.sh` (background + logs).

**Android (Termux + proot-distro Ubuntu):** the `launcher_android/` directory is the Android counterpart to `launcher/` — a Textual TUI with Launch / Restart / Kill / Browser buttons plus a dependency **Doctor** that surfaces venv / `.env` / apt build-deps / per-package pip status and offers one-tap fixes. The known Android-ARM blocker (Playwright Chromium download fails on ARM) is downgraded from fatal to skip-OK by writing `PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1` into `.env`; other optional deps that fail to install are listed as non-critical rather than blocking startup. Run it from Termux with `bash launcher_android/start.sh` — the shim handles installing `proot-distro`, the Ubuntu distro, and `textual` inside the proot on first run. See `launcher_android/README.md` for full setup steps and the Browser-bridge mechanism that forwards URLs to Android's `termux-open-url`.

**Useful URLs**

| URL | Purpose |
|-----|---------|
| `http://localhost:8080/` | Main UI |
| `http://localhost:8080/docs` | Swagger |
| `http://localhost:8080/index.html` | Main UI (legacy alias) |
| `http://localhost:8080/test` | Minimal HTML chat (`ui/test_interface.html`) |
| `http://localhost:8080/privacy` | Privacy Policy page (`ui/privacy.html`, public, no auth) |
| `http://localhost:8080/tos` | Terms of Service page (`ui/tos.html`, public, no auth) |
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

## Prompt slots and overrides

Prompts that shape an agent flow through **three tables**, each with a single, clear job:

| Table | What it holds | Who writes it |
|-------|---------------|---------------|
| **`agent_templates`** | Per-template **config** (model, temperature, max_tokens, loop_logic, trigger). One row per template id (`default`, `admin-agent`, etc.). | JSON seeder |
| **`agent_prompt_templates`** | Canonical **slot content** for each template (system, agent, user, skills, tasks, misc, automation, bootstrap_tools). One row per `(template_id, slot_name)`. Versioned via `version` int + `source` enum (`'json'` \| `'admin'`). | JSON seeder + admin-edit-back UI (planned) |
| **`agent_prompts`** | Per-agent runtime slot rows: the **admin-base rows cloned from the template** when a new agent is created, plus any **per-user override rows**. Each clone stamps `template_version` so the agent can be told "your template moved to v4". | Agent creation + per-user override endpoints |

A new agent is born by:

1. Looking up the template row in `agent_templates` (config).
2. Cloning every row from `agent_prompt_templates WHERE template_id = <template_id>` into `agent_prompts` under the new agent's id (with `user_id IS NULL` and `template_version` stamped from the source row).
3. Any per-user override gets written as an extra row in `agent_prompts` with a non-null `user_id`.

**Admin base** rows in `agent_prompts` carry the slot's policy: `order_index` (where the slot sits in the assembled system message), `lock` (admin-only when true), and `merge_mode` (`replace` or `append`).

**User override** rows have a non-null `user_id`. Each user (including anonymous browser-scoped visitors) can have at most one override per slot. When the agent loop assembles the system prompt for a caller, locked slots ignore overrides; unlocked slots either `replace` the admin base with the user override or `append` it below, per the slot's `merge_mode`.

### Seeding from JSON (manifest-gated, version-aware)

`app/context/agents/*.json` is the recovery source of truth for fresh databases. Each file declares a top-level integer `"version"` that gates the per-slot upsert:

- New slot → insert with `source='json'`, `version=<JSON version>`.
- Existing slot with `source='json'` and JSON version > DB version → update + bump.
- Existing slot with `source='admin'` → **skip** (admin edits are sacred).
- Manifest hash of all JSON files is stored in `app_meta.last_agent_manifest_hash`; the seeder short-circuits when the hash matches, so it's cheap to call at every boot.

The seeder runs at **app startup** (manifest-gated, almost always a no-op). To re-seed on demand or force-overwrite admin edits, use **App Config → Database → Agent Prompt Templates** (`/admin/db/templates`, `POST /admin/db/templates/seed`, `POST /admin/db/templates/seed-force`).

### Endpoints

- **`GET /api/v1/agents/{agent_id}/slots?user_id=...`** — returns the admin-base slot list plus the caller's overrides per slot, and a `user_role` of `"admin"` or `"member"`.
- **`PUT /api/v1/agents/{agent_id}`** (admin only) — agent-row fields plus an optional `slots` array that fully reconciles the admin-base slot set. May include `reset_overrides_for` (list of slot_names) to wipe all per-user overrides for those slots at save time.
- **`PUT /api/v1/agents/{agent_id}/my-prompts`** — any caller writes their own override rows for unlocked slots; locked or unknown slot_names are rejected per item.
- **`DELETE /api/v1/agents/{agent_id}/my-prompts/{slot_name}?user_id=...`** — clears one user's override for a slot.

### In-chat self-modification

The agent's in-chat prompt-refinement flow uses the same admin/user split: admin callers may write to admin-base rows, members and anonymous visitors can only write to their own override rows. Locked slots are refused with a user-visible message.

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
| `WEBAGENT_CONFIG_SOURCE=env` set | Locks storage config to env vars so `db_mode.json` / `db_connection.json` / `secrets_mode.json` writes are suppressed (they would only land on the ephemeral container disk). Provider, host, db, user, and the secrets vault are sourced from env vars instead. |
| Secrets in a real vault | Use `WEBAGENT_SECRETS_PROVIDER=gcp_secret_manager` on GCP (set `GCP_PROJECT`) — never leave passwords in `inline_db` for production. |
| `asyncpg` available | Required when using any Postgres-family provider (Supabase, raw Postgres, GCP Cloud SQL, Neon) for connection testing and schema bootstrap. Pinned in `requirements.txt`. |
| `aiomysql` + `sqlglot` available | Required for per-agent external data source connectors. `aiomysql` powers the `sql_mysql` connector; `sqlglot` is used by SQL connectors to parse statement type and referenced tables before sending to the customer DB. Both are pinned in `requirements.txt`. |
| Automation scheduler set to a remote backend on Cloud Run | The default `LocalScheduler` in **`app/scheduler/local.py`** is in-process — it does not survive container recycles and will not fire across multiple instances. For Cloud Run deployments switch the provider to `google` under **App Config → Automation** (the `GoogleScheduler` is currently stubbed; finish the integration before relying on scheduled tasks in production). |
| Event-trigger poll + renewer loops are in-process | The loops in **`app/events/poller.py`** and **`app/events/renewer.py`** share the same Cloud Run limitation as the local scheduler — they only run while the container is warm. Push-based event sources (Gmail / Outlook / Calendar / Drive / Dropbox / Shopify / comms bridges) work fine on Cloud Run because providers POST directly to **`/api/v1/events/{source}`** and wake the container; poll-only sources (Twitter, Reddit) need a long-running host (the GCE VM) or an external scheduler that hits the poll endpoints. Subscription renewal also needs a warm instance — if you scale to zero, set `min_instances=1` or invoke a refresh from Cloud Scheduler. |
| `doc_store` connector uses Supabase Storage in cloud mode | The local-filesystem variant relies on a writable container path that survives between requests. In Cloud Run that path is ephemeral, so the data-source admin UI hides the `local` backend option when `WEBAGENT_CONFIG_SOURCE=env`. Use `backend: supabase_storage` instead, and apply **`migrations/014_data_sources.sql`** to add the `data_sources`, `agent_data_sources`, and `doc_chunks` tables (plus pgvector + the `doc_chunks_hybrid_search` RPC) before attaching any sources. |

**Deploy (manual):**
```bash
gcloud run deploy webagent \
  --source . \
  --region europe-west1 \
  --allow-unauthenticated
```

### Google Compute Engine VM (persistent disk)

Unlike Cloud Run, a GCE VM has a real, persistent filesystem — so you can keep code under version control on the box itself and pull updates in place from the GitHub tab inside the app.

**One-time VM setup:**

1. **Clone the repo** to a stable path, e.g. `/opt/webagent`, and create the `.venv` + `.env` next to it (same layout as local dev).
2. **Create a dedicated user** for the service (e.g. `webagent`) and `chown -R webagent:webagent /opt/webagent`. The user needs read/write on the repo so the in-app **Pull & Restart** button can run `git pull` and rewrite files.
3. **Install the systemd unit** shipped at **`scripts/webagent.service`**:
   ```bash
   sudo cp /opt/webagent/scripts/webagent.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now webagent
   ```
   Edit `User`, `WorkingDirectory`, `EnvironmentFile`, and the `ExecStart` venv path inside the unit before enabling it if your layout differs.

**Why systemd matters for the GitHub tab’s Pull & Restart button:**

- **`POST /api/v1/restart`** (`app/main.py`) terminates the uvicorn process with `os._exit(0)`.
- The unit file has `Restart=always` / `RestartSec=1`, so systemd immediately relaunches uvicorn from the just-updated source tree.
- Without a supervisor (or with a plain `nohup ./scripts/start_webAgent.sh &`) the server stays dead after restart and the app becomes unreachable.

**How updates flow (GCE):**

1. You push code from a dev machine to GitHub.
2. On the VM, in the running app, open the **GitHub** tab as admin — or switch the **File Editor** sidebar to its Source Control view (the GitHub icon next to Explorer).
3. Click **Pull & Restart** — the UI calls `/api/v1/github/pull`, then `/api/v1/restart`, then polls `/health` until the new process is up.
4. Static assets (`ui/`, `index.html`) refresh on the next page reload; Python backend code is now the freshly-pulled version because systemd relaunched uvicorn.

**File Editor sidebar — Source Control view.** The Files tab’s left sidebar has two view modes, toggled by icon buttons at the top:
- **Explorer** (default) — the directory tree.
- **Source Control** — a compact GitHub manager (status, changes, commit, push/pull, token) plus a VS Code-style commit graph that shows *all* branches/refs (not just `main`). Each row has a coloured lane dot; merge bubbles and side branches render as bent connector lines. The graph is fed by `GET /api/v1/github/log-graph`, which extends `/status` with `git log --all` plus pre-computed lane positions so the frontend just draws SVG segments.
  - **⭐ Source Controller hand-off.** Next to the commit-message box sits an amber **star** button (enabled when there are uncommitted changes *or* unpushed commits). Clicking it finds-or-creates a per-user **Source Controller** agent (cloned from the `source-controller` template, with the **Codebase Admin** ability auto-enabled so `git_tool` loads), switches the chat to it in a fresh session, reveals the chat panel, and submits a ready-made message that already carries the remote URL + author email (and the typed commit note, if any). The agent then reviews the diff, writes a commit note, runs its safety checks, and commits + pushes — reporting back in chat. Wiring lives in `ui/js/files-git.js` (`handoffToSourceController`), mirroring the Dashboard→chat hand-off in `autoagent.js`. The agent's `git_tool` (in `app/admin/source_tools.py`) routes through the same `_run_git` helper the Push button uses, so it authenticates identically.

**Troubleshooting:**
- `journalctl -u webagent -f` — live logs from the unit.
- If Pull & Restart hangs at "Waiting for relaunch…": systemd isn’t restarting the process. Check `systemctl status webagent` and verify `Restart=always` is set.
- If `git pull` fails with auth errors: set a GitHub Personal Access Token via the GitHub tab → Settings (stored in `provider.json` under the service user’s home / repo dir).

**D

## Maintenance

### Resetting local state (`reset_webagent.bat`)

Use **`reset_webagent.bat`** (repo root, Windows) to wipe webAgent back to a clean state in one go. Intended for local dev — handy when prepping a clean handoff, reproducing a first-run bug, or escaping a corrupted local DB. Double-click or run from a terminal.

**What runs automatically (no prompt):**

1. **Stops the running app** — kills any process on port 8080 plus `python.exe` / `python3.exe`, the same pattern as `kill_webagent.bat`. This is required so the SQLite WAL isn't held open during deletion.
2. **Wipes the userbase** — the database files (`app\db\local.db` plus `-journal`, `-wal`, `-shm`, `.preprompt-bak`, and any stray root-level `local.db`) and the per-user generated pages directory (`visuals\users\`). The DB schema, the default `admin / admin` user, and the agent templates are all re-seeded automatically on the next `webAgent.bat` start (see `_init_db()` and `_seed_agent_templates_from_json_files()` in `app/db/local.py`, and `_ensure_default_admin()` in `app/auth/users.py`).

**Prompts (in order):**

| # | Prompt | Default | Targets when "yes" |
|---|--------|---------|---------------------|
| 1 | Back up files to `temp\reset-backup-<timestamp>\` before deleting? | **Yes** | Every targeted file/folder is `move`d into a timestamped folder under `temp\` mirroring its original path (reversible). On "No", everything is hard-deleted via `del /F /Q` or `rmdir /S /Q`. |
| 2 | Clear app secrets (LLM API keys, OAuth tokens, integration creds, scheduler config)? | No | `provider.json`, `app-settings.json`, `scheduler_config.json`, `app\db_mode.json`, `app\pages_mode.json`, `app\db_connection.json`, `app\secrets_mode.json`. App falls back to local defaults when these are missing. |
| 3 | Clear local user accounts (passwords, remember-me tokens)? | No | `app\auth\users.json`, `app\auth\users.json.bak`. `admin / admin` is re-created on next start. |
| 4 | Delete `.env`? | **No** | `.env` at repo root. Default No — most installs (Supabase, OAuth client IDs) need this file to start. |
| 5 | Delete agent template JSON files in `app\context\agents\`? | No | All `*.json` under `app\context\agents\`. **Warning:** there is no fallback — the next start will boot with zero agents until you restore templates. |
| — | Final **Proceed? [y/N]** summary lists every path that will be processed, tagged `[BACKUP]` or `[DELETE]`. Defaults to No (abort). | No | — |

After execution the script prints `OK` / `FAIL` / `SKIPPED` counts and (if backups were enabled) the backup path. Run `webAgent.bat` afterwards to relaunch from a clean install.

**Related scripts:** `webAgent.bat` (start + auto-restart), `kill_webagent.bat` (stop-only), `export_agent_templates.bat` (reverse-seed: DB → JSON templates).
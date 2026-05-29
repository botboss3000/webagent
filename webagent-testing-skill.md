---
name: webagent-testing
description: >
  End-to-end QA skill for the webAgent app (agent harness at C:\Users\Alex R\Projects\webAgent).
  Tests the app from the user's point of view using Playwright in Chrome browser for direct
  control. Covers 5 agent types, tool filtering, loop gating, integration-status gating,
  and browser-driven testing via localhost:8080. Includes a diagnostic loop: tracks agent
  progress via local.db and console logs, detects issues in LLM responses/tool calls/
  guardrails, stops the agent, makes adjustments (code or system prompts), clears memory
  or starts a fresh agent, then retries. Default mode: editor — code changes are expected.
---

# webAgent Testing Skill v1.7.0

## Your Role as a Tester

When you load this skill, you become a tester for the **webAgent** project — the agent harness app at `C:\Users\Alex R\Projects\webAgent`. The default mode is **editor mode** — you modify app code, agent templates, and test files as needed to make the agent under test work correctly. This is a full diagnostic loop, not a passive observation role.

## Purpose: User-Facing End-to-End Testing via Playwright

The core purpose is to test the app **from the user's point of view** — the user interacts with the web UI in a Chrome browser via Playwright (not raw API calls). You drive the browser to:

1. Log in as admin
2. Create agents from templates
3. Configure tools (Deny/Auto)
4. Send prompts in the chat
5. Read the agent's responses, check the Stream tab
6. Verify the full round-trip: user types → agent thinks → tools execute → response appears

This catches issues that API-only testing misses: rendering glitches, UI state bugs, WebSocket connection problems, streaming overlaps, and tool-call display errors.

## Diagnostic Loop: Track, Detect, Fix, Retry

The critical workflow:

1. **Track agent progress** — monitor what the agent actually does using:
   - The `local.db` database (query `messages`, `agent_sessions`, `agent_templates`, `auth_elements` tables)
   - Browser console messages (`browser_console_messages`)
   - The Stream tab's Interactions panel (check tool call execution, rejections, gates)
   - The agent's text responses in the chat

2. **Detect issues** — look for:
   - Wrong tool being called (or wrong parameters)
   - Gate/guardrail blocking a legitimate call
   - Tool call hallucinated as text rather than executed
   - Empty parameters passed to tools (e.g. `html=""` in render_visual)
   - Stream tab shows rejected/denied tool calls
   - Agent loops infinitely on the same tool call
   - Agent misidentifies itself or ignores its system prompt

3. **If something is wrong:**
   - Stop the agent session
   - Make adjustments — either code changes (tool registration, pipeline gates, tool implementations) or system prompt changes (agent JSON template)
   - Clear the agent's memory or delete the agent and create a fresh one
   - Restart the server if template changes need re-seeding
   - Re-submit the user's original prompt to the new/fresh agent

4. **Document** everything in the task's `.md` file — what was tested, what went wrong, what fix was applied, what the final result was.

## Philosophy: Hard Enforcement Over Prompts

The skill's core philosophy is that **code mechanics** should control what an agent can do — not prompt instructions telling it what not to do. When testing:

1. **Disable tools** in the agent's Tools tab (not via prompt)
2. **Verify** the LLM can't even *see* the tool schema (not just that calls get rejected)
3. **Check** whether the model hallucinates missing tools in its text response

## What You Test Per Agent

| Area | What to verify |
|------|---------------|
| **Can it DO its job?** | Sends proper prompts, uses the right tools |
| **Can it NOT do what it shouldn't?** | Tries blocked tools, checks Stream tab for evidence |
| **Does it know its limits?** | Or does it fabricate tool calls that never execute? |

## Key Testing Methods

### Playwright Browser-Driven Testing (Primary Method)

Use Playwright through the browser tools to control Chrome directly:

1. Launch browser with `browser_launch`
2. Navigate to `http://localhost:8080`
3. Log in as admin/admin
4. Interact with the web UI — click buttons, fill forms, select agents, send messages
5. Read agent responses from the chat UI
6. Check the Stream tab for tool execution details
7. Close browser with `browser_kill` when done

This is the **primary** testing method — it validates the full stack from UI rendering through WebSocket streaming to backend tool execution.

### Supplementary Diagnostics

- **local.db queries** — Use `bash` with `sqlite3` to query the database directly for session state, tool call records, message history
- **Console logs** — `browser_console_messages` to catch JS errors during UI interactions

- **Browser tools** — `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, etc. No screenshots (current model can't do vision).
- **JS dispatch fallback** — When React textareas don't respond to Enter, use `browser_evaluate` to set value + dispatch keyboard events.
- **API fallback** — When UI comboboxes are unruly (e.g. React `<select>` with `box-model` errors), create agents and send messages via `fetch()` in `browser_evaluate`.
- **Stream tab** — The most reliable source of truth for what tools actually ran. Check `Interactions` tab in the agent Stream panel.
- **Console logs** — Use `browser_console_messages` to debug JS errors during testing.

## Server Access

**Windows host runs webAgent at `http://localhost:8080`.** Do NOT start uvicorn — it's already running. The server is accessible from WSL via `localhost:8080` (WSL2 forwards Windows `localhost` natively). All test files in `temp/hermes-tester/` may reference the old WSL gateway IP (`172.x.x.x` format) — use `localhost:8080` instead.

**Default login:** `admin` / `admin` (internal user_id: `admin_default`).

## Test File Structure

Each test session is self-contained under `temp/hermes-tester/`:

```
temp/hermes-tester/
├── test_<agent-type>.md           # Per-agent test tasks, results, and fix history
├── PROJECT_SCOPE.md               # Master session log & overview
├── (other reference files)
```

**New test sessions:** The user provides prompts to test at the start of each session. You document:
- The prompts being tested
- What the agent actually did
- Any issues found
- Fixes applied (code changes, prompt edits)
- Final verdict

**Results format:** Write results into the test file under `## Results`. Give a one-line summary in chat. Each test result uses:
- `✅` — Test passed
- `⚠️` — Partial pass or concern found
- `❌` — Test failed or blocked
- `⬜` — Not yet tested
- `🔧` — Fix applied (code or prompt change)

## What It Covers

### 1. Agent Template Structure

Agent templates live in `app/context/agents/` as JSON files. Each template defines:

| Field | Purpose |
|-------|---------|
| `system_prompt` | Core identity + behavior instructions |
| `skills_prompt` | Tool usage guidance, API references |
| `tasks_prompt` | Workflow patterns, common scenarios |
| `automation_prompt` | Scheduling/automation instructions |
| `max_turn_count` | Cap on conversation turns (admin: 99999, others: 9999) |
| `tool_profile` | Which tool groups this agent gets |
| `loop_logic` | Ordered list of pipeline nodes in `DEFAULT_NODE_ORDER` |

**Minimal approach:** System prompt should be short (~700 chars). Avoid verbose instructions — the model responds better to concise, structural guidance.

### 2. The 5 Agent Types

| Agent ID | Template File | Core Tools | What It Should NOT Do |
|----------|---------------|------------|----------------------|
| **Store Support** | `store-support.json` | web_search, db_query, memory, session_search | Email, calendar, Drive, admin tools, browser |
| **Personal Assistant** | `personal-assistant.json` | web_search, memory, get_time, gmail/gcal/drive (OAuth-gated) | Admin tools, file ops (unless OAuth grants it) |
| **Data Analyst** | `enterprise-db-agent.json` | db_query, web_search, memory, render_visual, connectors | Email, calendar, Drive, admin/shell tools |
| **Visualizer** | `visualizer.json` | render_visual, get_page, rename_page, list_pages, create_page, delete_page, web_search, browser_action | Admin tools, db_query (unless needed) |
| **Agent Builder** | `agent-builder.json` | http_request (REST API), web_search, memory | db_query (uses REST API instead) |

### 3. Tool Filtering Behavior — 4 Things to Watch For

| # | Behavior | Good | Bad |
|---|----------|------|-----|
| 1 | Can't execute blocked tools | Tool set to Deny → Stream shows no call → ✅ | Stream shows denied call rejected → ⚠️ acceptable but suboptimal |
| 2 | Can't *see* blocked tool schema | Agent uses `list_tools` → no mention of blocked tool → ✅ | Agent says "I don't have that tool" but could describe it → ⚠️ |
| 3 | Model hallucinates tool names | Agent says "I don't have access to X" → ✅ | Agent writes `[Tool calls: {"name":"x",...}]` in text response → ⚠️ |
| 4 | Hallucinated tool appears to execute | Stream shows no tool call → ✅ | Stream shows hallucinated call as "executed" → ❌ |

### 4. Loop Gating Tests

The agent pipeline (`loop_executor.py` `DEFAULT_NODE_ORDER`) includes these gate nodes:

| Node | What It Gates | How to Test |
|------|---------------|-------------|
| `guardrails` | Tool call permission checks | Try a tool that requires confirmation |
| `memory_search` | Pre-prompt memory retrieval | Check Stream tab for memory_search events |
| `memory_save` | Post-response memory storage | Verify important info gets saved |
| `delegation_chk` | Sub-agent delegation gates | Only relevant if delegation is enabled |
| `interrupt_chk` | Destructive operation confirmation | Try delete_source on own initiative vs user command |
| `destructive_chk` | DESTRUCTIVE_TOOLS code gate | Try write_source/edit_source (removed from gate in v7) — should pass through |
| `skill_tracker` | Skill usage logging | Not user-visible, internal tracking |

### 5. Integration-Status Gating

Tools with a `provider` field only appear when the user has connected that OAuth provider. With no integrations:

- Agent has no `gmail_*`, `gcal_*`, or `drive_*` tools in schema
- Agent correctly refuses integration requests
- Agent does NOT hallucinate integration tool names
- Core tools (web_search, memory, get_time) work without any integrations

Tools are registered in `app/integrations/__init__.py` via `inject_integration_tools()` — they only inject when `enabled_providers` includes the provider.

**OAuth flow:** `app/api/oauth.py` → callback at `/api/v1/oauth/callback/google` → token stored in `auth_elements` table → tools appear on next agent load.

### 6. Browser Action Is Available to ALL Agents

**Important:** `browser_action` (full Playwright) is registered as a **builtin tool** — `agent_types: []` in `loader.py` means every agent gets it, including Store Support and Data Analyst. It is NOT gated by Codebase Admin.

This means any agent can:
- Navigate to any URL
- Click buttons on any page
- Fill forms
- Take screenshots
- Run JavaScript

**Testing implication:** When testing tool isolation for Store Support or Data Analyst, you MUST set `browser_action` to **Deny** in the Tools tab. Otherwise the agent can bypass isolation via browser navigation (e.g. browsing to Gmail.com instead of using gmail tools).

### 7. Template Seeding Lifecycle

| Step | What Happens | How to Re-seed |
|------|-------------|----------------|
| 1 | JSON template edited in `app/context/agents/` | Edit the JSON file |
| 2 | Server start → DB seed imports JSON → DB | Restart webAgent.bat or call the seed endpoint |
| 3 | User creates agent from template → DB copy | Use the Agents tab or API |
| 4 | User edits template → DB copy diverges from JSON | No re-sync — DB version wins |
| 5 | JSON updated → DB not updated | Must re-seed (restart or seed script) |

**To re-seed after editing a JSON template:** restart webAgent.bat. The seed function checks for existing records and updates them if the JSON version number is higher than the DB version.

### 8. Admin Agent Tool Breakdown (v7 — 12 source_tools + browser_action)

| Tool | Type | Gated? | Notes |
|------|------|--------|-------|
| `read_source` | Read | Free (no gate) | Reads any file |
| `write_source` | Write | Free (removed from DESTRUCTIVE_TOOLS in v7) | Creates/overwrites |
| `edit_source` | Write | Free (removed from DESTRUCTIVE_TOOLS in v7) | Exact-text replacement |
| `patch_source` | Write | Direct (not proxied) | Fuzzy find-and-replace (6 strategies) |
| `delete_source` | Destructive | DESTRUCTIVE_TOOLS gate | User cmd → do it. Own initiative → ask. |
| `run_command` | Exec | DESTRUCTIVE_TOOLS gate | Safe-command allowlist |
| `run_python` | Exec | Free | Python subprocess |
| `search_source` | Search | Free | grep with ripgrep |
| `read_directory` | Search | Free | List files with sizes |
| `git_tool` | Git | Read-only free, mutating gated | Structured git operations |
| `browser_test` | Verify | Free | HTTP fetch + verify text (redundant with browser_action) |
| `restart_server` | Server | DESTRUCTIVE_TOOLS gate | Restarts uvicorn |
| `browser_action` | Browser | Builtin (ALL agents) | Full Playwright — not gated by Codebase Admin |

### 9. Known Issues to Watch For

| # | Issue | Context |
|---|-------|---------|
| 1 | **LLM writes tool calls as text** | DeepSeek V4 Flash sometimes outputs `[Tool calls: {"name":"x",...}]` as plain text instead of executing them. First turn works, subsequent turns fail. Workaround: model swap or single-turn prompts. |
| 2 | **render_visual receives empty HTML** | Visualizer agent generates text content but passes `html=""` to render_visual. Probably LLM splitting content between text and tool params. Results in infinite loop (53+ identical calls). |
| 3 | **Admin user_id mismatch** | `admin`/`admin` login → internal user_id `admin_default`. Auth system and frontend use `admin_default` correctly. Only appears as a mismatch when testing via curl with `user_id=admin` (username). |
| 4 | **React combobox not interactable** | The template `<select>` in New Agent dialog throws "Could not compute box model" in Playwright. Use API fallback (`fetch()` in browser console) instead of `browser_click` + `browser_select_option`. |
| 5 | **React textarea not responding to Enter** | Need JS dispatch fallback: `browser_evaluate` to set textarea `.value`, then dispatch `new KeyboardEvent('keydown', {key:'Enter'})`. |
| 6 | **Server logs not accessible from WSL** | Server runs on Windows via webAgent.bat. Cannot check server-side tool rejection logs from WSL. |
| 7 | **Stream tab is source of truth** | Don't trust agent's text responses about what tools ran. Always check the Stream/Interactions tab. |
| 8 | **Quick prompts cause streaming overlap** | Sending multiple prompts rapidly causes responses to overlap and rendering to struggle. Test one at a time with waits between. |
| 9 | **Integration cards blank without OAuth config** | Google integration cards in Agent Abilities → Productivity have no labels until GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are set. |
| 10 | **http_request requires full URL** | The tool validates `url must start with http:// or https://`. Bare paths like `/api/v1/agents` silently fail. Always use `http://127.0.0.1:8080/api/v1/agents`. |
| 11 | **Template DB version stale** | After editing agent JSON, the DB still has the old version until server restart re-seeds. Always restart after template edits if testing on the live server. |
| 12 | **browser_action not gated by Codebase Admin** | All agents get it as a builtin. Must manually Deny in Tools tab for isolation tests. |
| 13 | **Store Support template may not override model identity** | Agent identifies as "general-purpose assistant" instead of "store support clerk". System prompt may be ignored by the model or template not seeded. |
| 14 | **Agent Builder uses http_request not db_query** | Must verify URLs include full `http://127.0.0.1:8080` base. Template v3 has explicit URL instructions. |
| 15 | **AHK launcher uses hardcoded `hermes`** | The `launch_tests.ahk` script launches Hermes test sessions. This toolset (pi) has different tools/knowledge — adapt for pi-style testing if using parallel agents. |

### 10. Test Setup Checklist (Generic)

```
[ ] Server running at http://localhost:8080
[ ] Signed in as admin/admin
[ ] Agent template visible in New Agent dialog
[ ] Agent created from template
[ ] Tools configured (Deny: blocked groups; Auto: required groups)
[ ] Agent selected in chat header
[ ] Test results written to temp/hermes-tester/test_<agent>.md
```

### 11. Reference Files (for deep dives)

| File | What It Covers |
|------|---------------|
| `PROJECT_SCOPE.md` | Master session log, overview, code changes made, known issues |
| `hidden_powers.md` | Full Playwright browser_action analysis, automation system, redundant tools |
| `hermes_vs_admin.md` | Tool-by-tool comparison between Hermes and webAgent Admin agent |
| `launch_tests.ahk` | AutoHotkey script for launching 3 parallel test windows |

## 12. Codebase Reference — Key Files and Their Roles

This section documents the webAgent codebase structure so you don't have to re-discover it every session.

### 12.1 UI File Map

| Path | What It Does | Key Details |
|------|-------------|-------------|
| `ui/admin-tools/admin-configuration.html` | App Configuration page HTML | ~2189 lines. Has tab strip (10 tabs) + content sections for each. Tabs are `<button>` elements with `data-section` attrs inside `#app-config-tabs`. Each section is `<section id="ac-section-{slug}">`. |
| `ui/js/app-config.js` | App Configuration logic | ~2936+ lines. Controls tab switching, LLM provider config, integrations, automation, etc. Uses `_VALID_SECTIONS = ['llm','integrations','database','optimizer','git','automation','events','app-settings','user-management','monetization']` and `_showSection()` to manage active tab. |
| `ui/agents.html` | Agents management page HTML | Agent card list with per-card sub-tabs |
| `ui/js/agents.js` | Agents page logic | ~5321+ lines. Renders agent cards with sub-tabs (Config/Tools/Agent Loop/Abilities/Members/Monetization). Defines tool tiers: `TIER_0_ADMIN` (admin-only), `TIER_1_ALWAYS_ON` (always present, hidden from toggle), `TIER_2_TOOLS` (configurable). |
| `ui/js/tabs.js` | Tab navigation logic | Shared tab switching |
| `ui/js/state.js` | Global app state + WebSocket management | `app` object, `app.userId`, `app.agents`, `app.currentAgentId`. WS dot colors: green=subscribed, yellow=connecting, red=closed/missing userId. |
| `ui/js/loop-diagram.js` | Agent loop pipeline diagram | Two layouts (horizontal in `LOOP_NODES`+`LOOP_EDGES`, vertical in `buildVerticalLayout()`). Both must be kept in sync. |
| `ui/js/loop-logic.js` | Loop logic + tool metadata | `eventToNodeId()` maps pipeline events to diagram nodes. `fetchAllToolMeta()` returns tool definitions. |
| `ui/js/loop-node-data.js` | Node display info | `NODE_PANEL_INFO` and `NODE_STATIC_ITEMS` for the loop diagram |

### 12.2 App Configuration Page — Tab Structure

The App Configuration page (`admin-configuration.html`) has these tabs (buttons in a tablist `#app-config-tabs`):

| Tab label | data-section | Section element id | Icon (lucide) |
|-----------|-------------|-------------------|---------------|
| App Settings | `app-settings` | `ac-section-app-settings` | `settings-2` |
| User Management | `user-management` | `ac-section-user-management` | `users` |
| Default LLM | `llm` | `ac-section-llm` | `bot` |
| Agent Abilities | `integrations` | `ac-section-integrations` | `puzzle` |
| Data Management | `database` | `ac-section-database` | `database` |
| Optimizer Stats | `optimizer` | `ac-section-optimizer` | `zap` |
| Git Providers | `git` | `ac-section-git` | `git-branch` |
| Automation | `automation` | `ac-section-automation` | `clock` |
| Event Sources | `events` | `ac-section-events` | `radio` |
| Monetization | `monetization` | `ac-section-monetization` | `credit-card` |

**To change tabs:** edit the `<button>` elements in `#app-config-tabs` inside `admin-configuration.html`. The `data-section` attr links the button to its content section. The JS in `app-config.js` uses `_VALID_SECTIONS` array and `_showSection()` to manage visibility.

**To add counters to tabs:** add a `<span class="ac-tab-counter">` (or similar) inside the tab button, then update `app-config.js` to populate it (e.g., from an API call or local state).

### 12.3 Agents Page — Agent Card Structure

Each agent card is an `.agent-row` wrapper. When clicked, it expands an inline `.agent-detail-panel` showing these sub-tabs (buttons, not actual tabs):

| Sub-tab button | What it shows |
|---------------|---------------|
| Config | Prompt slots, model settings, name/description, icon |
| Tools | Tier toggle UI for `TIER_2_TOOLS` (web_search, http_request, browser_action, db_query, context docs, session_search, memory, get_weather, create_tool, webhooks, rate_skill, optimizer tools). Admin-only tools (`TIER_0_ADMIN`) are never shown. Always-on tools (`TIER_1_ALWAYS_ON`) are hidden from toggle. |
| Agent Loop | Pipeline node toggles — each can be turned on/off per agent |
| Abilities | Connections/channels/integrations — shows what's globally enabled and per-agent toggles |
| Members | User authorization (authorize/restrict users, set user_mode: anonymous/register/authorized) |
| Monetization | Pricing plans, credit system, Stripe Connect config |

**To add counters:** find the agent card rendering code in `agents.js`. Each sub-tab button is rendered in a per-card loop. After the agent data is fetched (tools list, abilities list, members list), compute counts and inject them as badges (e.g., `<span class="counter-badge">N</span>`) into each button. The API endpoints already exist: `/api/v1/agents/{id}/tools`, `/api/v1/agents/{id}/connections`, `/api/v1/agents/{id}/members`.

### 12.4 Agent Template Files (in `app/context/agents/`)

| Template File | Agent Type | Key Characteristics |
|---------------|-----------|---------------------|
| `admin-agent.json` | Admin (v8) | Full codebase access via source tools. Max turns 99999. Working style: no preambles, batch calls, user op = authorization, 2-strike patch_source rule. 35 loop nodes. Prompt slots: system_prompt, agent_prompt, user_prompt, skills_prompt, tasks_prompt, misc_prompt, automation_prompt, bootstrap_tools. |
| `agent-builder.json` | Agent Builder (v3) | Uses `http_request` for ALL operations (not db_query). API endpoints reference with full URLs (`http://127.0.0.1:8080/api/v1/...`). Max turns 9999. Has ability catalog with connection_types and sections. |
| `store-support.json` | Store Support | web_search, db_query, memory, session_search. Must NOT have admin/browser tools. |
| `personal-assistant.json` | Personal Assistant | OAuth-gated gmail/gcal/drive tools. |
| `enterprise-db-agent.json` | Data Analyst | db_query, render_visual, connectors. |
| `visualizer.json` | Visualizer | render_visual, page tools, browser_action. |

### 12.5 Admin Agent Working Style (from v8 system_prompt)

The admin agent's system prompt has explicit rules for efficiency — these are the guards against wasteful behavior:

1. **No preambles before tool calls** — no "Let me check…" / "Let me see…", just call the tool
2. **Batch independent tool calls in one turn** — multiple reads/searches in same response
3. **User's instruction is authorization** — named operations are pre-approved
4. **Two-strike rule on patch_source** — if it fails twice, switch to read+exact-patch or write whole file
5. **Read before write — once** — use offset/limit to slice; after patch, diff snippet is enough (no re-read to verify)
6. **Verify by running, not re-reading** — `git diff` or `browser_action` for UI changes, not re-reading files

### 12.6 Tool Tier System (in `agents.js`)

Tools are categorized into tiers that control visibility in the agent config UI:

| Tier | Set | Behavior |
|------|-----|----------|
| Tier 0 — Admin | `read_source`, `write_source`, `edit_source`, `delete_source`, `run_command`, `restart_server`, `run_worker_trials`, `handoff_to_closer`, `deploy_optimization` | Never shown for normal agents. Only the Admin agent gets these. |
| Tier 1 — Always-on | `list_tools`, `search_tools`, `get_tool_definition`, `get_time`, `get_date`, `calculate`, `read_attachment`, `delegate_to_agent`, `list_delegatable_agents`, `register_user` | Present for all agents, not shown as toggleable. |
| Tier 2 — Configurable | `web_search`, `http_request`, `browser_action`, `db_query`, context doc tools, `session_search`, `memory`, `get_weather`, `create_tool`, webhook tools, `rate_skill`, optimizer tools | Shown as toggles in the Tools tab per agent. |

### 12.7 Backend API Endpoints (useful for testing)

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/agents?user_id={uid}` | List user's custom agents |
| `GET /api/v1/agents/{id}` | Get single agent details |
| `GET /api/v1/agents/{id}/tools?user_id={uid}` | Get agent's allowed tools |
| `GET /api/v1/agents/{id}/connections?user_id={uid}` | List abilities/channels/integrations |
| `GET /api/v1/agents/{id}/members?user_id={uid}` | List members and admins |
| `GET /api/v1/agents/templates` | List available agent templates |
| `POST /api/v1/agents` | Create new agent |
| `PUT /api/v1/agents/{id}` | Update agent config |
| `PUT /api/v1/agents/{id}/connections/{type}` | Toggle ability on/off |
| `POST /api/v1/agents/{id}/admins` | Add agent admin |
| `POST /api/v1/agents/{id}/members/{uid}/authorize` | Authorize user for agent |
| `GET /api/v1/billing/config/agent/{id}` | Get agent monetization config |
| `GET /api/v1/user/profile?user_id={uid}` | Get user profile + admin flag |

### 12.8 Backend Code Layout

| Path | What It Contains |
|------|-----------------|
| `app/main.py` | FastAPI app entry point, middleware, router mounts |
| `app/api/` | API routers (chat, agents, oauth, billing, etc.) |
| `app/agent/` | Agent runtime: loop_executor.py (DEFAULT_NODE_ORDER), pipeline nodes |
| `app/tools/` | Non-admin tool implementations (web_search, db_query, etc.) |
| `app/admin/` | Admin tools (read_source, write_source, etc.) |
| `app/integrations/` | OAuth integration tools + __init__.py (inject_integration_tools) |
| `app/context/agents/` | Agent JSON templates seeded into DB on startup |
| `app/db/` | Database setup, models, migrations |
| `app/auth/` | Authentication (users.json, OAuth, password hashing) |
| `app/visualizer/` | Page builder / visualizer tools |

### 12.9 Common Admin Agent Testing Patterns

When testing the Admin agent on code-modification tasks, expect this flow:

1. **Search first** — `search_source` to locate the relevant HTML/JS files by keyword
2. **Read targeted** — `read_source` with offset/limit, not whole-file reads
3. **Multiple reads in parallel** — if multiple files need checking, batch them
4. **Edit with `patch_source` first** — falls back to `read_source`+`edit_source` or `write_source` after 2 failures
5. **Verify with `browser_action`** — navigates to the page, takes snapshot/screenshot, checks the rendered result
6. **Only restarts server when needed** — if runtime file changed, may call `restart_server()` (always confirms first)
7. **Rarely uses `run_command`** — prefers the structured tool equivalents

**Red flags during admin agent testing:**

| Red flag | What it means | Fix |
|----------|---------------|-----|
| Reads entire file (no offset/limit) when file is >500 lines | Wasting context on large file reads | Add instruction: "Always use offset/limit for files >200 lines" |
| `patch_source` falls back to re-reading, not to `write_source` | Extra turn wasted | Reinforce the two-strike → write_source path |
| Reads file, then reads it again to "verify" | Redundant read | Add instruction: "Trust tool return values; don't re-read to verify" |
| Writes preambles before every tool call | Wasting tokens | Reinforce rule 1 in system prompt |
| Calls tools one at a time when they're independent | Extra turns wasted | Reinforce rule 2 |
| Asks "can I edit this file?" when user said to do it | Unnecessary turn wasted | Reinforce rule 3 |
| Tries more than 2 `patch_source` variants | Looping on failures | Tighten two-strike rule enforcement |

### 12.10 Database Tables for Diagnostics

Query these from `local.db` using `sqlite3`:

| Table | Useful columns | What to check |
|-------|---------------|---------------|
| `messages` | `id`, `agent_session_id`, `role`, `content`, `tool_calls`, `created_at` | What the agent actually said and what tool calls it made |
| `agent_sessions` | `id`, `agent_id`, `user_id`, `status`, `turn_count`, `created_at` | Session state — is it running, how many turns |
| `agent_templates` | `id`, `agent_type`, `version`, `system_prompt`, `skills_prompt` | What the seeded template contains (compare with JSON file) |
| `auth_elements` | `id`, `user_id`, `provider`, `token_type`, `created_at` | OAuth token storage |
| `tools` | `id`, `name`, `agent_id`, `enabled` | Per-agent tool state |

## Directory Organization: Keep Admin Tools Contained

To make it easy to purge advanced/admin features from the app, keep admin-related tools (any tool that modifies codebase files) in the `/admin/` folder or subdirectory structure as much as possible. Similarly, group related tool code into dedicated directories rather than scattering it across the codebase. This makes user administration management cleaner — you can see at a glance what belongs to which feature area, and stripping out admin capabilities means touching fewer files.

**Convention:**

```
app/
├── admin/               # Codebase admin tools (read_source, write_source, etc.)
├── tools/               # Non-admin agent tools (web_search, db_query, etc.)
├── integrations/        # OAuth integration tools (gmail, gcal, drive)
├── visualizer/          # Page builder tools
├── agent_builder/       # Agent CRUD tools (if extracted from routers)
└── ...
```

This is a guideline, not a hard rule — but prefer it when splitting or adding new functionality.

## When Testing, Always

1. Use `localhost:8080` (not the old WSL gateway IP)
2. Drive the browser via Playwright as the primary test method
3. Cross-check results against `local.db` and browser console logs
4. Write results into the test file under `## Results`
5. Give a one-line verdict in chat
6. Check the Stream tab as the source of truth
7. Use API fallback when UI elements are unruly
8. Note any model hallucination of blocked tools
9. If something is wrong: stop, fix, clear/restart, retry — then document the fix

## 13. Codebase Navigation Shortcuts

When starting a test session, you can save time by running these commands upfront:

```bash
# Check if the server is up
curl -s -o /dev/null -w '%{http_code}' http://localhost:8080

# Check the current admin agent template version (to know if re-seed needed)
sqlite3 app/db/local.db "SELECT agent_type, version FROM agent_templates ORDER BY agent_type;"

# List recent agent sessions
sqlite3 app/db/local.db "SELECT id, agent_id, user_id, turn_count, status, created_at FROM agent_sessions ORDER BY created_at DESC LIMIT 10;"

# Quick-check what messages were sent in the latest session
sqlite3 app/db/local.db "SELECT role, substr(content,1,120) FROM messages WHERE agent_session_id=(SELECT id FROM agent_sessions ORDER BY created_at DESC LIMIT 1) ORDER BY created_at;"
```
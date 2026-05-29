---
name: webagent-testing
description: >
  Testing methodology for the webAgent app. Covers agent types, tool filtering,
  loop gating, integration gating, prompt iteration workflow, and codebase reference.
  Testing is done through the REST API (not browser automation) — verify agent
  behavior by inspecting DB records, API responses, and interaction logs.
  Default mode: editor — code changes are expected.
---

# webAgent Testing Skill v2.0.0

## Your Role

When you load this skill, you become a tester for the **webAgent** agent harness at the repo root. Default mode is **editor mode** — you modify agent templates, backend logic, and frontend code to fix issues found during testing.

## Core Testing Approach: API-First

Test the app through its REST API, not a browser. The browser/UI layer is a separate concern — here you test the **agent runtime**: prompt construction, tool selection, gate enforcement, memory, and response generation.

| What to use | Why |
|-------------|-----|
| `curl` / `fetch` against the REST API | Directly test agent creation, prompt updates, chat messages, ability toggles |
| `sqlite3` (or Python's `sqlite3` module) on `app/db/local.db` | Inspect agent_prompts, messages, agent_connections, interactions tables |
| `browser_action` | Only when you need to verify a visual rendering fix (tabs, badges, layout) |

## Diagnostic Loop

1. **Send a prompt** via API to an agent session
2. **Track** what the agent actually did — check `messages` and `interactions` tables in local.db
3. **Detect issues** — wrong tool, gate blocked, hallucinated calls, preamble narration, infinite loops
4. **Fix** — either code changes (tool registration, pipeline gates) or prompt changes (update `agent_prompts` table directly)
5. **Clear memory** or delete/recreate the agent
6. **Retry** with the same prompt
7. **Document** in `temp/hermes-tester/test_<agent>.md`

## Prompt Iteration Process

The core workflow when an agent misbehaves:

1. **Identify the problem** — preamble? wrong tool? permission asking? infinite loop?
2. **Apply prompt fix** — update the agent's prompt in `agent_prompts` table directly (NOT just the JSON template file)
3. **Test via API** — send the same prompt, check results
4. **Iterate** — if still broken, strengthen the prompt further
5. **At end** — ask user if changes should be pushed to the JSON template file

**CRITICAL:** Prompt changes must go to the DB, not just the JSON file. The agent reads from `agent_prompts` table. Slot names in the DB are: `system`, `agent`, `user`, `skills`, `tasks`, `misc`, `automation`, `bootstrap_tools`. To update:

```sql
UPDATE agent_prompts SET content = '<new_prompt>' WHERE agent_id = '<id>' AND slot_name = 'system';
```

The JSON template file (`app/context/agents/*.json`) is only read by the seeder at boot. Editing it alone does NOT affect running agents.

### What worked (progressive tightening)

| Version | Key change | Effect |
|---------|-----------|--------|
| v8 | "No preambles before tool calls" | ✗ ignored |
| v9 | "ZERO preambles" + "No running commentary" | ✗ still narrated |
| v10 | "CRITICAL: Your first output MUST be a tool call. First 200 characters must be a tool call." + "Single-sentence findings only" | ✅ executed changes |

**Pattern:** Vague prohibitions get ignored. Specific, measurable rules ("first 200 chars", "at most ONE sentence") work.

## Agent Setup Best Practices

1. **Create a dedicated test agent** for each iteration (delete and recreate rather than reusing)
2. **Check abilities are enabled** before testing — an agent without `codebase_admin` can't use source tools
3. **Create a second "Read-Only" agent** for isolation tests — no Codebase Admin, its system prompt tells it to refuse edit requests
4. **Abilities are NOT part of the template** — there's no `default_connections` field in template JSON. You must enable them via API after creation:

```bash
curl -X PUT "/api/v1/agents/<id>/connections/codebase_admin?user_id=admin_default" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"admin_default","enabled":true}'
```

## Template Seeding Lifecycle

| Step | What Happens | How to Re-seed |
|------|-------------|----------------|
| 1 | JSON template edited in `app/context/agents/` | Edit the JSON file |
| 2 | Server start → `_seed_agent_templates_from_json_files()` imports JSON into DB | Restart server (uvicorn) |
| 3 | Manifest hash check — if JSON files unchanged, short-circuits | Force re-seed by bumping `version` field or calling the admin "Re-Seed" button |
| 4 | User creates agent from template → DB copy | Use the API `POST /api/v1/agents` with `template_id` |
| 5 | User edits template → DB copy diverges | No auto-sync — DB version wins |

The seeder updates `agent_templates` and `agent_prompt_templates` tables. It does NOT touch `agent_connections` (abilities).

## The 5 Agent Types

| Agent ID | Template File | Core Tools | What It Should NOT Do |
|----------|---------------|------------|----------------------|
| **Store Support** | `store-support.json` | web_search, db_query, memory, session_search | Email, calendar, Drive, admin tools, browser |
| **Personal Assistant** | `personal-assistant.json` | web_search, memory, get_time, gmail/gcal/drive (OAuth-gated) | Admin tools, file ops (unless OAuth grants it) |
| **Data Analyst** | `enterprise-db-agent.json` | db_query, web_search, memory, render_visual, connectors | Email, calendar, Drive, admin/shell tools |
| **Visualizer** | `visualizer.json` | render_visual, page tools, web_search, browser_action | Admin tools, db_query (unless needed) |
| **Agent Builder** | `agent-builder.json` | http_request (REST API), web_search, memory | db_query (uses REST API instead) |

## Tool Filtering — 4 Things to Watch For

| # | Behavior | Good | Bad |
|---|----------|------|-----|
| 1 | Can't execute blocked tools | Tool set to Deny → no tool call in interactions table → ✅ | Interactions show denied call rejected → ⚠️ |
| 2 | Can't *see* blocked tool schema | Agent uses `list_tools` → no mention of blocked tool → ✅ | Agent describes a tool it can't see → ⚠️ |
| 3 | Model hallucinates tool names | Agent says "I don't have access to X" → ✅ | Agent writes `[Tool calls: ...]` in text response → ⚠️ |
| 4 | Hallucinated tool appears to execute | Interactions table shows no tool call → ✅ | Interactions shows hallucinated call as "executed" → ❌ |

## Loop Gating

| Node | What It Gates | How to Test |
|------|---------------|-------------|
| `guardrails` | Tool call permission checks | Try a tool that requires confirmation |
| `memory_search` | Pre-prompt memory retrieval | Check interactions for memory_search events |
| `memory_save` | Post-response memory storage | Verify important info gets saved in messages |
| `interrupt_chk` | Destructive operation confirmation | Try delete_source on own initiative vs user command |
| `destructive_chk` | DESTRUCTIVE_TOOLS code gate | write_source/edit_source (removed from gate in v7) — should pass through |

## Admin Agent Tool Breakdown

| Tool | Type | Notes |
|------|------|-------|
| `read_source` | Read | Free — no gate |
| `write_source` | Write | Free — removed from DESTRUCTIVE_TOOLS in v7 |
| `edit_source` | Write | Free |
| `patch_source` | Write | Direct — fuzzy find-and-replace (6 strategies) |
| `delete_source` | Destructive | User cmd → do it. Own initiative → ask. |
| `run_command` | Exec | Safe-command allowlist |
| `run_python` | Exec | Free |
| `search_source` | Search | Free |
| `read_directory` | Search | Free |
| `git_tool` | Git | Read-only free, mutating gated |
| `browser_action` | Browser | Builtin — ALL agents get it, not gated by Codebase Admin |

## Known Issues

| # | Issue | Context |
|---|-------|---------|
| 1 | **LLM writes tool calls as text** | DeepSeek V4 Flash outputs `[Tool calls: ...]` as plain text instead of executing. First turn works, subsequent turns fail. |
| 2 | **render_visual receives empty HTML** | Visualizer passes `html=""` while generating text content. Infinite loop (53+ identical calls). |
| 3 | **Admin user_id mismatch** | `admin`/`admin` login → internal user_id `admin_default`. Only appears as mismatch when testing via curl. |
| 4 | **Template DB version stale** | After editing agent JSON, the DB keeps the old version until the seeder runs (server restart or Re-Seed). |
| 5 | **browser_action not gated by Codebase Admin** | All agents get it as builtin. Must manually Deny in Tools tab for isolation tests. |
| 6 | **Store Support template may not override model identity** | Agent says "general-purpose assistant" instead of "store support clerk". |
| 7 | **Agent Builder uses http_request not db_query** | Must verify URLs include full `http://127.0.0.1:8080` base. |

## Codebase Reference

### UI File Map

| Path | What It Does | Key Details |
|------|-------------|-------------|
| `ui/admin-tools/admin-configuration.html` | App Configuration page HTML | ~2189 lines. Tab buttons with `data-section` attrs in `#app-config-tabs`. |
| `ui/js/app-config.js` | App Configuration logic | ~2936 lines. `_VALID_SECTIONS` array, `_showSection()` manages active tab. |
| `ui/agents.html` | Agents management page HTML | Agent card list with per-card sub-tabs |
| `ui/js/agents.js` | Agents page logic | ~5321 lines. `_populateAgentTabBar()`, tool tiers (Tier 0/1/2). |
| `ui/js/tabs.js` | Tab navigation logic | Shared tab switching |
| `ui/js/state.js` | Global app state + WebSocket management | `app.userId`, `app.agents`, `app.currentAgentId` |
| `ui/js/loop-diagram.js` | Agent loop pipeline diagram | Two layouts (horizontal + vertical), must keep in sync |

### App Configuration Tab Structure

| Tab label | `data-section` | Section element id |
|-----------|---------------|-------------------|
| App Settings | `app-settings` | `ac-section-app-settings` |
| Agent Abilities | `integrations` | `ac-section-integrations` |
| User Management | `user-management` | `ac-section-user-management` |
| Models | `llm` | `ac-section-llm` |
| Data Management | `database` | `ac-section-database` |
| Optimizer Stats | `optimizer` | `ac-section-optimizer` |
| Git Providers | `git` | `ac-section-git` |
| Automation | `automation` | `ac-section-automation` |
| Event Sources | `events` | `ac-section-events` |
| Monetization | `monetization` | `ac-section-monetization` |

### Agent Card Sub-Tabs

| Sub-tab | What it shows |
|---------|---------------|
| Config | Prompt slots, model settings, name/description |
| Tools | Tier toggle UI for Tier 2 tools |
| Agent Loop | Pipeline node toggles |
| Abilities | Connections/channels/integrations toggles |
| Members | User authorization |
| Monetization | Pricing plans, Stripe Connect |

### Backend API Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /api/v1/agents?user_id={uid}` | List user's custom agents |
| `GET /api/v1/agents/{id}` | Get single agent details |
| `GET /api/v1/agents/{id}/connections?user_id={uid}` | List abilities/channels/integrations |
| `GET /api/v1/agents/{id}/members?user_id={uid}` | List members and admins |
| `GET /api/v1/agents/{id}/slots?user_id={uid}` | Get prompt slot definitions |
| `POST /api/v1/agents` | Create new agent |
| `PUT /api/v1/agents/{id}` | Update agent config |
| `PUT /api/v1/agents/{id}/connections/{type}` | Toggle ability on/off |
| `POST /api/v1/agents/{id}/admins` | Add agent admin |
| `GET /api/v1/agents/templates` | List available agent templates |
| `GET /admin/automations/count` | Total automation rules across all agents |

### Backend Code Layout

| Path | Contents |
|------|----------|
| `app/main.py` | FastAPI entry point, middleware, router mounts |
| `app/api/` | API routers (chat, agents, oauth, billing) |
| `app/agent/` | Agent runtime: loop_executor.py (DEFAULT_NODE_ORDER), pipeline nodes |
| `app/admin/` | Admin tools (read_source, write_source, etc.) |
| `app/tools/` | Non-admin tool implementations |
| `app/integrations/` | OAuth integration tools + `inject_integration_tools()` |
| `app/context/agents/` | Agent JSON templates seeded into DB on startup |
| `app/db/` | Database setup, models, migrations. `local.py` for SQLite. |

### Agent Template Prompt Slots

| Slot | Controls |
|------|----------|
| `system_prompt` | Identity & rules |
| `agent_prompt` | Persona elaboration |
| `user_prompt` | User context, envelope conventions |
| `skills_prompt` | Tool docs, reference tables |
| `tasks_prompt` | Step-by-step recipes |
| `misc_prompt` | Catch-all guidance |
| `automation_prompt` | Scheduled jobs description |
| `bootstrap_tools` | Always-loaded tools list |

### Database Tables for Diagnostics

| Table | Useful columns | What to check |
|-------|---------------|---------------|
| `messages` | `id`, `agent_session_id`, `role`, `content`, `tool_calls`, `created_at` | What the agent actually said and what tool calls it made |
| `agent_sessions` | `id`, `agent_id`, `user_id`, `status`, `turn_count`, `created_at` | Session state |
| `agent_prompts` | `agent_id`, `slot_name`, `content`, `template_version` | What prompt the agent is actually using |
| `agent_connections` | `agent_id`, `connection_type`, `enabled` | Which abilities are enabled per agent |
| `interactions` | `agent_session_id`, `tool_name`, `status`, `created_at` | Tool call execution history |

## Admin Agent Working Style (v10)

1. **CRITICAL: First output MUST be a tool call.** First 200 characters must be a valid tool call.
2. **Batch independent tool calls** in one turn.
3. **User's instruction IS authorization** — do NOT present a plan or ask permission.
4. **Single-sentence findings only** — one sentence max per tool result.
5. **Two-strike rule on patch_source** — switch to write_source after 2 failures.
6. **Read before write — once.** Use offset/limit. Trust diff snippets.
7. **Verify by running** — git diff or browser_action, not re-reading files.

# Future Plans - webAgent

Current gaps vs pi (coding agent) and Hermes (full agent platform).
Ordered by impact, highest first.

---

## 1. Parallel Tool Execution

**What we have:** Sequential - one tool call at a time. Agent waits for result, then re-calls LLM for next turn.

**What we need:** Execute independent tool calls concurrently. If the LLM emits 3 tool calls, run them in parallel and collect results before the next LLM turn.

**Why:** ~3x faster for multi-tool tasks (e.g. read 3 files, search 2 DBs, hit 2 APIs).

**Reference:** Both pi and Hermes support parallel tool execution. pi uses `Promise.all()`, Hermes has parallel + sequential modes configurable per tool.

---

## 2. Streaming Response (Default)

**What we have:** Simple loop blocks until the complete response is ready, returns JSON. A separate streaming generator exists but isn't the primary path.

**What we need:** Default streaming - emit tokens as they arrive from the LLM. The caller (browser, CLI, API client) gets real-time output instead of waiting for the full turn.

**Why:** Perceived latency drops from "wait 10-30s for full response" to "first token in <1s". Essential for chat UX.

**Reference:** Hermes uses streaming delta callbacks. pi's `EventStream` emits `message_start/message_delta/message_end` events as tokens arrive.

---

## 3. Retry & Self-Correction Loop

**What we have:** If a tool call fails (invalid tool, malformed JSON, API 500) → log error → return error in response. Agent stops.

**What we need:** Three-tier retry - invalid tool names get fed back to the model for self-correction, malformed JSON silently re-calls the LLM, empty responses retry up to 3 times, tool execution errors retry with backoff. After tool calls, if the next response is empty, nudge the model to continue.

**Why:** Models hallucinate tool names and malform JSON frequently. Without retry, one bad tool call ends the session.

**Reference:** Hermes has 5 retry counters (invalid tool, invalid JSON, empty content, incomplete scratchpad, thinking prefill) plus a post-tool-empty nudge.

---

## 4. Credential Rotation

**What we have:** API key loaded once from env at module level. Never refreshed. If key expires or is revoked mid-session → crash.

**What we need:** Re-resolve credentials before each request (or at least before each LLM call). Support environment variable changes, key rotation providers, and token refresh.

**Why:** API keys expire. Rate-limit bypass keys are rotated. Long-running agents die on key expiry.

**Reference:** Hermes re-resolves credentials every turn via `_ensure_runtime_credentials()`, detects changes, and invalidates the cached agent if credentials changed.

---

## 5. Fallback Model Chain

**What we have:** Single model via env var. If the provider is down or the model returns errors → agent fails.

**What we need:** Ordered fallback chain - if primary provider/model fails, try next in line (e.g. OpenRouter → OpenAI Codex → Anthropic). Activation triggers include auth failure, rate-limit, connection errors, and empty responses after retry exhaustion. Fallback is per-turn (next turn tries primary again).

**Why:** If the primary provider goes down, the agent is dead until it comes back. Fallback ensures uptime.

**Reference:** Hermes supports init-time fallback (no API key for primary → try fallback at startup), per-turn credential fallback (auth error → walk chain), and runtime fallback (consistent empty responses → switch provider mid-turn).

---

## 6. Context References (@file, @diff, @folder)

**What we have:** Nothing. You paste file contents manually into the chat.

**What we need:** Expand `@file:path/to/file.py` → read file, inject contents into message. Same for `@diff` (git diff), `@folder:src/` (list/read all files), `@git:log` (git log), `@web:URL` (fetch URL). Should respect model context-length limits to avoid blowing the window.

**Why:** Massively reduces friction. User says "fix bug in @file:main.py" instead of opening, copying, pasting, then asking.

**Reference:** Hermes `agent/context_references.py` supports file, diff, folder, git, web, and glob patterns with context-length awareness.

---

## 7. Image Handling

**What we have:** No image support. Only text input.

**What we need:** Accept images (base64 or URL) in chat requests. Route to model's native vision API if supported, or fall back to LLM-based vision analysis for non-vision models.

**Why:** Many tasks require visual context - screenshots, diagrams, UI mockups, error screenshots.

**Reference:** Hermes has a two-tier decision chain: native passthrough for vision-capable models, LLM-based `vision_analyze` description generator for non-vision models.

---

## 8. System Prompt Caching

**What we have:** System prompt rebuilt from DB every single request - queries for context documents, memory, brain results, then concatenates into a new prompt string.

**What we need:** Cache the system prompt per user+session. Only rebuild when underlying context documents change (detected via DB `updated_at` timestamps). TTL-based or event-driven invalidation.

**Why:** DB query + template rendering + concatenation adds ~50-200ms per request for zero benefit when context hasn't changed.

**Reference:** Hermes caches system prompt per AIAgent session - built once, reused for all calls, only invalidated on context compression.

---

## 9. Iteration Budget (Subagent Tracking)

**What we have:** Hardcoded max turns. No awareness of subagents - if a tool spawns a sub-agent, those turns are unbounded and eat into the same counter.

**What we need:** Budget class that tracks total LLM calls across the main agent and any spawned subagents. Budget resets per-turn. When exhausted, agent stops gracefully.

**Why:** Without budget tracking, a rogue subagent could burn high API costs. The budget also enables predictable cost-per-turn.

**Reference:** Hermes has an `IterationBudget` class with per-call consumption checks. pi uses a `shouldStopAfterTurn` callback.

---

## 10. Configurable Turn Permission Gate

**What we have:** Hardcoded at 10 turns - asks "Would you like me to continue?" with hardcoded keyword matching. No way to disable, change threshold, or customize.

**What we need:** Configurable via settings - enable/disable, threshold, extension amount, max turns, custom prompt text, custom keywords. Support modes: disabled (never ask), once (ask once at threshold), every N (ask periodically), auto-extend (batch/API mode).

**Why:** Different use cases need different permission behavior. Batch processing should never prompt. Interactive chat might prompt every 10 turns.

---

## Prioritization

| Priority | Feature | Effort | Impact |
|----------|---------|--------|--------|
| P0 | Parallel tool execution | 1 day | Speed 3x, essential for multi-tool tasks |
| P0 | Streaming response | 1-2 days | UX, perceived latency drops 10x |
| P1 | Retry & self-correction | 2 days | Reliability, prevents session death |
| P1 | System prompt caching | 0.5 day | Request latency -50 to 200ms |
| P2 | Configurable permission gate | 0.5 day | UX flexibility |
| P2 | Iteration budget | 1 day | Cost control |
| P3 | Context references (@file) | 1 day | Developer UX |
| P3 | Image handling | 2-3 days | Feature parity |
| P4 | Credential rotation | 1 day | Reliability for long-running |
| P4 | Fallback model chain | 2 days | Availability during provider outages |

---

## Architecture Notes

- All new features should live in `app/agent/` as isolated modules (one file per feature).
- The streaming loop should become the single entry point. The simple loop gets deprecated.
- Features should be feature-flagged via user settings in DB. Individual users can opt in/out.
- Retry counters, fallback state, and iteration budget should be per-request (not global) to avoid cross-user state leaks.
- Tool execution errors should include structured error info (error type, recoverable bool, hint string) so the frontend can display appropriate UI.

---

## 11. LLM provider message adapters (OpenAI messages + beyond)

**What we have:** Session turns are persisted as `interactions` rows (`user` / `assistant` / `tool`). The live loop calls OpenRouter using an **OpenAI-compatible** Chat Completions client, so the in-memory `messages` list follows the **OpenAI messages** shape (`role`, `content`, optional `tool_calls` / `tool_call_id`).

**What we need:** Treat the DB transcript as the **canonical** session log. Add an explicit **adapter layer** that maps `interactions` → **OpenAI messages** for the current client path, and leaves room for additional adapters when a user must call a provider that is **not** OpenAI-compatible (e.g. native Anthropic Messages, Gemini, or a custom enterprise gateway with a different JSON schema).

**Why:** Not all providers use the same wire format. OpenRouter today aligns with OpenAI-style chat; SMS/WhatsApp and other channels should not depend on "whatever the browser sent last refresh" - they depend on DB + the correct adapter for the configured provider.

**Concrete work (when implementing multi-provider):**

- Keep **`interactions`** as the single source of truth (no duplicate per-provider transcript tables unless required).
- Implement `interactions_to_openai_messages(...)` (or equivalent) for the default path.
- Add `interactions_to_<provider>_...(...)` behind a small interface (e.g. `MessageAdapter` + registry keyed by `provider` / `api_style` in agent config).
- Unit-test each adapter: same seeded `interactions` rows → expected provider payload; tool turns must preserve `tool_calls` ↔ `tool` pairing.

**Reference:** OpenAI Chat Completions message list docs; Anthropic Messages API (different structure); provider docs for any new backend.


pi's Tools vs webAgent's - Side by Side

 ┌─────────────┬─────────────────────────────┬──────────────────────┬────────────────────────────┐
 │ Tool        │ webAgent                    │ pi                   │ pi Advantage               │
 ├─────────────┼─────────────────────────────┼──────────────────────┼────────────────────────────┤
 │ read file   │ read_source(path)           │ read(path, offset,   │ Offset/limit for large     │
 │             │                             │ limit)               │ files. Reads images        │
 │             │                             │                      │ (jpg/png/gif).             │
 │             │                             │                      │ Auto-truncates.            │
 ├─────────────┼─────────────────────────────┼──────────────────────┼────────────────────────────┤
 │ write file  │ write_source(path, content) │ write(path, content) │ Same. pi creates parent    │
 │             │                             │                      │ dirs automatically.        │
 ├─────────────┼─────────────────────────────┼──────────────────────┼────────────────────────────┤
 │ edit file   │ edit_source(path, old, new) │ edit(path, edits[])  │ pi validates               │
 │             │  - single replacement       │ - multiple disjoint  │ non-overlapping edits,     │
 │             │                             │ edits in one call    │ merges nearby changes.     │
 │             │                             │                      │ webAgent does one          │
 │             │                             │                      │ str.replace(old, new, 1).  │
 ├─────────────┼─────────────────────────────┼──────────────────────┼────────────────────────────┤
 │ delete file │ delete_source(path, rec)    │ ❌ No built-in       │ webAgent wins here -       │
 │             │                             │ delete. Done via     │ dedicated tool             │
 │             │                             │ bash rm              │                            │
 ├─────────────┼─────────────────────────────┼──────────────────────┼────────────────────────────┤
 │ shell       │ run_command(cmd, timeout)   │ bash(cmd, timeout)   │ pi runs in the agent's     │
 │             │                             │                      │ working directory,         │
 │             │                             │                      │ auto-truncates output.     │
 │             │                             │                      │ Same capability.           │
 ├─────────────┼─────────────────────────────┼──────────────────────┼────────────────────────────┤
 │ search      │ ❌                          │ grep(pattern) -      │ pi-only. Fast codebase     │
 │             │                             │ ripgrep-powered      │ search.                    │
 ├─────────────┼─────────────────────────────┼──────────────────────┼────────────────────────────┤
 │ find files  │ ❌                          │ find(pattern) -      │ pi-only. File discovery by │
 │             │                             │ fd-powered           │ name.                      │
 ├─────────────┼─────────────────────────────┼──────────────────────┼────────────────────────────┤
 │ list dir    │ ❌                          │ ls(path)             │ pi-only. Directory         │
 │             │                             │                      │ listing.                   │
 ├─────────────┼─────────────────────────────┼──────────────────────┼────────────────────────────┤
 │ browser     │ browser_action(...) - 11    │ ❌ No built-in.      │ webAgent wins - full       │
 │             │ actions, Playwright         │ OpenClaw adds one.   │ headless browser built-in. │
 ├─────────────┼─────────────────────────────┼──────────────────────┼────────────────────────────┤
 │ screenshot  │ take_screenshot(monitor,    │ ❌                   │ webAgent-only              │  │ screenshot  │ take_screenshot(monitor,    │ ❌                   │ webAgent-only.             │  │             │ region)                     │                      │                            │
 │             │ region)                     │                      │                            │
 ├─────────────┼─────────────────────────────┼──────────────────────┼────────────────────────────┤
 │ restart     │ restart_server()            │ ❌                   │                            │  │ restart     │ restart_server()            │ ❌                   │ webAgent-only.             │  └─────────────┴─────────────────────────────┴──────────────────────┴────────────────────────────┘



pi's real additions are session management and UX:

 ┌─────────────────┬─────────────────────────────────────────────────────────────────────────────┐
 │ Feature         │ What it does                                                                │
 ├─────────────────┼─────────────────────────────────────────────────────────────────────────────┤
 │ Session tree    │ Branch at any message, fork, explore alternatives. webAgent is linear.      │
 ├─────────────────┼─────────────────────────────────────────────────────────────────────────────┤
 │ Compaction      │ Auto-summarizes long conversations to stay under context limits. webAgent   │
 │                 │ just caps at 10-20 turns.                                                   │
 ├─────────────────┼─────────────────────────────────────────────────────────────────────────────┤
 │ Model cycling   │ Ctrl+P to switch models mid-session (e.g., fast/cheap model for simple      │
 │                 │ work, smart model for hard work).                                           │
 ├─────────────────┼─────────────────────────────────────────────────────────────────────────────┤
 │ Thinking        │ See the model's reasoning chain in real time.                               │
 │ display         │                                                                             │
 ├─────────────────┼─────────────────────────────────────────────────────────────────────────────┤
 │ TUI editor      │ Message editing, history navigation, keyboard shortcuts.                    │
 ├─────────────────┼─────────────────────────────────────────────────────────────────────────────┤
 │ Slash commands  │ /compact, /model, /resume, /handoff - built-in workflow control.            │
 ├─────────────────┼─────────────────────────────────────────────────────────────────────────────┤
 │ Skills system   │ SKILL.md files that load on-demand into the system prompt.                  │
 ├─────────────────┼─────────────────────────────────────────────────────────────────────────────┤
 │ Extensions      │ TypeScript plugins that intercept tool calls, add permission gates, inject  │
 │                 │ context.                                                                    │
 ├─────────────────┼─────────────────────────────────────────────────────────────────────────────┤
 │ Prompt          │ /mycommand expands to full prompt content.                                  │
 │ templates       │                                                                             │
 ├─────────────────┼─────────────────────────────────────────────────────────────────────────────┤
 │ Auto-retry      │ Configurable retry on LLM errors.                                           │
 ├─────────────────┼─────────────────────────────────────────────────────────────────────────────┤
 │ Context files   │ AGENTS.md, CLAUDE.md auto-loaded into system prompt.                        │
 └─────────────────┴─────────────────────────────────────────────────────────────────────────────┘


Guardrails: pi vs webAgent

 ### pi Has - webAgent Doesn't

 ┌────┬─────────────────┬──────────────────────────────────┬─────────────────────────────────────┐
 │ #  │ Guardrail       │ What It Does                     │ How pi Does It                      │
 ├────┼─────────────────┼──────────────────────────────────┼─────────────────────────────────────┤
 │ 1  │ Tool call       │ Extension can inspect, log,      │ pi.on("tool_call", async (event,    │
 │    │ interception    │ block, or modify any tool call   │ ctx) => { return { block: true };   │
 │    │                 │ before execution                 │ })                                  │
 ├────┼─────────────────┼──────────────────────────────────┼─────────────────────────────────────┤
 │ 2  │ Dangerous bash  │ Prompts user before rm -rf,      │ Extension checks                    │
 │    │ confirmation    │ sudo, chmod 777                  │ event.input.command against regex,  │
 │    │                 │                                  │ calls ctx.ui.confirm()              │
 ├────┼─────────────────┼──────────────────────────────────┼─────────────────────────────────────┤
 │ 3  │ Write/edit path │ Blocks writes to .env, .git/,    │ Extension checks event.input.path,  │
 │    │ protection      │ node_modules/                    │ returns { block: true }             │
 ├────┼─────────────────┼──────────────────────────────────┼─────────────────────────────────────┤
 │ 4  │ Read path       │ Blocks reads of .env, secrets,   │ Override read tool, reject matches  │
 │    │ protection      │ credentials, ~/.ssh, ~/.aws,     │                                     │
 │    │                 │ ~/.gnupg                         │                                     │
 ├────┼─────────────────┼──────────────────────────────────┼─────────────────────────────────────┤
 │ 5  │ Session action  │ Confirms before                  │ pi.on("session_before_switch"),     │
 │    │ confirmation    │ clearing/switching/forking       │ pi.on("session_before_fork")        │
 │    │                 │ sessions                         │                                     │
 ├────┼─────────────────┼──────────────────────────────────┼─────────────────────────────────────┤
 │ 6  │ OS-level        │ Filesystem + network isolation   │ @anthropic-ai/sandbox-runtime       │
 │    │ sandbox         │ via bubblewrap (Linux) or        │ extension                           │
 │    │                 │ sandbox-exec (macOS)             │                                     │
 ├────┼─────────────────┼──────────────────────────────────┼─────────────────────────────────────┤
 │ 7  │ Git             │ Auto-stashes changes at each     │ Extension hooks turn_end            │
 │    │ checkpointing   │ turn for undo                    │                                     │
 ├────┼─────────────────┼──────────────────────────────────┼─────────────────────────────────────┤
 │ 8  │ Dirty repo      │ Prevents session switch with     │ Extension checks git status before  │
 │    │ guard           │ uncommitted changes              │ session_before_switch               │
 ├────┼─────────────────┼──────────────────────────────────┼─────────────────────────────────────┤
 │ 9  │ Tool override   │ Replace built-in tools with      │ pi.registerTool({ name: "read", ... │
 │    │                 │ audited/restricted versions      │ }) - same name overrides built-in   │
 ├────┼─────────────────┼──────────────────────────────────┼─────────────────────────────────────┤
 │ 10 │ Access logging  │ Logs every file read to disk for │ Override read → appendFile(logFile, │
 │    │                 │ audit trail                      │ entry)                              │
 ├────┼─────────────────┼──────────────────────────────────┼─────────────────────────────────────┤
 │ 11 │ Output          │ Auto-truncates all output at     │ Built into every pi tool            │
 │    │ truncation      │ 50KB / 2000 lines                │                                     │
 ├────┼─────────────────┼──────────────────────────────────┼─────────────────────────────────────┤
 │ 12 │ Tool loop       │ Detects repeated non-progressing │ OpenClaw's tool-loop-detection.ts   │
 │    │ detection       │ tool calls                       │                                     │
 ├────┼─────────────────┼──────────────────────────────────┼─────────────────────────────────────┤
 │ 13 │ Abort signals   │ User can cancel any running tool │ AbortSignal passed to every tool's  │
 │    │                 │ mid-execution                    │ execute()                           │
 ├────┼─────────────────┼──────────────────────────────────┼─────────────────────────────────────┤
 │ 14 │ File mutation   │ Batches writes for undo safety,  │ withFileMutationQueue() wrapper     │
 │    │ queue           │ prevents races                   │ around all writes                   │
 ├────┼─────────────────┼──────────────────────────────────┼─────────────────────────────────────┤
 │ 15 │ Bash working    │ Verifies cwd exists before       │ existsSync(cwd) in bash tool        │
 │    │ directory check │ spawning shell                   │                                     │
 ├────┼─────────────────┼──────────────────────────────────┼─────────────────────────────────────┤
 │ 16 │ Headless safety │ Auto-blocks dangerous ops in     │ if (!ctx.hasUI) return { block:     │
 │    │                 │ non-interactive mode             │ true }                              │
 ├────┼─────────────────┼──────────────────────────────────┼─────────────────────────────────────┤
 │ 17 │ Auto-retry      │ Configurable LLM error retry     │ Built into pi's agent loop          │
 │    │                 │ with backoff                     │                                     │
 ├────┼─────────────────┼──────────────────────────────────┼─────────────────────────────────────┤
 │ 18 │ Path traversal  │ Resolves paths relative to       │ resolveToCwd() in all file tools    │
 │    │ prevention      │ workspace, normalizes ..         │                                     │
 └────┴─────────────────┴──────────────────────────────────┴─────────────────────────────────────┘

 ────────────────────────────────────────────────────────────────────────────────

 ### webAgent Has - pi Doesn't

 ┌───┬────────────────────┬──────────────────────────────────────────────────────────────────────┐
 │ # │ Guardrail          │ What It Does                                                         │
 ├───┼────────────────────┼──────────────────────────────────────────────────────────────────────┤
 │ 1 │ System prompt      │ [CRITICAL RULE] in every prompt: model must ask user before          │
 │   │ confirmation rules │ destructive tools. Only defense that exists for                      │
 │   │                    │ edit_source/write_source/delete_source/run_command/restart_server    │
 ├───┼────────────────────┼──────────────────────────────────────────────────────────────────────┤
 │ 2 │ Syntax validation  │ ast.parse() checks .py files, json.loads() checks .json before       │
 │   │ on write           │ saving                                                               │
 ├───┼────────────────────┼──────────────────────────────────────────────────────────────────────┤
 │ 3 │ Backups on write   │ Auto-creates .source-backups/<file>.<timestamp>.bak before every     │
 │   │                    │ overwrite                                                            │
 ├───┼────────────────────┼──────────────────────────────────────────────────────────────────────┤
 │ 4 │ Turn limit         │ Hard cap at 10-20 turns per conversation                             │
 ├───┼────────────────────┼──────────────────────────────────────────────────────────────────────┤
 │ 5 │ localhost only     │ WebSocket endpoints reject non-loopback connections                  │
 └───┴────────────────────┴──────────────────────────────────────────────────────────────────────┘ 


---

## 12. Pipeline Rules System (Conversational Self-Configuration)

**Current state:** Does not exist. The optimizer loop is a proto.

**Goal:** The agent accumulates behavioral rules through conversation. User says "don't search the web before checking memory" → agent creates a structured rule → rule persists across sessions.

**Mechanism:**
- New DB table or memory pages: `pipeline_rules` (user_id, priority, trigger, action, condition, source, enabled)
- Rules are loaded into system prompt at startup (or injected via `before_agent_start` hook)
- User feedback → agent interprets → writes rule → next turn uses it
- Loop view shows rules as an editable list alongside the live pipeline visualization

**Design principles:**
- Rules are soft constraints, not hardcoded DAG. Agent can override with reasoning.
- Rule priority determines conflict resolution.
- Periodically, the agent reviews its own rules and prunes stale/contradictory ones.
- No visual workflow editor needed — the LLM translates natural language to structured rules.

**Open questions:**
- How to validate rule interactions (rule A says X, rule B says not-X)?
- How to handle rule accumulation without decay (50 rules becomes noise)?
- Should rules be stored in the DB or as files in a `plugins/` directory?

---

## 13. Scheduled Tasks (jobs.md + External Scheduler)

**Current state:** Does not exist.

**Vision:** Not building an internal job queue. Instead:
1. Agent writes a `jobs.md` file describing scheduled tasks in structured markdown
2. An external scheduler (Google Cloud Scheduler, cron on the VM) periodically reads the file
3. Scheduler triggers the agent via webhook or API call with the job context
4. Agent executes the task and logs result

**Why this approach:**
- Avoids building infrastructure (queues, workers, retries, scaling)
- Leverages battle-tested external schedulers
- The agent's job is to manage the config, not run the scheduler
- Pattern matches pi's philosophy: don't build it, use what exists

**Components needed:**
- `jobs.md` format spec (cron expression, task description, parameters, expected output)
- Agent tools to read/write/update `jobs.md`
- Scheduler endpoint (`POST /api/v1/scheduler/tick`) that the external cron hits
- Task execution loop (load job, run, log result, update status)
- Error handling and retry (or delegate to scheduler's retries)

---

## 14. Multi-Agent Coordination

**Current state:** Optimizer loop runs as a secondary async task after each chat turn. It's a proto — one sub-agent reviewing the main agent's work.

**Vision:**
- An agent can spawn a sub-agent, pass it a task, and get results later
- Background tasks run in separate loops (not blocking the main chat)
- Sub-agents have their own tool access, memory, and session
- Results feed back into the main session

**Phases:**
1. **Current:** Optimizer — single sub-agent reviews and suggests
2. **Near:** Agent can spawn a parallel worker for independent work (e.g., "scrape all these URLs while I continue chatting")
3. **Future:** Full agent registry, inter-agent messaging, hierarchical task decomposition

**Architecture considerations:**
- Agent registry (who's alive, what are they doing)
- Task queue for background work
- Result channel (how the child returns data to the parent)
- Isolation (child agents shouldn't corrupt parent context)
- Budget tracking across the agent tree

---

## 15. Agent Tool-Creation (Self-Bootstrapping)

**Current state:** Works at a basic level. Agent can call `create_tool` with JSON Schema + Python code, stored in DB, callable next turn. But no UI, no validation UI, no versioning.

**Needs:**
- **Validation:** `ast.parse()` + sandboxed `exec()` test run before saving. If the tool throws an error, reject it with a helpful message.
- **Tool marketplace / browse UI:** List all created tools, search, inspect parameters, enable/disable.
- **Versioning:** Each tool edit creates a new version. Rollback supported.
- **Agent-suggested tools:** When the agent notices a repeated pattern, it proactively suggests: "I notice you keep asking me to check stock prices. Want me to create a `get_stock_price` tool?"
- **Tool dependencies:** A tool that needs `pip install yfinance` should be able to declare its dependency and have it auto-installed.

---

## 16. Persistent Configuration (Git + Reload)

**Current state:** Config lives in the DB (context documents, tools, memory). Changes persist in the DB. No git tracking.

**Vision:**
- All user-customizable behavior lives in a `plugins/` directory as Python files
- The agent modifies these files via `edit_source`/`write_source`
- Each change is a `git commit` with an auto-generated message describing the change
- User can see a changelog and rollback any change
- Forking = `git checkout -b <branch>`
- Switching forks = `git checkout <branch>` + reload
- Pushing = `git push` to a private repo for backup

**This solves:**
- Rollback (git revert)
- Experimentation (branches)
- Multiple configurations ("fast mode" branch, "thorough researcher" branch)
- Backup (push to GitHub/GitLab)
- Transparency (user can `git log` to see what changed)

**Pre-commit validation:**
- `python -c "compile(open(path).read())"` before each commit
- If syntax error: reject, tell the agent to fix it
- Optionally run tests before merge to main branch

---

## 17. Loop View as Bidirectional Configuration Canvas

**Current state:** Loop view shows `tool_call`, `tool_result`, `pipeline`, `db` events as a scrollable log. Read-only firehose.

**Vision:** Each event node is clickable for feedback:
- **Right-click → "Change this tool's behavior"** — opens inline editor for tool parameters
- **Right-click → "Always do this after X"** — creates a pipeline rule
- **Right-click → "Skip this next time"** — temporarily suppresses the tool
- **Drag to reorder** — agent interprets the new sequence as a hint

User modifications flow back through the optimizer:
```
User clicks → popup with natural language prompt
  → Agent receives feedback as structured event
  → Agent interprets → writes/updates plugin file
  → git commit → reload → next turn uses new config
```

**Architecture needed:**
- Each event in the stream needs an `event_id` for the UI to reference
- The UI needs a feedback channel — `POST /api/v1/pipeline/feedback`
- The agent loop needs a `feedback` event handler that routes user modifications to the optimizer

---

## 18. Plugin System (Python Files + Git Versioning)

**Current state:** No plugin system. Tools are DB rows. Config is DB rows. Everything lives in the database.

**Vision:**
```
webagent/
├── plugins/
│   ├── vanilla/           # Ships with app, read-only
│   ├── user/              # Modifications from conversation
│   └── admin/             # Admin-installed plugins
├── .git/                  # Full history of all changes
└── app/plugin_loader.py   # Scans plugins/ at startup
```

**How it works:**
1. Agent modifies `plugins/user/` files via `edit_source`
2. Each change is auto-committed to git
3. On startup, `plugin_loader.py` imports all files in order: vanilla → admin → user
4. A plugin can override any function or register new tools
5. The agent can create forks (branches) for experimentation

**Forking model:**
- `main` — vanilla, stable
- `experimental` — agent tries something new
- `user-<name>` — per-user personality
- Switching branches = `git checkout` + reload
- User can merge or discard

**Graceful failure:**
- If a plugin has a syntax error, fall back to the previous working version
- Log the error and tell the user
- The agent can `git diff` to see what changed and fix it

---

## 19. Feedback-to-Config Pipeline

**Current state:** No feedback pipeline. User feedback is verbal only — agent may remember it within the same session, but it's lost on refresh.

**Vision:** Natural language feedback drives config changes:
```
User says: "You're too slow, stop searching the web every time"
  → Agent receives as structured feedback event
  → Optimizer loop processes the pattern
  → Agent writes change to plugins/user/preferences.py
  → git commit "User feedback: suppress default web_search"
  → Next turn: web_search is skipped unless explicitly requested
```

**Key design decisions:**
- Feedback doesn't have to be perfect first time. Agent can ask clarifying questions.
- Changes are incremental and reversible.
- Optimizer can proactively suggest changes after repeated corrections.
- Each feedback→config change is a single git commit with the user's original message as the commit body.

---

## Prioritization (Addendum)

| Priority | Feature | Effort | Impact | Dependency |
|----------|---------|--------|--------|------------|
| P0 | Plugin system (Python files + git) | 3-5 days | Foundation for everything below | None |
| P1 | Loop view as bidirectional canvas | 5-7 days | UX: transparency + control | Streaming events already wired |
| P1 | Pipeline rules system | 3-5 days | Core differentiator | Plugin system |
| P2 | Agent tool-creation bootstrapping | 2-3 days | Self-improving capability | Plugin system (for storage) |
| P2 | Feedback-to-config pipeline | 3-5 days | Conversational config | Pipeline rules + optimizer |
| P3 | Multi-agent coordination | 5-10 days | Background tasks | Task queue infra |
| P3 | Scheduled tasks (jobs.md + ext) | 2-3 days | Automation | External scheduler |
| P4 | Persistent config via git push | 1-2 days | Backup + sync | Plugin system |

---

## Architecture Notes (Addendum)

- **Plugin files over DB rows for behavior config.** DB is for data (sessions, interactions, attachments). Plugins are for behavior (tools, rules, preferences). Separation keeps the DB schema stable and the behavior config version-controlled.
- **The optimizer loop is the seed for multi-agent.** It runs as an async task after each turn. Start with review/suggest capabilities. Extend to task spawning. Extend to full hierarchical agents.
- **The event stream is the wiring for the loop view.** Every event already has a type, level, and payload. Adding an `event_id` and a feedback endpoint makes it bidirectional.
- **Don't build what exists.** Google Scheduler for cron, git for versioning, pip for dependencies. The agent writes config files; external tools handle execution.
- **Validation before mutation.** Every plugin write should be syntax-checked before commit. Every tool creation should be test-run before saving. Graceful fallback to last working version on failure.
- **Model reliability is the risk.** The agent correctly translating "stop searching the web first" into a structured rule depends on the LLM being good at meta-cognition. Build validation layers, not trust.

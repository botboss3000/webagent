# webAgent — Project Roadmap

Single source of truth for what's being built, why, and in what order. Organized into projects (execution spine). Each phase folds in rationale + reference architecture from prior research. Competitor comparison tables in appendix.

---

## Project A: Agent Loop Reliability
**Theme:** Make the core agent loop production-ready — fast, resilient, streaming-first.

**Dependencies:** None (starts immediately)

**Milestones:**

| Phase | Tasks | Outcome |
|-------|-------|---------|
| A1 | Parallel tool execution — run independent tool calls concurrently via `asyncio.gather` | 3x speedup on multi-tool turns |
| A2 | Streaming as default path — SSE becomes primary, buffered mode deprecated | First token <1s perceived latency |
| A3 | Retry & self-correction — invalid tool names, malformed JSON, empty responses, tool errors all retry with feedback | Sessions survive bad LLM outputs |
| A4 | System prompt caching — cache per user+session, invalidate on `updated_at` change | 50-200ms shaved off every request |
| A5 | Iteration budget — track total LLM calls across main + sub-agents, graceful stop at limit | Predictable cost per session |
| A6 | Configurable turn gate — settings for threshold, extension amount, auto-extend mode | UX flexibility for batch vs interactive |

**Phase rationale:**

- **A1 (Parallel tools).** Today sequential — one tool call at a time, agent waits, then re-calls LLM. Need: concurrent execution of independent calls, collect results before next LLM turn. Why: ~3x faster for multi-tool tasks (read 3 files, search 2 DBs, hit 2 APIs). Reference: pi uses `Promise.all()`, Hermes has parallel + sequential modes configurable per tool.
- **A2 (Streaming default).** Today: simple loop blocks until full response, returns JSON. Streaming generator exists but isn't primary path. Need: streaming as default, emit tokens as they arrive. Why: perceived latency drops from 10-30s wait to <1s first token. Reference: Hermes uses streaming delta callbacks; pi's `EventStream` emits `message_start/message_delta/message_end`.
- **A3 (Retry).** Today: tool call fails → log → return error → agent stops. Need: three-tier retry — invalid tool names fed back to model for self-correction, malformed JSON silently re-calls LLM, empty responses retry up to 3 times, tool exec errors retry with backoff. Post-tool-empty nudge to continue. Why: models hallucinate tool names + malform JSON frequently. Reference: Hermes has 5 retry counters (invalid tool, invalid JSON, empty content, incomplete scratchpad, thinking prefill) + post-tool-empty nudge.
- **A4 (System prompt cache).** Today: rebuilt from DB every request — queries context docs, memory, brain results, concatenates. Need: cache per user+session, invalidate on `updated_at` change (TTL or event-driven). Why: DB query + render + concat adds 50-200ms per request when context unchanged. Reference: Hermes caches per-AIAgent session, invalidated on context compression.
- **A5 (Iteration budget).** Today: hardcoded max turns, no subagent awareness — sub-agent turns unbounded, share same counter. Need: Budget class tracking total LLM calls across main + spawned subagents, per-turn reset, graceful stop on exhaustion. Why: rogue subagent burns cost; budget enables predictable cost-per-turn. Reference: Hermes `IterationBudget` per-call check; pi `shouldStopAfterTurn` callback.
- **A6 (Turn gate).** Today: hardcoded 10 turns, hardcoded keyword matching. Need: configurable via settings — enable/disable, threshold, extension amount, max turns, custom prompt + keywords. Modes: disabled, once, every N, auto-extend. Why: batch should never prompt; interactive might every 10.

**Key files touched:**
- `app/agent/loop.py` — rewrite streaming as primary path
- `app/agent/llm.py` — retry logic
- `app/agent/tool_executor.py` — parallel execution
- `app/db/interface.py` — caching layer

**Complexity:** Moderate. Mostly refactor existing code, not new architecture.

---

## Project B: Plugin System & Self-Configuration
**Theme:** The core differentiator — agent bootstraps its own tools, rules, and behavior through conversation. Everything is git-versioned and persistent.

**Dependency:** None (starts immediately, independent of Project A)

**Milestones:**

| Phase | Tasks | Outcome |
|-------|-------|---------|
| B1 | Plugin directory structure + loader — `plugins/vanilla/`, `plugins/user/`, `plugins/admin/` with import order | Agent behavior lives in Python files, not just DB rows |
| B2 | Git auto-commit on plugin writes — `write_source`/`edit_source` triggers `git commit` with auto-generated message | Every change is tracked, rollback possible |
| B3 | Agent tool-creation validation — `ast.parse()` + sandboxed `exec()` test run before saving | Tools can't break the runtime |
| B4 | Pipeline rules system — `pipeline_rules` table, loaded into system prompt as soft constraints | User says "don't X before Y" → rule persists |
| B5 | Feedback-to-config pipeline — user feedback → optimizer interprets → writes plugin file → commits → reloads | The agent permanently learns from corrections |
| B6 | Tool marketplace — export/import JSON, community repo (`webagent-tools/` on GitHub) | Network effects, compounding tool library |
| B7 | Agent-suggested tools — optimizer detects repeated patterns, offers to create a tool | Proactive self-improvement |
| B8 | Plugin forking — `git branch` per configuration, switchable at runtime | Experimentation without risk |

**Phase rationale:**

- **B1 (Plugin system).** Today: no plugins, tools + config are DB rows. Need: `plugins/vanilla/` (ships read-only), `plugins/user/` (conversation mods), `plugins/admin/` (admin-installed). `plugin_loader.py` scans at startup, imports in order vanilla → admin → user. Plugins can override functions or register tools. Graceful failure: syntax error → fall back to last working version, log + tell user. Why: separates behavior (plugins, version-controlled) from data (DB, stable schema).
- **B2 (Git auto-commit).** Today: DB changes persist but no history. Need: every plugin edit → auto `git commit` with generated message. Forking = `git checkout -b`, switching = `git checkout` + reload, push = backup to private repo. Pre-commit validation: `python -c "compile(open(path).read())"`. Why: rollback, experimentation branches, multi-config ("fast mode" vs "thorough researcher"), backup, transparency via `git log`.
- **B3 (Tool-creation validation).** Today: `create_tool` works at basic level — JSON Schema + Python, stored in DB, callable next turn. Missing: no UI, no validation, no versioning. Need: `ast.parse()` + sandboxed `exec()` test run before save. Reject broken tools with helpful message. Versioning per edit, rollback. Tool dependencies (`pip install`) declared + auto-installed.
- **B4 (Pipeline rules).** Today: doesn't exist; optimizer is proto. Need: `pipeline_rules` table or memory pages (user_id, priority, trigger, action, condition, source, enabled). Loaded into system prompt at startup or via `before_agent_start` hook. User feedback → interpret → write rule → next turn uses it. Soft constraints, not hardcoded DAG — agent can override with reasoning. Open: rule conflict resolution, accumulation decay, DB vs `plugins/` storage.
- **B5 (Feedback-to-config).** Today: feedback verbal only, lost on refresh. Need: user says "you're too slow, stop searching web every time" → structured feedback event → optimizer processes pattern → writes `plugins/user/preferences.py` → commits → next turn applies. Incremental + reversible. Optimizer proactively suggests changes after repeated corrections.
- **B6 (Tool marketplace).** Browse UI: list all created tools, search, inspect parameters, enable/disable. Export/import JSON. Community repo on GitHub.
- **B7 (Agent-suggested tools).** Optimizer detects repeated pattern → "I notice you keep asking for stock prices. Create `get_stock_price` tool?"
- **B8 (Plugin forking).** `main` = vanilla stable; `experimental` = agent tries new; `user-<name>` = per-user personality. Switch = `git checkout` + reload.

**Key files created:**
- `app/plugin_loader.py` — directory scanner + importer
- `app/agent/pipeline_rules.py` — rule storage, loading, validation
- `app/agent/feedback_handler.py` — user feedback → optimizer wiring
- `plugins/vanilla/` — default tools as plugin files

**Key files touched:**
- `app/admin/source_tools.py` — add git commit hook after write/edit
- `app/agent/prompts.py` — inject pipeline rules into system prompt
- `app/agent/loop.py` — add feedback event type

**Complexity:** High. New architecture, but the primitives exist (edit_source, run_command, optimizer loop).

**Risk:** Model reliability — correctly translating "stop searching the web first" into a structured rule depends on LLM meta-cognition. Build validation layers, not trust.

---

## Project C: Multi-Provider & Service Resilience
**Theme:** Never be down. Fallback providers, credential rotation, provider-agnostic message format.

**Dependency:** Project A (shares the LLM call path — best done after streaming/retry is stable)

**Milestones:**

| Phase | Tasks | Outcome |
|-------|-------|---------|
| C1 | LLM provider message adapters — `interactions` → any provider format (OpenAI, Anthropic, Gemini) | Drop-in support for any LLM backend |
| C2 | Credential rotation — re-resolve API key before each request, detect env var changes | Zero-downtime key rotation |
| C3 | Fallback model chain — ordered list of providers, per-turn fallback on error | Agent keeps running when primary is down |
| C4 | Multi-provider racing — fan out to N providers, use fastest (already prototyped) | Lowest latency across providers |

**Phase rationale:**

- **C1 (Adapters).** Today: session turns persisted as `interactions` rows (`user`/`assistant`/`tool`). Live loop calls OpenRouter via OpenAI-compatible Chat Completions — in-memory `messages` follows OpenAI shape. Need: treat DB transcript as canonical, add adapter layer mapping `interactions` → OpenAI messages (current path), leave room for adapters when provider isn't OpenAI-compatible (native Anthropic Messages, Gemini, enterprise gateways). Implementation: keep `interactions` as single source of truth, implement `interactions_to_openai_messages(...)`, add `interactions_to_<provider>_...(...)` behind `MessageAdapter` interface + registry keyed by `provider`/`api_style`. Unit-test each adapter: seeded rows → expected payload; tool turns preserve `tool_calls` ↔ `tool` pairing. Why: SMS/WhatsApp + other channels need DB + correct adapter, not "whatever browser sent last refresh."
- **C2 (Credential rotation).** Today: API key loaded once from env at module level, never refreshed — expiry/revocation mid-session crashes. Need: re-resolve before each request (or each LLM call). Support env var changes, rotation providers, token refresh. Reference: Hermes `_ensure_runtime_credentials()` each turn, invalidates cached agent on change.
- **C3 (Fallback chain).** Today: single model via env var, provider down = agent fails. Need: ordered chain (OpenRouter → OpenAI Codex → Anthropic). Triggers: auth fail, rate-limit, connection error, empty responses after retry exhaustion. Per-turn (next turn tries primary again). Reference: Hermes has init-time fallback (no primary key → fallback at startup), per-turn credential fallback (auth error → walk chain), runtime fallback (consistent empty → switch mid-turn).
- **C4 (Racing).** Already prototyped. Fan out to N providers, use fastest response.

**Key files created:**
- `app/agent/message_adapters/` — one file per provider format
- `app/agent/credential_resolver.py` — re-read env vars each turn

**Key files touched:**
- `app/agent/loop.py` — integrate adapter + fallback into LLM call path
- `app/db/interactions.py` — ensure canonical format supports all adapters

**Complexity:** Low-Moderate. Mostly new files, isolated from rest of codebase.

---

## Project D: Multi-Agent & Scheduled Tasks
**Theme:** The agent is not alone. Sub-agents for background work, scheduled jobs for automation.

**Dependency:** Project B (plugin system stores multi-agent config). Best after B1-B2.

**Milestones:**

| Phase | Tasks | Outcome |
|-------|-------|---------|
| D1 | Agent registry — track alive agents, their tasks, status | Visibility into who's doing what |
| D2 | Sub-agent spawning — main agent spawns a child with its own session, tools, memory | "Scrape these URLs while I chat" |
| D3 | Task queue + result channel — child writes results to a shared location, parent picks up | Async background work |
| D4 | jobs.md format spec + scheduler endpoint — `POST /api/v1/scheduler/tick` | External cron triggers agent tasks |
| D5 | Agent tools for jobs.md — read, write, update, delete scheduled jobs | Agent manages its own schedule |
| D6 | Hierarchical budget — iteration budget tracks across agent tree | Cost control at scale |

**Phase rationale:**

- **D1-D3 (Multi-agent).** Today: optimizer loop runs as secondary async task after each chat turn — proto, one sub-agent reviewing main agent's work. Phases: (1) current optimizer — single sub-agent reviews + suggests; (2) near — agent spawns parallel worker for independent work (e.g. "scrape these URLs while I continue chatting"); (3) future — full agent registry, inter-agent messaging, hierarchical task decomposition. Architecture: registry (who's alive, what doing), task queue, result channel (child → parent), isolation (child shouldn't corrupt parent context), budget tracking across tree.
- **D4-D5 (Scheduled tasks).** Today: doesn't exist. Approach: not building internal job queue. Agent writes `jobs.md` describing scheduled tasks in structured markdown. External scheduler (Google Cloud Scheduler, VM cron) reads periodically, triggers agent via webhook/API with job context. Agent executes, logs result. Why: avoid building infra (queues, workers, retries, scaling). Leverage battle-tested schedulers. Agent's job = manage config, not run scheduler. Matches pi philosophy: don't build it, use what exists. Components: `jobs.md` spec (cron expr, description, params, expected output), tools to read/write/update, `POST /api/v1/scheduler/tick` endpoint, execution loop (load → run → log → update status), error/retry handling (or delegate to scheduler).
- **D6 (Hierarchical budget).** Extension of A5 — budget propagates across spawned agent tree.

**Key files created:**
- `app/agent/agent_registry.py` — in-memory registry of all agents
- `app/agent/sub_agent.py` — sub-agent lifecycle (spawn, run, collect)
- `app/agent/scheduler.py` — jobs.md parser + scheduler endpoint
- `jobs.md` (in project root) — example scheduled task file

**Key files touched:**
- `app/main.py` — mount scheduler route
- `app/agent/loop.py` — integrate sub-agent spawn/kill

**Complexity:** High. New runtime architecture. Sub-agent isolation is hard.

---

## Project E: UX & Onboarding
**Theme:** People need to understand webAgent in 5 minutes and deploy it in 10.

**Dependency:** None (can run in parallel with everything)

**Milestones:**

| Phase | Tasks | Outcome |
|-------|-------|---------|
| E1 | Welcome message — first-time user sees what the agent can do with example prompts | Zero-guess-start |
| E2 | Pre-loaded demo tools — 3-5 example tools active on fresh install | Immediate utility |
| E3 | Sample `context_templates` — seeded on first chat, gives the agent personality + guidelines | Agent responds well out of the box |
| E4 | Quick start tutorial — "Install → send first message → create first tool" in README | Documentation lowers barrier |
| E5 | Example workflows — 3 documented use cases (website monitor, news summarizer, todo manager) | Users see what's possible |
| E6 | Docker compose + $5 VPS deploy guide | One-command deployment |
| E7 | 30-second demo video — "I told it to stop searching the web and it never did it again" | Shareable moment, viral potential |
| E8 | One-line pitch in README — "An AI agent that builds its own tools and remembers how you like to work" | Clear identity |

**Key files touched:**
- `ui/index.html` — welcome message + suggested prompts
- `app/context/context_templates/` — seed files
- `README.md` — rewrite with pitch, tutorial, workflows
- `Dockerfile` — production-ready
- `docker-compose.yml` — new file

**Complexity:** Low. All content and config, no new runtime code.

---

## Project F: Loop View as Configuration Canvas
**Theme:** The loop view goes from read-only firehose to bidirectional control panel.

**Dependency:** Project B (pipeline rules exist to display + edit). Best after B4.

**Milestones:**

| Phase | Tasks | Outcome |
|-------|-------|---------|
| F1 | Event ID system — every `tool_call`/`tool_result` event gets a unique ID | UI can reference specific events |
| F2 | Feedback endpoint — `POST /api/v1/pipeline/feedback` (event_id + natural language) | UI sends user modifications back |
| F3 | Clickable event nodes — click a tool call → "Change this", "Always after X", "Skip next time" | Pipeline is interactive |
| F4 | Rule visualization — saved pipeline rules listed alongside live events | User sees what's active |
| F5 | Inline editor — right-click tool call → edit parameters inline | Quick tweaks without leaving the view |

**Phase rationale:**

Today: loop view shows `tool_call`, `tool_result`, `pipeline`, `db` events as scrollable log — read-only firehose. Vision: each event node clickable for feedback. Right-click → "Change this tool's behavior" (inline editor for params), "Always do this after X" (creates pipeline rule), "Skip next time" (temporarily suppresses tool). Drag to reorder → agent interprets new sequence as hint. Flow: user clicks → popup with natural language prompt → agent receives feedback as structured event → interprets → writes/updates plugin file → git commit → reload → next turn uses new config.

Architecture needed: each event needs `event_id` for UI reference; UI needs feedback channel (`POST /api/v1/pipeline/feedback`); agent loop needs `feedback` event handler routing user modifications to optimizer.

**Key files created:**
- `app/api/pipeline.py` — feedback endpoint
- `ui/js/pipelineCanvas.js` — interactive event rendering

**Key files touched:**
- `app/agent/loop.py` — add event_id to all emitted events
- `ui/js/loop.js` — rewrite from scrolling log to interactive tree

**Complexity:** Moderate. Combines backend (event IDs, feedback endpoint) and frontend (interactive canvas).

---

## Project G: Developer UX
**Theme:** Quality-of-life features for people who use webAgent for technical work.

**Dependency:** Project A (reliable agent loop). Lower priority than all above.

**Milestones:**

| Phase | Tasks | Outcome |
|-------|-------|---------|
| G1 | Context references — `@file:path`, `@web:URL`, `@git:log` expand into full content | Less copy-paste |
| G2 | Image handling — accept base64/URL images, route to vision model or fallback analysis | Visual context works |
| G3 | Context length awareness — references respect model context window | Don't blow the prompt |

**Phase rationale:**

- **G1 (Context refs).** Today: nothing — paste file contents manually. Need: expand `@file:path/to/file.py` → read file, inject into message. Same for `@diff` (git diff), `@folder:src/` (list/read all), `@git:log`, `@web:URL` (fetch URL). Respect context-length limits. Why: massive friction reduction — "fix bug in @file:main.py" instead of open/copy/paste. Reference: Hermes `agent/context_references.py` supports file, diff, folder, git, web, glob with context-length awareness.
- **G2 (Image handling).** Today: no image support, text only. Need: accept images (base64 or URL) in chat requests. Route to native vision API if model supports, else fall back to LLM-based vision analysis. Why: many tasks need visual context — screenshots, diagrams, UI mockups, error screenshots. Reference: Hermes has two-tier decision chain: native passthrough for vision-capable models, LLM-based `vision_analyze` description generator for non-vision.
- **G3 (Context length awareness).** Refs respect model window — don't blow the prompt.

**Key files created:**
- `app/agent/context_references.py` — parser + resolver

**Key files touched:**
- `app/api/chat.py` — accept image attachments
- `ui/js/chat.js` — @-completion for file paths

**Complexity:** Moderate. Image handling is frontend+backend, context references is parsing.

---

## Project Timeline (Recommended Order)

```
Week 1-2     Week 3-4     Week 5-6     Week 7-8     Week 9-10    Week 11-12
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A1-A3:        A4-A6:       B5-B6:       D1-D3:       D4-D6:       C2-C4:
Loop          Polish       Feedback     Multi-       Scheduled    Credential
reliability   + cache      pipeline     agent        tasks        + fallback
                             
B1-B3:        B4:          B7-B8:       F1-F3:       F4-F5:       G1-G3:
Plugin        Pipeline     Tool         Event IDs    Rule viz     Developer
system        rules        suggestions  + feedback   + inline     UX
                             
E1-E5:        E6-E8:                                C1:
Onboarding    Deploy                                Provider
              guide                                 adapters
              + demo
```

## Parallel Tracks

| Track | Projects | Who |
|-------|----------|-----|
| **Backend core** | A → C → G | Agent loop, providers, developer UX |
| **Self-configuration** | B → F | Plugins, rules, feedback, loop canvas |
| **Multi-agent** | D | Sub-agents, scheduled tasks |
| **Go-to-market** | E | Onboarding, docs, deploy, demo |

Tracks are independent. Solo dev: sequential (backend core → self-configuration → multi-agent → GTM). Team: parallelize.

## Critical Path

Highest-risk, highest-reward in order:

1. **Plugin system (B1-B3)** — unlocks everything else. Without this, config lives in DB with no versioning.
2. **Feedback-to-config pipeline (B5)** — the core differentiator. Without this, agent doesn't learn.
3. **Loop view as canvas (F)** — the UI that makes #1 and #2 visible and controllable.
4. **Onboarding (E)** — none of this matters if nobody gets past first 5 minutes.
5. **Multi-agent (D)** — valuable but can wait until single-agent experience is solid.

## What Success Looks Like

**Month 1:** Agent loop is fast and resilient. Plugins work. First feedback-to-config demo works end-to-end.

**Month 2:** Loop view is interactive. Users can deploy on $5 VPS in 10 minutes. First community tool shared.

**Month 3:** Multi-agent ships. Scheduled tasks work. The 30-second demo video exists. People start using webAgent for real workflows.

**Month 6:** webAgent has 50+ community tools, 1k+ GitHub stars, and clear identity as "the agent that gets better the more you use it."

---

## Architecture Notes

- **Plugin files over DB rows for behavior config.** DB is for data (sessions, interactions, attachments). Plugins are for behavior (tools, rules, preferences). Separation keeps DB schema stable and behavior version-controlled.
- **Optimizer loop is the seed for multi-agent.** Runs async after each turn. Start with review/suggest. Extend to task spawning. Extend to full hierarchical agents.
- **Event stream is the wiring for the loop view.** Every event has type, level, payload. Add `event_id` + feedback endpoint → bidirectional.
- **Don't build what exists.** Google Scheduler for cron, git for versioning, pip for dependencies. Agent writes config files; external tools handle execution.
- **Validation before mutation.** Every plugin write syntax-checked before commit. Every tool creation test-run before save. Graceful fallback to last working version on failure.
- **Model reliability is the risk.** Agent translating natural feedback into structured rules depends on LLM meta-cognition. Build validation layers, not trust.
- **All new agent-loop features live in `app/agent/` as isolated modules** (one file per feature). Streaming loop becomes single entry point — simple loop deprecates.
- **Feature flags via user settings in DB.** Individual users opt in/out.
- **Retry counters, fallback state, iteration budget per-request** (not global) — avoid cross-user state leaks.
- **Tool execution errors include structured info** (error type, recoverable bool, hint string) so frontend can render appropriate UI.

---

## Appendix A: pi vs webAgent — Tools Side-by-Side

| Tool | webAgent | pi | Advantage |
|------|----------|-----|-----------|
| read file | `read_source(path)` | `read(path, offset, limit)` | pi: offset/limit for large files, reads images (jpg/png/gif), auto-truncates |
| write file | `write_source(path, content)` | `write(path, content)` | pi creates parent dirs automatically |
| edit file | `edit_source(path, old, new)` — single replacement, `str.replace(old, new, 1)` | `edit(path, edits[])` — multiple disjoint edits in one call | pi validates non-overlapping edits, merges nearby changes |
| delete file | `delete_source(path, rec)` | ❌ done via `bash rm` | webAgent — dedicated tool |
| shell | `run_command(cmd, timeout)` | `bash(cmd, timeout)` | pi runs in agent's working dir, auto-truncates output — same capability |
| search | ❌ | `grep(pattern)` — ripgrep-powered | pi-only — fast codebase search |
| find files | ❌ | `find(pattern)` — fd-powered | pi-only — file discovery by name |
| list dir | ❌ | `ls(path)` | pi-only |
| browser | `browser_action(...)` — 11 actions, Playwright | ❌ (OpenClaw adds one) | webAgent — full headless browser built-in |
| screenshot | `take_screenshot(monitor, region)` | ❌ | webAgent-only |
| restart | `restart_server()` | ❌ | webAgent-only |

## Appendix B: pi Session/UX Additions webAgent Lacks

| Feature | What it does |
|---------|--------------|
| Session tree | Branch at any message, fork, explore alternatives. webAgent is linear. |
| Compaction | Auto-summarizes long conversations to stay under context limits. webAgent caps at 10-20 turns. |
| Model cycling | Ctrl+P to switch models mid-session (fast/cheap for simple, smart for hard). |
| Thinking display | See model's reasoning chain in real time. |
| TUI editor | Message editing, history navigation, keyboard shortcuts. |
| Slash commands | `/compact`, `/model`, `/resume`, `/handoff` — built-in workflow control. |
| Skills system | SKILL.md files load on-demand into system prompt. |
| Extensions | TypeScript plugins intercept tool calls, add permission gates, inject context. |
| Prompt templates | `/mycommand` expands to full prompt content. |
| Auto-retry | Configurable retry on LLM errors. |
| Context files | AGENTS.md, CLAUDE.md auto-loaded into system prompt. |

## Appendix C: Guardrails — pi vs webAgent

### pi has, webAgent doesn't

| # | Guardrail | What it does | How pi does it |
|---|-----------|--------------|----------------|
| 1 | Tool call interception | Extension can inspect, log, block, or modify any tool call before execution | `pi.on("tool_call", async (event, ctx) => { return { block: true }; })` |
| 2 | Dangerous bash confirmation | Prompts user before `rm -rf`, `sudo`, `chmod 777` | Extension checks `event.input.command` against regex, calls `ctx.ui.confirm()` |
| 3 | Write/edit path protection | Blocks writes to `.env`, `.git/`, `node_modules/` | Extension checks `event.input.path`, returns `{ block: true }` |
| 4 | Read path protection | Blocks reads of `.env`, secrets, credentials, `~/.ssh`, `~/.aws`, `~/.gnupg` | Override `read` tool, reject matches |
| 5 | Session action confirmation | Confirms before clearing/switching/forking sessions | `pi.on("session_before_switch")`, `pi.on("session_before_fork")` |
| 6 | OS-level sandbox | Filesystem + network isolation via bubblewrap (Linux) or sandbox-exec (macOS) | `@anthropic-ai/sandbox-runtime` extension |
| 7 | Git checkpointing | Auto-stashes changes at each turn for undo | Extension hooks `turn_end` |
| 8 | Dirty repo guard | Prevents session switch with uncommitted changes | Extension checks `git status` before `session_before_switch` |
| 9 | Tool override | Replace built-in tools with audited/restricted versions | `pi.registerTool({ name: "read", ... })` — same name overrides built-in |
| 10 | Access logging | Logs every file read to disk for audit trail | Override `read` → `appendFile(logFile, entry)` |
| 11 | Output truncation | Auto-truncates all output at 50KB / 2000 lines | Built into every pi tool |
| 12 | Tool loop detection | Detects repeated non-progressing tool calls | OpenClaw's `tool-loop-detection.ts` |
| 13 | Abort signals | User can cancel any running tool mid-execution | `AbortSignal` passed to every tool's `execute()` |
| 14 | File mutation queue | Batches writes for undo safety, prevents races | `withFileMutationQueue()` wrapper around all writes |
| 15 | Bash cwd check | Verifies cwd exists before spawning shell | `existsSync(cwd)` in bash tool |
| 16 | Headless safety | Auto-blocks dangerous ops in non-interactive mode | `if (!ctx.hasUI) return { block: true }` |
| 17 | Auto-retry | Configurable LLM error retry with backoff | Built into pi's agent loop |
| 18 | Path traversal prevention | Resolves paths relative to workspace, normalizes `..` | `resolveToCwd()` in all file tools |

### webAgent has, pi doesn't

| # | Guardrail | What it does |
|---|-----------|--------------|
| 1 | System prompt confirmation rules | `[CRITICAL RULE]` in every prompt: model must ask user before destructive tools. Only defense for `edit_source`/`write_source`/`delete_source`/`run_command`/`restart_server` |
| 2 | Syntax validation on write | `ast.parse()` checks `.py` files, `json.loads()` checks `.json` before saving |
| 3 | Backups on write | Auto-creates `.source-backups/<file>.<timestamp>.bak` before every overwrite |
| 4 | Turn limit | Hard cap at 10-20 turns per conversation |
| 5 | localhost only | WebSocket endpoints reject non-loopback connections |

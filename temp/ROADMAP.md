# webAgent — Project Roadmap

Organized into high-level projects. Each project is a self-contained chunk of work that delivers value independently. Projects can run in parallel where dependencies allow.

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

**Key files touched:**
- `app/agent/loop.py` — rewrite streaming as primary path
- `app/agent/llm.py` — retry logic
- `app/agent/tool_executor.py` — parallel execution
- `app/db/interface.py` — caching layer

**Complexity:** Moderate. Most work is refactoring existing code, not new architecture.

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

Tracks are independent. A solo developer can work tracks sequentially (backend core → self-configuration → multi-agent → GTM). A team can parallelize.

## Critical Path

The **highest-risk, highest-reward** items in order:

1. **Plugin system (B1-B3)** — unlocks everything else. Without this, config lives in the DB with no versioning.
2. **Feedback-to-config pipeline (B5)** — the core differentiator. Without this, the agent doesn't learn.
3. **Loop view as canvas (F)** — the UI that makes #1 and #2 visible and controllable.
4. **Onboarding (E)** — none of this matters if nobody gets past the first 5 minutes.
5. **Multi-agent (D)** — valuable but can wait until the single-agent experience is solid.

## What Success Looks Like

**Month 1:** Agent loop is fast and resilient. Plugins work. First feedback-to-config demo works end-to-end.

**Month 2:** Loop view is interactive. Users can deploy on a $5 VPS in 10 minutes. First community tool shared.

**Month 3:** Multi-agent ships. Scheduled tasks work. The 30-second demo video exists. People start using webAgent for real workflows.

**Month 6:** webAgent has a 50+ community tools, 1k+ GitHub stars, and a clear identity as "the agent that gets better the more you use it."

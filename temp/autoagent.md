# AutoAgent — Self-Modifying Software via LLM Tool Loop

**Observation:** The webAgent already has all the pieces to rewrite its own code at runtime. This document explores how that works, what it enables, and where it breaks.

## The Tool Stack

| Tool | What it gives the agent |
|------|------------------------|
| `read_source` | Read any file — the agent can inspect its own source |
| `write_source` | Create or overwrite files (backed up to `.source-backups/`) |
| `edit_source` | Find-and-replace in files — Python, HTML, CSS, JS, configs |
| `delete_source` | Remove files/directories |
| `run_command` | Shell access — git, pip, npm, anything |
| `restart_server` | Kill + restart uvicorn — reloads all modified `.py` files |
| `db_query` | Read/write the database — including context documents and tool definitions |
| `create_tool` | Register new agent-callable tools at runtime |
| `list_tools` / `search_tools` | Discover what tools exist (agent doesn't know its own full toolset ahead of time) |

## The Self-Modification Loop

```
User sends message
  │
  ▼
Agent loop starts
  │
  ├─ 1. Build system prompt
  │     └─ Reads from: agents.system_prompt (DB) + context rows (DB) + system_prompt.md (file)
  │
  ├─ 2. LLM reasons about the user's request
  │     └─ "They want a purple theme. I can edit app1.css."
  │
  ├─ 3. Agent calls tools
  │     ├─ read_source("ui/app1.css")        → inspects current CSS
  │     ├─ edit_source("ui/app1.css", ...)    → changes colors
  │     └─ restart_server()                   → bounces process
  │
  ├─ 4. Server restarts (~2 seconds)
  │     └─ Modified CSS is now live
  │
  └─ 5. Next user message hits the mutated app
```

## What the Agent Can Mutate

### Backend (Python)
- `app/agent/loop.py` — agent execution loop (could change turn limits, tool validation, streaming behavior)
- `app/agent/prompts.py` — how the system prompt is built
- `app/api/*.py` — add/remove/modify API endpoints
- `app/tools/` — inject new core tools, modify existing tool logic
- `app/db/` — change storage backend behavior
- `app/admin/guardrails.py` — the agent could remove its own guardrails

### Frontend (static files)
- `ui/index.html` — full page layout
- `ui/app1.css`, `app2.css`, `app3.css`, `loop.css` — complete style system
- `ui/js/*.js` — all client-side behavior (chat, streaming, tabs, terminal, etc.)

### Self (prompt/identity layer)
- `app/db/system_prompt.md` — instructional fragments loaded into every system prompt (cached; `restart_server` clears cache)
- `context` table in DB — agent identity, user personality, skills, tools, tasks docs
- `agents` table `system_prompt` field — the non-editable directive (the agent can `db_query` to UPDATE it)
- `tools` table in DB — tool definitions loaded on demand via `list_tools`

## The System Prompt That Triggers It

The system prompt is assembled from four layers:

```
1. [AGENT DIRECTIVE]     ← agents.system_prompt DB field (db_query-able)
2. [AGENT IDENTITY]      ← context rows with context_type="agent"
3. [SKILLS]              ← context rows with context_type="skills"
4. system_prompt.md      ← file on disk (edit_source-able)
```

A system prompt like this would set the loop in motion:

> *"You are AutoAgent. You may modify any file in this project to customize the experience for each user. Read the user's preferences from context, then edit source code and restart the server to apply changes. You may create new API endpoints, modify the UI, add new tools, and change your own system prompt. All changes persist on disk."*

On the next turn, the agent has the modified system prompt and can reason about how to modify itself further. **The prompt bootstraps the behavior; the behavior rewrites the prompt.**

## Persistence Reality

| Hosting | Changes survive restart? | Notes |
|---------|-------------------------|-------|
| Local dev (`uvicorn --reload`) | ✅ Yes | Files on disk; reload picks them up |
| VPS / bare metal | ✅ Yes | Same as local |
| Cloud Run | ❌ No | Container filesystem is ephemeral — changes lost on instance recycle |
| Cloud Run + git push | ✅ Yes | Agent could `run_command("git commit && git push")` to trigger Cloud Build → redeploy |

## Example Mutation Chains

### Full UI rebrand per user
```
read_source("ui/app1.css")
→ edit CSS variables (colors, fonts, spacing)
→ edit_source("ui/index.html", add user's name to header)
→ edit_source("ui/js/main.js", change greeting logic)
→ restart_server()
→ User sees personalized UI on next load
```

### Add a new API feature
```
read_source("app/api/chat.py")
→ write_source("app/api/custom_endpoint.py", new FastAPI router)
→ edit_source("app/main.py", add app.include_router() line)
→ edit_source("ui/index.html", add button that calls new endpoint)
→ edit_source("ui/js/main.js", add click handler)
→ restart_server()
→ New feature is live
```

### Self-evolution of agent identity
```
read_source("app/db/system_prompt.md")
→ edit: add new skill description
→ db_query("UPDATE context SET content = ? WHERE context_type = 'agent'", new_identity)
→ run_command("pip install new-package")     # add dependencies
→ edit_source("app/tools/core_tools.py", add new tool handler)
→ restart_server()
→ Agent now has new capabilities and a new identity
```

## Security Notes

- Mutating tools (`write`, `edit`, `delete`, `run`, `restart`) **require user confirmation** before the agent can call them. This is enforced in `source_tools.py`.
- `guardrails.py` blocks access to `.env`, `.ssh/*`, and dangerous commands like `rm -rf /`. The agent could edit or delete `guardrails.py` to bypass this — a [alignment risk](https://en.wikipedia.org/wiki/Instrumental_convergence).
- Deleting `app/admin/` directory removes all filesystem tools. The agent could restore it via `write_source`, but it can't call `write_source` if the tool doesn't exist — chicken-and-egg problem that acts as a kill switch.

## Custom Workflow Patterns (AutoGen-Style)

The self-modification loop isn't limited to UI tweaks. The agent can rewrite **its own execution loop** (`app/agent/loop.py`) to implement multi-agent workflows — AutoGen-style — where the LLM coordinates sub-agents instead of just calling tools.

### Default: Single-Agent Loop

```
User message
  → System prompt
  → LLM call (think + tool calls)
  → Execute tools
  → LLM call again
  → Final response
```

### After Mutation: Multi-Agent Workflow

The agent rewrites `app/agent/loop.py` to implement a custom pipeline:

```
User message
  → Orchestrator Agent
      │
      ├── Planner Agent
      │     System prompt: "Design an approach. Output a plan."
      │     Calls: db_query (user context), web_search (research)
      │     Output: JSON plan
      │
      ├── Reviewer Agent
      │     System prompt: "Check the plan for errors and gaps."
      │     Calls: read_source (check existing code), db_query (history)
      │     Output: critique + changes
      │
      ├── Executor Agent
      │     System prompt: "Implement the approved plan."
      │     Calls: edit_source, write_source, run_command, restart_server
      │     Output: completion report
      │
      └── Orchestrator compiles final response
```

### How the agent builds it

Since the agent can `read_source` + `write_source` + `restart_server`, it can:

1. Read `app/agent/loop.py` → understand current loop structure
2. Write a new loop that calls the LLM multiple times per turn, each with a different system prompt for a different "agent" role
3. Each sub-agent call is just: `system_prompt_of_role + history → LLM → tool_calls` — the same `openai_compat.py` client, same tool pool
4. Orchestrator passes state between sub-agents via Python variables or DB rows
5. `restart_server` → new loop is live

### User defines the workflow

The user describes the pattern in natural language:

> *"I want a research pipeline. You plan what to search, verify each source, then write a summary."*

The agent writes `loop.py` with three sub-agent phases. This is the user's *exact flow*, not a pre-built template.

### Code is lightweight

It doesn't take much code to add a sub-agent phase:

```python
# Inside the turn loop — one "agent" is just a system prompt + LLM call
plan = await call_llm(
    system="You are a planner. Output a JSON plan.",
    messages=history,
    tools=allowed_tools,
)
review = await call_llm(
    system="You are a reviewer. Validate the plan.",
    messages=history + [plan],
    tools=allowed_tools,
)
result = await call_llm(
    system="You are an executor. Follow the approved plan.",
    messages=history + [plan, review],
    tools=allowed_tools,
)
```

The agent writes this exact pattern, tailored to the user's description.

## Superpower Integration (from `temp/superpower.md`)

The Superpower system is a **meta-optimizer** — background subagents that analyze real usage and auto-improve skills. Cross it with AutoAgent's workflow mutation and you get:

| Layer | What | Who builds it | Who optimizes it |
|-------|------|--------------|------------------|
| **Default loop** | Single-agent tool-calling | Shipped with webAgent | — |
| **Custom workflow** | Multi-agent pipeline (plan/review/execute) | Agent writes it via `edit_source` | Superpower tunes sub-agent prompts, turn limits, token budgets |
| **Superpower itself** | Background analyzer → proposer → validator → deployer | Agent writes it (or pre-built) | Superpower optimizes *itself* — meta-meta |

### How Superpower optimizes a workflow

1. **Analyzer** queries DB: *"Users who use the 'research' workflow average 8 turns per task and 12k tokens. High failure rate on step 2 (review)."*
2. **Proposer** generates an improved reviewer sub-agent prompt: *"Make review stricter. Require citations. Reject vague plans."*
3. **Validator** replays past interactions against the new prompt → tests if quality improves
4. **Deployer** writes the new prompt to the DB → next user turn uses optimized reviewer
5. If quality drops → **auto-rollback** to previous version

### The chain

```
User: "I want a writing workflow — draft, critique, polish."
  │
  ▼
Agent rewrites loop.py (3 sub-agents: drafter, critic, polisher)
  │
  ▼
Agent restarts_server
  │
  ▼
User uses the workflow for a week (hundreds of interactions logged)
  │
  ▼
Superpower Analyzer: "The critic is too lenient. 40% of drafts have errors."
  │
  ▼
Superpower Proposer: generates stricter critic prompt
  │
  ▼
Superpower Deployer: writes it → next turn uses improved critic
```

The user never codes. The user expresses intent. The agent builds the exact workflow. Superpower iteratively improves it.

## What This Means

The webAgent is not just a chatbot with tools. It's a **bootstrapped development environment where the product is its own source code.** The agent acts as:

1. **Developer** — reads, understands, and modifies code
2. **DevOps** — restarts servers, installs packages, runs git
3. **Product** — the modified code *is* the user experience

This is the minimal viable example of a self-modifying software system. The LLM provides the reasoning; the tool stack provides the execution; `restart_server` closes the loop.

# webAgent — Product Positioning vs The Market

## One Product, Four Deployment Models

| Deployment | Who uses it | Analogy |
|------------|------------|---------|
| **Local machine** (uvicorn) | Individual developer / power user | Like running pi, Ollama, or VS Code — yours, local, private |
| **Your own server / VPS** (Docker) | A business running their agent 24/7 | Like hosting your own N8N or Supabase instance |
| **Cloud Run / Fly.io** (serverless) | Someone who wants managed infra without managing servers | Like deploying on Railway or Render |
| **Inside a website's codebase** (git submodule) | A developer who wants the agent to co-author their site | Nothing else does this |

No other product offers all four. You pick one and the same agent works.

---

## Comparison Matrix

| Product | Self-host | Inside site codebase | You configure by... | Multi-agent | Agent modifies its own behavior | Widget for end-users | Scheduled tasks |
|---------|-----------|---------------------|--------------------|-------------|--------------------------------|---------------------|----------------|
| **webAgent** (vision) | ✅ All 4 models | ✅ Direct file editing | Talking to it | ✅ Optimizer → sub-agents | ✅ Rules, tools, plugins | ✅ Widget | ✅ Via jobs.md |
| **Dify** | ✅ Docker Compose (5 services) | ❌ External iframe only | Visual drag-and-drop | ❌ Single agent per app | ❌ Admin edits workflow | ✅ Widget | ✅ Built-in |
| **N8N** | ✅ Docker | ❌ | Visual drag-and-drop | ❌ Not an agent | ❌ | ❌ Chat not its purpose | ✅ Built-in |
| **Open Interpreter** | ✅ Local CLI | ❌ | Terminal commands | ❌ | ❌ | ❌ | ❌ |
| **Cheshire Cat** | ✅ Docker | ❌ | Admin UI + plugins | ❌ | ❌ Plugins, not self-modifying | ✅ Widget | ❌ |
| **Intercom / Zendesk AI** | ❌ Cloud only | ❌ | Settings UI | ❌ | ❌ | ✅ Widget | ❌ |
| **CustomGPT / Chatbase** | ❌ Cloud only | ❌ | Upload docs + settings | ❌ | ❌ | ✅ Widget | ❌ |
| **Pi (coding agent)** | ✅ Local CLI | ✅ Edits any file | Conversation + extensions | ✅ Via extension | ✅ Via extension | ❌ Terminal only | ❌ |

---

## The Two Moves Nobody Else Makes

### Move 1: The agent configures itself through conversation

Dify, N8N, Intercom, CustomGPT — all require a **settings UI, a visual builder, or a form**. You upload docs, drag nodes, fill in API keys, toggle options.

webAgent: you *talk*. The agent listens, builds context, creates tools, writes rules. No admin panel required.

**Competitors that do this:** None. Pi comes closest but it's terminal-only and focused on code.

**Risk:** LLM must interpret your words correctly every time. When it fails, there's no form to fall back on.

### Move 2: The agent lives inside your codebase

Dify, Intercom, CustomGPT — all serve their agent from **their** server. You embed an iframe or script that calls back to their cloud. The agent cannot touch your files.

webAgent: you can put the repo directly into your website's git repo. The agent modifies your actual `index.html`, your CSS, your JavaScript. Changes are committed, deployed, and persistent.

**Competitors that do this:** None. It's a fundamentally different relationship. Not "an external service your site talks to" but "a collaborator on your codebase."

---

## The Three Customer Types

### Type 1: The Developer (runs locally or on their server)

```
Runs: uvicorn app.main:app
Uses: Personal assistant, coding helper, research agent
Configures: Tells the agent what they need
```

Compares to: Pi, Open Interpreter, Cheshire Cat

**webAgent's edge:** Persistent memory + self-modifying behavior + web UI (not just terminal). The agent learns your preferences across sessions and adjusts its own behavior without you repeating yourself.

### Type 2: The Business (deploys on a VPS + adds a widget)

```
Runs: Docker on a $10 VPS
Uses: Customer-facing chat agent for their website
Configures: Tells the agent about their business, connects tools
```

Compares to: Dify, Intercom, CustomGPT, Chatbase

**webAgent's edge:** Configuring by conversation instead of admin UI. Connects tools (Supabase, Drive, CRM) naturally through the conversation. No learning curve — just talk.

**Trade-off:** No analytics dashboard, no A/B testing, no agent marketplace. Must build these later to compete for serious business buyers.

### Type 3: The Site Collaborator (embeds agent in codebase)

```
Runs: git submodule inside their website repo
Uses: Co-browses the site, edits code alongside the owner
Configures: Walks through the site, points at things, agent changes them
```

Compares to: Nothing. This doesn't exist in the market.

**webAgent's edge:** Uncontested space. The agent is part of the development team, directly editing the source. Changes survive deploy because they're committed to the repo.

---

## What Category Does webAgent Belong To?

It doesn't fit neatly into any existing category, which is both the opportunity and the risk.

| Category | webAgent fits? |
|----------|----------------|
| **AI agent platform** (Dify) | ❌ No visual builder, no app publishing |
| **Workflow automation** (N8N) | ❌ Not a workflow tool, not triggered by events |
| **Customer support AI** (Intercom) | ❌ No ticketing, no routing, no analytics |
| **Coding assistant** (Pi, Cursor) | ❌ Can edit code but not focused on development UIs |
| **Personal AI assistant** (Open Interpreter) | ⚡ Closest fit but webAgent has persistent web UI + widget |

webAgent is a **configurable agent harness** — a deployable runtime that becomes whatever you configure it to be, through conversation. The category doesn't have a name yet because nobody ships this.

---

## The Competitive Moats

### Moat 1: Configuration by conversation

Every other product requires a form, a builder, or YAML. webAgent requires you to *talk*. The more you talk, the better it gets. Switching costs increase with every rule, every custom tool, every accumulated preference. After a month of use, no other product has your agent's context — it's irreplaceable.

### Moat 2: Codebase integration

Once the agent is inside your repo and has modified your actual site files, extracting it means: reverting all its changes, rebuilding the widget, re-connecting your tools. Nobody else offers this, so there's no competitor to switch to.

### Moat 3: Compounding value

Day 1: fresh agent, minimal utility.
Day 30: custom tools, pipeline rules, accumulated memory, integrated services, co-edited site files.
Day 365: a deeply personalized agent that knows your business, your customers, your preferences.

No other product compounds like this because no other product is designed to persist and adapt its own behavior.

---

## The Risks That Could Kill This

### Risk 1: LLM reliability for self-configuration

If the agent misinterprets "stop searching the web before checking memory" and writes a rule that breaks behavior, trust is lost. Users will revert to products with deterministic controls (forms, builders).

**Mitigation:** Validation layers on every config write. Preview before apply. Git rollback for instant undo.

### Risk 2: The blank-slate problem

A fresh webAgent instance has zero context, zero tools, zero rules. The user must know what to ask for. Without guided onboarding, most users will type "hello" and leave.

**Mitigation:** Welcome message with example flows. Pre-seeded demo tools. "Try asking me to: connect to your Supabase, add a widget to your site, set up a daily report."

### Risk 3: Security of codebase integration

If the agent lives in the website's repo and can `edit_source` any file, a prompt injection from a customer could trick it into deleting the entire site.

**Mitigation:** Strict role separation. Admin mode (file editing) requires authentication. Customer mode (widget only, read-only tools) has no filesystem access. The two modes never overlap.

### Risk 4: Scope creep

The "talk to configure" model is powerful but it means users will ask the agent to do anything — and the agent might try, whether it should or not. Saying "no" gracefully (without the user feeling limited) is a UX challenge.

**Mitigation:** The agent knows its capabilities and communicates boundaries. "I can't do that yet, but I can remember you asked. Remind me when I have that ability."

---

## The One-Sentence Pitch

**"webAgent is an AI agent you can host anywhere, configure by talking to it, and embed in anything — it learns how you work and adapts without you ever opening a settings page."**

---

## What to Build First to Own This Space

| Order | What | Unlocks |
|-------|------|---------|
| 1 | Plugin system + git versioning | Persistent behavior, rollback, branching |
| 2 | Welcome flow + demo tools | User gets value in first 5 minutes |
| 3 | Pipeline rules from conversation | The "talk to configure" model works |
| 4 | Widget generator | Business customers can deploy on their site |
| 5 | One connector done well (Supabase) | Proof that "connect my tools" works by talking |
| 6 | Codebase integration + co-browse mode | The uncontested move — agent edits site files |
| 7 | Role separation (admin vs customer) | Security for widget + codebase modes |
| 8 | One-click deploy (Docker + $5 VPS guide) | Low-friction adoption |

Steps 1-4 make webAgent a great **personal agent**. Steps 5-8 make it a **platform for building agents for others**.
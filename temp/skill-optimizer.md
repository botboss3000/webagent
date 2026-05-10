# Skill Optimizer for webAgent

A self-improving skill layer. Background subagents analyze real user interactions and autonomously improve skill definitions to minimize user time, turns per task, message size, and token usage. User feedback is captured after each task and fed into the optimizer as a signal for what needs improving.

---

## Core Architecture

```
                    ┌─────────────────────────────┐
                    │     optimizer.json           │
                    │  (models, mode, thresholds,   │
                    │   schedule, app/per-user,     │
                    │   ask_for_feedback)           │
                    └─────────────┬───────────────┘
                                  │
                    ┌─────────────▼───────────────┐
                    │       Optimizer Runner       │
                    │  Two modes (configurable):   │
                    │                              │
                    │  MODE A (development):       │
                    │  Runs after EVERY user       │
                    │  message. Instant feedback   │
                    │  on each interaction.         │
                    │                              │
                    │  MODE B (production):        │
                    │  Background task, wakes      │
                    │  on schedule or "Run Now"    │
                    └─────────────┬───────────────┘
                                  │
                    ┌─────────────▼───────────────┐
                    │         Analyzer             │
                    │  Reads DB: interaction logs, │
                    │  skill executions, user      │
                    │  feedback comments.           │
                    │  Two scopes:                  │
                    │  - App-wide (all users)      │
                    │  - Per-user (one user)       │
                    │  Output: ranked opportunities │
                    └─────────────┬───────────────┘
                                  │
                    ┌─────────────▼───────────────┐
                    │         Proposer             │
                    │  (best model)                │
                    │  For each opportunity:       │
                    │  1. Read skill content       │
                    │  2. Read execution logs      │
                    │  3. Read user feedback text  │
                    │  4. Generate improved v2     │
                    │  5. Return diff + reasoning  │
                    └─────────────┬───────────────┘
                                  │
                    ┌─────────────▼───────────────┐
                    │         Validator            │
                    │  (medium model)              │
                    │  Replay history vs v2        │
                    │  Pass → deploy               │
                    │  Fail → reject + log         │
                    └─────────────┬───────────────┘
                                  │
                    ┌─────────────▼───────────────┐
                    │         Deployer             │
                    │  App-wide: bump official      │
                    │  Per-user: bump user's fork   │
                    │  DB: new version, old         │
                    │  deprecated (rollback-ready)  │
                    └─────────────┬───────────────┘
                                  │
                    ┌─────────────▼───────────────┐
                    │       Rollback Watch         │
                    │  (deferred — 24h timer)      │
                    │  Compare v2 vs v1 metrics    │
                    │  If worse → auto-revert      │
                    └─────────────┬───────────────┘
                                  │
                    ┌─────────────▼───────────────┐
                    │   Skill auto-improves        │
                    │  Next user request → agent   │
                    │  loads v2, follows improved  │
                    │  process. User never sees    │
                    │  the old version again.      │
                    └─────────────────────────────┘
```

---

## User Feedback Loop (Post-Task)

After the agent completes a task, if enabled in config, it asks for feedback:

```
Agent: [completes task — sends the report to Jim]
Agent: "How was that? (good / needs work / wrong)"

User: "needs work — the report was good but you forgot to include
       the client deadline from Linda's Slack message on Tuesday"

Agent: stored ✓. I'll remember the deadline next time.
```

This feedback is stored in the `skill_feedback` table.

**How the analyzer uses this:**

| User said | Analyzer interprets | Feeds into |
|-----------|-------------------|------------|
| "too verbose" | Target: token usage, message size | Proposer trims instructions |
| "missed X" | Target: completeness, accuracy | Proposer adds missing steps |
| "wrong [detail]" | Target: correctness, data source | Proposer fixes logic or adds verification |
| "great, fast" | Confirms: this skill pattern works | Stores as positive example for validator |
| "don't like the format" | Target: output structure | Proposer adjusts template/formatting |

**Toggle in UI:**
```
☑ Ask for feedback after each task
☐ Only ask on failure
☐ Never ask
```

Default: "Ask after each task" during development. "Only on failure" in production.

---

## Auth Gap Detection & Display

The analyzer detects two kinds of auth-related issues that show up in the optimizer UI.

### Scenario A: Blocked Task (Missing Credentials)

User asks for something the agent can't do because auth isn't set up.

```
User: "Check my Slack messages"

Agent checks auth_elements table for service=slack, user_id=current_user
→ not found

Agent responds:
  "I can't access Slack yet — I need you to authorize it.
   [Connect Slack]"

→ Agent logs this as a blocked interaction:
     interactions.role = "assistant"
     interactions.content = "Slack auth required"
     interactions.metadata = {"blocked": true, "service": "slack", "reason": "no_credentials"}
```

**What happens in the optimizer:**
- Analyzer sees blocked interactions → creates an auth-gap opportunity
- Type: `missing_auth` (distinct from `high_failure`, `low_rating`, etc.)
- Shows in What Would Change: `🔒 Slack — blocked (connect to enable)`
- When user connects auth → optimizer re-evaluates → skill can now run

### Scenario B: Degraded Task (Wrong/Missing Better Credentials)

Agent completes the task but with a fallback path — lower quality than if it had the right auth.

```
User: "Email Jim the report"

Agent has: generic SMTP relay credentials
Agent does NOT have: user's Gmail OAuth token

Agent sends email → succeeds → but email comes from "webagent@generic.com"
instead of "alex@company.com". Deliverability is lower. No reply tracking.

After task:
  Agent: "Email sent ✓ — but it came from a generic address.
          If you connect your work Gmail, future emails will come
          from alex@company.com with better deliverability.
          [Connect Gmail]"
```

**What the agent logs:**
- After task completion, if a better auth path exists but wasn't used:
  - insert a `skill_improvements` row with type `missing_auth` and validation `degraded`
  - metadata records: `{"used": "smtp_relay", "available": false, "optimal": "gmail_oauth"}`

**What happens in the optimizer:**
- Analyzer sees `missing_auth` improvements with `degraded` validation
- Includes them in the What Would Change preview
- Shows: `📧 send-email — degraded (connect Gmail for personal sender)` with an orange indicator

### What Would Change — Updated Colors

| Dot | Meaning | Example |
|-----|---------|---------|
| 🟢 Green | Skill working well | `process-image` — 12ms avg, no errors |
| 🟡 Yellow | Degraded — could be better with auth | `send-email` — works but uses generic sender |
| 🔴 Red | Broken — needs immediate fix | `search-slack` — 15% failure rate |
| ⚪ Gray | Blocked — can't run without auth | `check-slack` — Slack not connected |

**New optimizer opportunity type:** `missing_auth`
- Rendered differently from performance issues in the What Would Change table
- Action button: `[Connect <service>]` instead of `[Improve]`
- When user connects → `auth_element_set()` called → optimizer re-checks → skill now at full capability

### Auth DB Already Exists

webAgent already has `auth_elements` table with `service`, `config`, `secret_ref`, `is_active`. The agent checks this before running any external service tool. What's new is the optimizer **noticing** the gap and **displaying** it as an actionable item in the UI rather than just a one-time chat message.

---

## Interaction Source Labels

Each optimizer subagent's activity is stored in the `interactions` table with a new `source` column.

| `role` | `source` | Who/what |
|--------|----------|----------|
| user | user | Real human (existing default) |
| assistant | user | Real agent response (existing default) |
| tool | user | Real tool execution (existing default) |
| user | optimizer:runner | Optimizer runner sending a command |
| assistant | optimizer:runner | Optimizer's log/report response |
| user | optimizer:analyzer | Analyzer subagent querying stats |
| assistant | optimizer:analyzer | Analyzer's findings report |
| user | optimizer:proposer | Proposer reading skill content |
| assistant | optimizer:proposer | Proposer's proposed improvement |
| user | optimizer:validator | Validator replaying history |
| assistant | optimizer:validator | Validator's pass/fail decision |
| user | optimizer:deployer | Deployer writing new version |
| assistant | optimizer:deployer | Deployer's confirmation |
| user | user | Human responding to a paused optimizer prompt (in optimizer session) |

**In the UI:** Filter toggle "☐ Show optimizer interactions" (default unchecked).

---

## What Gets Measured

| Metric | How | Good/Bad |
|--------|-----|----------|
| Avg turns per task | Count interactions in sessions using this skill | High → consolidate steps |
| Avg token usage per invocation | Sum input+output tokens from interaction logs | High → trim instructions |
| Avg user time per task | Timestamp diff from user msg to final response | High → too many steps |
| Failure rate | skill_executions where success=0 | >5% → add error handling |
| User rating | skill_get_rating() score | <60 → users unhappy |
| User feedback text | skill_feedback.message content | Specific complaints → targeted fixes |
| Skill size | Length of skill content/code in tokens | Large → candidate for trim |
| Message size | Avg response length in tokens | Large → agent too verbose |

---

## Model per Agent Role

Stored in `optimizer.json`:

| Subagent | Model tier | Why |
|----------|-----------|-----|
| Analyzer | Cheap (fast/cheap) | Pure data queries — no LLM reasoning needed |
| Proposer | Best available | Writes skill code — needs full reasoning |
| Validator | Medium | Replays history — moderate reasoning |
| Deployer | Not an LLM | Pure DB writes — deterministic |
| Rollback Watch | Not an LLM | Timer-based metric comparison — deterministic |

---

## Two-Tier Training

| Tier | What's optimized | Sample needed | Who benefits |
|------|-----------------|---------------|-------------|
| App-wide | Official skills (`is_official=1`) | 100+ executions across all users | Everyone |
| Per-user | User-forked or personal skills | 10+ executions from that user | That user only |

When an official skill improves → all users get it on next load.
When a user-forked skill improves → only that user sees the change.
Users whose fork is behind official get a merge notification.

---

## Auto Rollback (Deferred)

Architecture designed but **not active during early development**.

When enabled: 24h watch period after each deploy. Compares failure rate, rating, token usage. Auto-reverts if metrics degrade.

Toggle in config UI: `☐ Enable rollback watch` — grayed out with note: "Enable after development phase."

---

## Self-Improving Skill Chain (with user feedback + pause/resume)

```
1. User sends message → agent responds with current skill v1
2. Agent asks: "How was that?" (if feedback mode enabled)
3. User gives feedback or rating → stored in skill_feedback table
4. Optimizer triggers (mode A: after every message, or mode B: on schedule)
5. Optimizer creates session: "optimizer-<original_session_id>"
6. All subagent interactions logged as interactions in this session
7. Analyzer reads interaction logs + user feedback + execution stats
8. Proposer generates improved v2 incorporating user feedback
9. Validator replays history against v2
  9a. Pass → continue to deploy
  9b. BLOCKED (needs auth/input) → status → paused_user_input
      → user sees pending task in optimizer session
      → user responds → optimizer resumes from Validator
10. Deployer writes v2 to DB, bumps version
11. Next time user's request triggers this skill
    → agent loads v2, follows improved process automatically
12. Rollback Watch (if enabled) monitors silently
```

---

## Optimizer Sessions in the WebUI

Every optimizer run creates a session that the user can open and view like any chat. The optimizer doesn't need a separate UI — it uses the existing session infrastructure, stream view, loop visualizer, and flow diagram.

### Session Creation

When the optimizer triggers, it creates a session:

```
session_id:   "optimizer-abc-123-def"
name:         "⚡ Optimizer — Email+Slack report"
created_by:   "optimizer"
```

All subagent interactions get logged as normal `interactions` rows with `source` set to the subagent role:

| Role | Source | Content |
|------|--------|---------|
| assistant | optimizer:analyzer | "Scanned 4 skills from session abc-123. search-slack: 800 tokens/call, 47 raw messages. compose-draft: 40% confirmations are rubber stamps. → 2 opportunities." |
| assistant | optimizer:proposer | "Proposal for search-slack v2: Add summarization step. Expected: -500 tokens/call." |
| tool | optimizer:proposer | [code diff — old vs new] |
| assistant | optimizer:validator | "Replayed 20 interactions. 18/20 improved. 2/20 lost nuance. → PASS (caution)." |
| assistant | optimizer:deployer | "Deployed search-slack v2. Old v1 retained for rollback." |

### What the User Sees

**In the sessions list** (left sidebar):

```
📋 Sessions
  ├── Chat with webAgent              (active)
  ├── Email Jim + Slack report        (today)
  ├── ⚡ Optimizer — Email+Slack      (today, 8 msgs)   ← auto-created
  ├── Check calendar                  (yesterday)
  └── Take meeting notes              (yesterday)
```

**Clicking the optimizer session opens it like any chat:**

```
┌───────────────────────────────────────────────┐
│ ⚡ Optimizer — Email+Slack report             │
│                                               │
│ 🔍 Analyzer                      2:14 PM      │
│ Scanned 4 skills from your "Email Jim"        │
│ session. Found 2 opportunities:               │
│                                               │
│ • search-slack: 800 tokens/call, too much     │
│   raw data. Summarize inside the tool.        │
│                                               │
│ • compose-draft: 40% of confirmations are     │
│   rubber stamps. Add confidence threshold.    │
│                                               │
│ ✏️ Proposer                       2:14 PM      │
│ Generated v2 for search-slack:                │
│ ┌─────────────────────────────────────────┐  │
│ │ + def summarize_results(messages):      │  │
│ │ +   return extract_key_points(...)      │  │
│ │ - return json.dumps(messages)           │  │
│ └─────────────────────────────────────────┘  │
│ Expected: -500 tokens per call                │
│                                               │
│ ✅ Validator                     2:14 PM      │
│ Replayed 20 past searches with v2:            │
│ ██████████████████░░ 18/20 improved           │
│ ██░░░░░░░░░░░░░░░░░  2/20 lost nuance         │
│ → PASS (caution)                              │
│                                               │
│ 📦 Deployer                      2:14 PM      │
│ Deployed search-slack v2. Old v1 kept.        │
│                                               │
│ ⚠️ Paused — needs input          2:14 PM      │
│ compose-draft v2 can't be validated            │
│ without user preference.                       │
│                                               │
│ Awaiting: Do you want auto-send for            │
│ high-confidence drafts (>90%)?                 │
│                                               │
│ [Yes, auto-send]  [No, always confirm]        │
│                                               │
└───────────────────────────────────────────────┘
```

**Stream view** — shows tool calls flowing through the optimizer pipeline.
**Loop view** — shows the optimizer's decision tree, turn by turn.
**Flow view** — shows the visual pipeline with each step highlighted.

All three visualizers work because optimizer interactions are real DB rows.

### Pause/Resume Flow

When the optimizer hits a blocker (needs OAuth scope, user preference, external setup), it pauses:

1. Validator returns `BLOCKED — needs <action>`
2. Optimizer sets status to `paused_user_input` with `blocked_skill`, `blocked_reason`, `blocked_action`
3. The paused question renders as interactive buttons in the optimizer session chat
4. User clicks a response → logged as `role: user, source: user` in the optimizer session
5. Optimizer runner detects the response → resumes from where it paused
6. Continues: Validator re-runs → Deployer → done

If the user ignores the paused session, the optimizer skips that proposal on next cycle and moves on.

**The user is never interrupted** — paused questions wait in the optimizer session. The user's regular chat is never blocked by an optimizer prompt.

### Config Modal — Status Summary

The optimizer config modal shows pending task counts:

```
┌──────────────────────────────────────────────────┐
│ ⚡ Optimizer Status                               │
│                                                  │
│ Last run: 8min ago ✓                             │
│ Deployments: 12 (all time)                        │
│ Tokens saved: ~45K across all sessions            │
│                                                  │
│ Pending tasks: 1                                  │
│   ⚠️ compose-draft — needs your input              │
│   (session: ⚡ Optimizer — Email+Slack)            │
│                                                  │
│ Active sessions: 3                                │
│   ⚡ Optimizer — Email+Slack                       │
│   ⚡ Optimizer — Meeting notes                     │
│   ⚡ Optimizer — Calendar check                    │
│                                                  │
│ [▶ Run Now]  [View Pending]                       │
└──────────────────────────────────────────────────┘
```

### Config Toggle

```
☑ Show optimizer sessions in session list
```

Default: on. In production where you don't want optimizer sessions cluttering the sidebar, toggle it off. The optimizer still runs — you just don't see the sessions unless you toggle it back on.

---

## Feedback Loop Architecture (designed now, activates later)

When traffic grows, enables post-deployment confirmation:

```
v2 deployed → wait N days → compare:
  - Avg turns per task (v2 vs v1)
  - Token usage (v2 vs v1)
  - User rating (v2 vs v1)
  - Failure rate (v2 vs v1)
  - User feedback comments (v2 vs v1 sentiment)

If improvement confirmed → mark "verified" in skill_improvements
If regressed → mark "degraded", try different approach next time
If no change → mark "neutral", no action needed
```

The tables (`skill_improvements.actual_deltas`, `optimizer_runs.summary`) already support this. Activate when sample sizes are meaningful.

---

## Config UI (Optimizer Panel in Settings)

Settings dropdown adds: `⚡ Optimizer`

Panel sections:

| Section | Fields |
|---------|--------|
| Mode | Per-interaction (dev) or Scheduled (prod) — radio buttons with description |
| Schedule | (visible only in Scheduled mode) Run every, Min interactions since last run |
| User Feedback | ☑ Ask for feedback after task / ☐ Only on failure / ☐ Never — radio or dropdown |
| Models | Analyzer model, Proposer model, Validator model (dropdowns) |
| Target Metrics | Checkboxes: turns, tokens, time, failure rate, rating, size |
| Scan Sources | Toggle cards: 🔧 Skills, ⚙️ Core Tools, 🔁 LLM Patterns, 👤 User Prefs. Controls what the analyzer scans for optimization opportunities. |
| Rollback | (deferred) ☐ Enable rollback watch — grayed out |
| Scope | App-wide: min sample, min age. Per-user: min sample, min age. |
| Notifications | Notify user, notify devs toggles, channel (email/telegram/webhook) |
| Prompt Templates | Expandable section. Lists .md files from `app/optimizer/prompts/`. Click to load into editor. Save overwrites the file. Reset restores previous. These control how analyzer/proposer/validator think. |
| Status | Last run, deployments count, tokens saved, pending tasks count, auth gaps count. ▶ Run Now, View Pending, View History buttons. |
| Auth | Shows connected services, available-to-connect services, and which skills improve with each connection. Links to `auth_element_set()`. |
| Sessions | ☑ Show optimizer sessions in session list. ☑ Allow optimizer to pause for user input (vs skip auth-required changes). |

---

## DB: New Tables

**`optimizer_runs`** — one row per optimizer execution:
- id, status (running/success/failed/paused_user_input), started_at, completed_at
- blocked_skill (nullable — skill name if paused), blocked_reason, blocked_action, blocked_at
- skills_analyzed, proposals_generated, proposals_deployed, proposals_rejected
- errors (JSON), summary (text), config_snapshot (JSON copy of optimizer.json)
- session_id (links to the optimizer session in the sessions table for UI display)

**`skill_improvements`** — one row per deployed improvement:
- id, skill_id, old_version, new_version
- opportunity_type (high_turns/high_tokens/high_failure/low_rating/large_size/missing_auth)
- old_metrics (JSON), expected_deltas (JSON), actual_deltas (JSON, filled later)
- validation_result, proposer_reasoning, diff_summary
- user_feedback_ref (nullable — link to the feedback that triggered this improvement)
- deployed_at, rolled_back_at (nullable)

---

## Config File: `optimizer.json`

Lives in project root (same pattern as `provider.json`). Sections:

| Section | Fields |
|---------|--------|
| schedule | interval, min_interactions, cron_override |
| user_feedback | ask_after_task (always / on_failure / never) |
| sessions | show_in_list (bool), allow_pause_for_input (bool) |
| models | analyzer, proposer, validator model names |
| target_metrics | List of enabled metrics |
| validation | strictness, min_improvement_pct |
| rollback | auto_rollback_failure_spike, failure_spike_threshold, rating_drop_threshold, watch_hours |
| app_wide | min_sample, min_skill_age_days, auto_deploy |
| per_user | min_sample, min_skill_age_days, auto_deploy |
| notifications | notify_user, notify_devs, channel |
| state | last_run_at, improvements_deployed, last_run_status |

---

## Files to Create

| File | Purpose |
|------|---------|
| `app/optimizer/config.py` | Load/save optimizer.json, schema validation |
| `app/optimizer/runner.py` | Background loop: schedule → orchestrates phases |
| `app/optimizer/analyzer.py` | Queries DB, computes per-skill metrics, ranks opportunities |
| `app/optimizer/prompts/analyzer-prompt.md` | Analyzer instructions — how to scan and rank skill performance |
| `app/optimizer/prompts/proposer-prompt.md` | Proposer instructions — how to generate improved skill versions |
| `app/optimizer/prompts/validator-prompt.md` | Validator instructions — how to replay history and validate changes |
| `app/optimizer/proposer.py` | Calls LLM with proposer template + skill evidence. Returns improved version as JSON. |
| `app/optimizer/prompt_loader.py` | Loads .md templates, fills placeholders, lists templates for UI. |
| `app/optimizer/validator.py` | Replays historical interactions against proposed skill |
| `app/optimizer/deployer.py` | Writes new skill version + logs improvement + wraps rollback watch |
| `app/optimizer/rollback.py` | Timer-based monitor, compares before/after metrics, reverts |
| `app/admin/optimizer.py` | FastAPI router: GET/POST config, GET run history, POST run-now |
| `ui/js/optimizer.js` | Settings panel UI for optimizer config + status display |

## Files to Modify

| File | Change |
|------|--------|
| `index.html` | Add optimizer section to settings modal + dropdown menu item |
| `app/main.py` | Register optimizer API router + start background task |
| `app/db/interface.py` | Add optimizer_runs + skill_improvements table methods |
| `app/db/local.py` | Add both tables + CRUD |
| `app/db/supabase.py` | Same schema migration |
| `app/agent/prompts.py` | Add feedback prompt injection (ask after task if enabled) |
| `app/agent/loop.py` | Pause/resume: detect task completion + inject feedback request. Recognize optimizer session IDs for user-input resumes. |
| `ui/js/sessions.js` | Show optimizer sessions in session list with ⚡ prefix and muted styling. Toggle to hide them. |
| `ui/js/stream.js` | Render optimizer source labels distinctly (colored badges per subagent type). |
| `ui/js/main.js` | Already done — initOptimizer wired. |

---

## Risks

| Risk | Mitigation |
|------|-----------|
| Proposer generates bad skill content | Validator rejects it. Rollback catches escapes. Versioned so revert is one write. |
| Validator too slow (replaying many interactions) | Sample last 20 sessions per skill. Cache simulation results. |
| Optimizer competes with user for LLM capacity | In dev mode: runs after response, not during. In scheduled mode: configurable quiet hours. |
| Per-interaction mode costs tokens on every user message | Config UI shows estimated daily cost. User can switch to scheduled mode. |
| Token cost of optimizer exceeds savings | Track optimizer cost separately. Warn in status panel if net negative. |
| App-wide optimization ignores diverse usage | Check variance across users. If high variance, skip app-wide, do per-user only. |
| User feedback is vague ("it's bad") | Analyzer reads full interaction + metadata to infer what went wrong. Asks clarifying questions. |
| User never gives feedback | Analyzer still has execution logs and failure rates as signals. Feedback is additive, not required. |

---

## Open Questions (Deferred)

- Feedback loop activation threshold (how many executions before auto-enable?)
- Per-user fork merge notification (auto-merge or ask user?)
- Multi-instance coordination
- Rollback watch activation threshold
- Minimum user feedback sample before feedback-driven proposals are generated

---

## Validation Checkpoints

Sequence of manual QA steps to verify each layer is working before building the next.

### Checkpoint A — Server Starts Cleanly

1. Start webAgent: `uvicorn app.main:app --reload`
2. Watch terminal for: no Python import errors, no startup crashes, API docs at `/docs`
3. Open `/docs` → look for `📦 Optimizer` tag under the endpoints — should see `/admin/optimizer/config`, `/admin/optimizer/runs`, `/admin/optimizer/run`, `/admin/optimizer/improvements`
4. What to look for: if `app/main.py` failed to import `admin.optimizer`, you'll get a 500 on `/docs` or a `ModuleNotFoundError` at startup

### Checkpoint B — Config API Works

1. `GET /admin/optimizer/config` → returns JSON with all defaults (mode: live, intensity: 3, user_feedback: always, metrics list, models, schedule, scope, notifications)
2. `POST /admin/optimizer/config` with `{"mode": "scheduled"}` → returns 200
3. `GET /admin/optimizer/config` again → mode is now "scheduled"
4. `POST /admin/optimizer/config/reset` → returns defaults again
5. What to look for: 500 means `optimizer.json` write failed (permissions or missing config module). Watch server logs for "Corrupt optimizer.json" warning if something malformed passed validation.

### Checkpoint C — UI Opens and Renders

1. Open `/index.html` in browser
2. Click `⚙️ Config ▼` → should see `⚡ Optimizer` menu item in the dropdown
3. Click it → optimizer modal opens. All sections render:
   - Mode toggle (⚡ Live highlighted blue, 📅 Scheduled dimmed gray)
   - User Feedback toggle (💬 Always ask highlighted)
   - Intensity slider with gradient bar + description text below
   - Target Metrics 2×3 grid (5 active cards in blue, 📏 Skill Size dimmed)
   - Models per Role dropdowns (Analyzer/Proposer/Validator)
   - Scope: App-Wide + Per-User number inputs with default values
   - What Would Change table with 4 skills + colored dots
   - Workflow diagram: 6-step pipeline with labels underneath
   - Activity Log (collapsed) — click "Show ▾" to expand
   - Notifications toggles + channel dropdown
   - Save / ▶ Run Now / View History buttons
4. Switch mode to 📅 Scheduled → schedule config appears (interval + min interactions)
5. Switch feedback to ⚠️ On failure → second button highlights
6. Drag intensity slider → description text changes between Conservative/Balanced/Aggressive
7. Click a metric card (e.g. 📏 Skill Size) → toggles between blue active and gray dimmed
8. Click "Run Now" → ⏳ button, workflow steps highlight sequentially (350ms each), then resets
9. Click "Save" → "Saved locally" toast appears (backend may not be ready in initial test)
10. Press ESC → modal closes
11. What to look for: JS console errors (missing import, undefined DOM ref, `initOptimizer` not called). White screen → check `main.js` import.

### Checkpoint D — DB Tables Exist

1. With local mode active, check the SQLite DB: `sqlite3 app/db/local.db ".schema optimizer_runs"`
2. Should show the full schema with all columns (id, session_id, status, blocked_skill, blocked_reason, blocked_action, blocked_at, skills_analyzed, proposals_generated, proposals_deployed, proposals_rejected, errors, summary, config_snapshot)
3. `sqlite3 app/db/local.db ".schema skill_improvements"` → same check
4. `sqlite3 app/db/local.db "PRAGMA table_info(interactions)" | grep source` → column exists, type TEXT, default 'user'
5. What to look for: `no such table` → migration didn't run. Check startup logs for "PRAGMA table_info(interactions)" errors. Delete `local.db` and restart to force fresh schema.

### Checkpoint E — Chat Triggers Optimizer Session

1. Open `/index.html`, start a new chat session
2. Send a simple message: "Hello, what can you help with?"
3. Wait 2-3 seconds after the agent responds (background optimizer fires asynchronously)
4. Check DB: `sqlite3 app/db/local.db "SELECT id, title, created_at FROM sessions ORDER BY created_at DESC LIMIT 3"`
5. Should see a session with id starting with `optimizer-` and title `⚡ Optimizer`
6. What to look for: no optimizer session → `_run_background_optimizer` failed silently. Check server logs for "Optimizer skipped" or exception. Check `app/api/chat.py` import of `run_optimizer`.

### Checkpoint F — Optimizer Run Record Created

1. After any chat message, check: `sqlite3 app/db/local.db "SELECT id, status, skills_analyzed, proposals_deployed, summary FROM optimizer_runs ORDER BY started_at DESC LIMIT 1"`
2. Should show: `running | 0 | 0 | ` (or `success` if fast enough)
3. Wait 5 seconds after a chat, check again → status should be `success`, summary has text like "Analyzed 0 skills. 0 improvements deployed"
4. What to look for: status stuck at `running` → runner crashed. Check logs for "Optimizer run failed".

### Checkpoint G — Analyzer Scans Skills

1. Create a skill with some execution history: tell the agent to use a tool that exists (e.g. `get_time`), OR manually insert dummy data:
   ```sql
   INSERT INTO skills (id, user_id, name, description, is_active) VALUES ('test1', '__anonymous__', 'test-skill', 'Test skill', 1);
   INSERT INTO skill_executions (id, skill_id, user_id, session_id, success, duration_ms) VALUES ('ex1', 'test1', '__anonymous__', 'sess-test', 0, 100);
   INSERT INTO skill_executions (id, skill_id, user_id, session_id, success, duration_ms) VALUES ('ex2', 'test1', '__anonymous__', 'sess-test', 0, 200);
   INSERT INTO skill_executions (id, skill_id, user_id, session_id, success, duration_ms) VALUES ('ex3', 'test1', '__anonymous__', 'sess-test', 1, 150);
   ```
2. Send another chat message → optimizer runs again
3. Check: `sqlite3 app/db/local.db "SELECT summary FROM optimizer_runs ORDER BY started_at DESC LIMIT 1"`
4. Should now say something like "Analyzed 1 skills. 1 improvements deployed, 0 rejected"
5. What to look for: still 0 analyzed → analyzer skipped because `total_executions < min_sample` (default is 10 for user skills). Insert 10+ executions.

### Checkpoint H — Skill Improvement Recorded

1. After a successful analyzer pass (G above passes):
2. Check: `sqlite3 app/db/local.db "SELECT skill_name, old_version, new_version, opportunity_type, validation_result FROM skill_improvements ORDER BY deployed_at DESC LIMIT 1"`
3. Should show the skill name, old version, new version (+1), opportunity_type (high_failure), validation_result (pass)
4. What to look for: no rows → deployer phase didn't run. Check that `proposals_deployed > 0` in optimizer_runs row. If proposals_generated > 0 but proposals_deployed = 0, the validator rejected it (check logs).

### Checkpoint I — Optimizer Session Visible

1. In the UI sessions list (left sidebar), check for entries starting with `⚡ Optimizer —`
2. If sessions.js hasn't been updated yet, check DB directly: `sqlite3 app/db/local.db "SELECT id, title FROM sessions WHERE id LIKE 'optimizer-%' ORDER BY created_at DESC"`
3. Each chat session that triggered an optimizer run should have a corresponding `optimizer-<session_id>` session
4. What to look for: sessions exist in DB but not in UI → `ui/js/sessions.js` needs the ⚡ prefix rendering fix (not blocking — session exists, just invisible).

### Checkpoint J — Interactions Have Source Column

1. `sqlite3 app/db/local.db "SELECT source, COUNT(*) FROM interactions GROUP BY source"`
2. Should show: `user` (real chats), `optimizer:analyzer`, `optimizer:proposer`, `optimizer:validator`, `optimizer:deployer`
3. What to look for: only `user` rows → `_log_optimizer_interaction` in runner.py failed silently. Check that `insert_interaction` accepts and passes `source` param correctly.

### Checkpoint K — Feedback Prompt in System Prompt

1. Start a new chat with feedback mode set to "always"
2. The system prompt (not visible to user, but you can log it) should contain: `# [FEEDBACK BOT]` followed by the feedback instructions
3. Alternatively: ask the agent "What's your instruction about feedback?" — it should respond about asking "How was that?"
4. What to look for: no feedback section → `get_feedback_mode()` returns wrong value or `prompts.py` conditional not reached. Set `optimizer.json` user_feedback to "always" explicitly.

### What Passes Mean — Ready for Next Phase

| Checkpoints passed | What's confirmed | Ready to build |
|--------------------|-----------------|----------------|
| A, B, C | UI + config API work end-to-end | ✅ Proposer LLM call |
| D | DB tables + source column exist | ✅ Proposer LLM call |
| E, F, J | Optimizer runs after chat, creates sessions, logs interactions with source labels | ✅ Proposer LLM call |
| G, H | Analyzer finds opportunities and records improvements | ✅ Validator replay engine |
| I | Sessions visible in UI (if not, just fix sessions.js) | ✅ Session list rendering |
| K | Feedback prompt injected | ✅ Feedback capture + rate_skill tool wiring |

### Expected Failures (Non-Blocking)

These are known gaps that will fail until explicitly built:

- **Proposer doesn't modify skill code** — it generates reasoning text but doesn't call an LLM. The `expected_deltas` will be empty `{}`. Not a bug — the LLM integration is the next phase.
- **Validator is simplified** — uses a `has_sufficient_data` check instead of full interaction replay. Real validation requires the Proposer LLM integration first.
- **Deployer doesn't bump skill versions** — it records the intent but doesn't update `skills.version` or write new `code` content. Needs Proposer output.
- **Sessions list doesn't show ⚡ prefix** — `sessions.js` hasn't been updated. Optimizer sessions exist in DB, just not rendered distinctly in the sidebar.
- **Supabase paths not implemented** — only `local.py` has the optimizer methods. `supabase.py` needs mirror methods for cloud mode to work.


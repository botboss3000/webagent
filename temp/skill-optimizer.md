# Skill Optimizer for webAgent

A self-improving skill layer. Users trigger optimization with feedback. The system analyzes interactions, proposes changes to modifiable elements, tests them in sandbox trials, compares results, and asks the user to approve winning changes.

---

## Architecture

```
User feedback + interaction log
        │
        ▼
┌──────────────────────────────────────────────────┐
│ PRE-FILTER (code, no LLM)                         │
│                                                   │
│ Quick DB scan: failure rates, durations, counts.   │
│ Pushes stats + transcript into Proposer context.   │
│ Free — just SQL queries.                           │
└──────────────┬───────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────┐
│ PROPOSER (single LLM, 3 modes)                     │
│                                                   │
│ System prompt from context_template:               │
│   "proposer-prompt"                                │
│                                                   │
│ Mode 1 — Analyze + Propose:                        │
│   Receives transcript + stats + all modifiable      │
│   elements. Identifies what went wrong, which      │
│   elements to change, generates concrete edits.     │
│                                                   │
│ Mode 2 — Present results:                          │
│   Takes Reviewer output, formats for user display   │
│   in optimizer session. Asks user which changes     │
│   to apply.                                        │
│                                                   │
│ Output Mode 1: analysis + N change proposals        │
│ Output Mode 2: user-facing comparison + callbacks   │
└──────────────┬───────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────┐
│ EXECUTOR (LLM, "executor-prompt")                  │
│                                                   │
│ For each proposed change:                          │
│   1. Applies change to sandbox copy                 │
│   2. Re-runs user's original prompt                 │
│   3. Measures: turns, tokens, time, response length │
│   4. Reports metrics                                │
│                                                   │
│ Costs N LLM calls per element tested.               │
│ Runs 2-3 trials per change to average variance.     │
└──────────────┬───────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────┐
│ REVIEWER (LLM, "reviewer-prompt")                   │
│                                                   │
│ Compares each trial against baseline:               │
│   - Time: faster?                                   │
│   - Tokens: fewer?                                  │
│   - Response length: shorter (better)?              │
│   - Turns: fewer?                                   │
│   - Outcome quality: same task completed?           │
│                                                   │
│ Trial must improve ≥ min_improvement% on one        │
│ criterion without degrading others by >5%.           │
│                                                   │
│ Output: ranked winners + metric deltas               │
└──────────────┬───────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────┐
│ User approval (optimizer session)                  │
│                                                   │
│ Proposer Mode 2 presents results in chat.           │
│ User clicks: [Apply] [Skip] [Try different]        │
│                                                   │
│ Approved changes → Deployer writes permanently.     │
│ Rejected changes → discarded.                      │
└──────────────────────────────────────────────────┘
```

---

## Optimizer Prompt Templates

All stored in `context_documents` with `context_type="optimizer"`. Seeded from `context_templates` on first optimizer run per user. User-editable via DB viewer or tools.

### `analyzer-report`
Format for the pre-filter's output when passed to Proposer. Not an LLM prompt — it's a structure template the pre-filter code fills in.

### `proposer-prompt`  
System prompt for the Proposer LLM. Instructions for analyzing interactions, identifying modifiable elements, proposing concrete changes, and presenting results to the user.

### `executor-prompt`
System prompt for the Executor LLM. Instructions for re-running a user prompt with a modified element applied to a sandbox copy and measuring the metrics.

### `reviewer-prompt`
System prompt for the Reviewer LLM. Instructions for comparing trial runs against the baseline and picking winners based on configurable criteria and thresholds.

---

## Modifiable Elements

| Element | Where it lives | How Proposer edits it |
|---------|---------------|----------------------|
| `skills.md` | context_documents WHERE context_type="skills" | Rewrites content |
| `tool.md` | context_documents WHERE context_type="tools" | Rewrites tool descriptions |
| `user.md` | context_documents WHERE context_type="user" | Updates user preferences |
| `agent.md` | context_documents WHERE context_type="agent" | Updates agent behavior rules |
| System prompt | agents.system_prompt column | Rewrites core prompt |
| Tool code | skills.code column | Rewrites Python code |
| Hard code (files) | `app/` source files on disk | Uses edit_source/write_source tools |

---

## User-Facing Metrics (Criteria)

Defined in optimizer config UI. Defaults:

| Metric | Good direction |
|--------|---------------|
| Total time | Lower = faster responses |
| Token cost | Lower = cheaper calls |
| Response length | Lower = more concise |
| Turn count | Lower = fewer back-and-forths |

---

## Config UI

Settings dropdown: `⚡ Optimizer`

| Section | Fields |
|---------|--------|
| Mode | Live (after every message) or Scheduled or Manual only |
| User Feedback | Always ask / Only on failure / Never |
| LLM Models | Proposer model, Executor model (dropdowns). Reviewer shares Proposer's model. |
| Criteria | Checkboxes + weight sliders: time, tokens, length, turns |
| Thresholds | Min improvement % for a trial to be considered a winner |
| Sessions | Show optimizer sessions in list: on/off |
| Prompts | Link to edit optimizer template context documents |

---

## Interaction Source Labels

| `source` | Who |
|----------|-----|
| user | Real human |
| optimizer:prefilter | Pre-filter script output |
| optimizer:proposer | Proposer LLM (analysis + proposals + user presentation) |
| optimizer:executor | Executor LLM (trial runs) |
| optimizer:reviewer | Reviewer LLM (comparison) |
| optimizer:deployer | Deployer (writes approved changes) |

---

## Self-Improving Chain

```
1. User sends message → agent responds
2. User: "optimize that" or gives feedback
3. Pre-filter scans stats
4. Proposer (Mode 1): analyzes transcript, proposes changes to elements
5. Executor: runs N trials (2-3 per change)
6. Reviewer: compares trials → picks winners
7. Proposer (Mode 2): presents winners to user in optimizer session
8. User approves/rejects changes
9. Deployer writes approved changes permanently
10. Next user request uses improved elements
```

---

## Config File: `optimizer.json`

| Section | Fields |
|---------|--------|
| mode | live / scheduled / manual |
| user_feedback | always / on_failure / never |
| models | proposer, executor model names |
| criteria | time (weight), tokens (weight), length (weight), turns (weight) |
| thresholds | min_improvement_pct, max_degradation_pct |
| trials | trials_per_change (default 2) |
| sessions | show_in_list, allow_pause_for_input |
| state | last_run_at, improvements_deployed, last_run_status |

---

## DB Tables

**`optimizer_runs`** — one per optimizer execution: status, skills/elements analyzed, proposals generated, trials run, deployments, session_id.

**`skill_improvements`** — one per deployed improvement: skill_id, old_version, new_version, before/after metrics, proposer_reasoning, user_approved_at.

**`context_templates`** rows — optimizer prompts seeded with `context_type="optimizer"`.

**`context_documents`** rows — per-user copies of optimizer prompts.

---

## Files

| File | Purpose |
|------|---------|
| `optimizer.json` | Config |
| `app/optimizer/prefilter.py` | Pre-filter: DB stats + transcript prep |
| `app/optimizer/proposer.py` | Proposer LLM (analyze + propose + present) |
| `app/optimizer/executor.py` | Executor LLM (sandbox trials) |
| `app/optimizer/reviewer.py` | Reviewer LLM (compare trials) |
| `app/optimizer/deployer.py` | Deployer (write approved changes) |
| `app/optimizer/runner.py` | Orchestrator: prefilter → proposer → executor → reviewer → deployer |
| `app/optimizer/sandbox.py` | Sandbox: apply temp changes for trial runs |
| `app/admin/optimizer.py` | API endpoints for config + manual trigger |
| `app/tools/loader.py` | `run_optimizer` tool (built-in) |
| `app/agent/loop.py` | Auto-fire after completion |
| `app/agent/prompts.py` | Feedback prompt injection |
| `ui/js/optimizer.js` | Config panel UI |
| `index.html` | Optimizer modal + menu item |

---

## Risks

| Risk | Mitigation |
|------|-----------|
| Executor trials are expensive (N × LLM calls) | Limit trials per run. Cache results. Pre-filter eliminates obviously bad proposals before testing. |
| LLM non-determinism makes trial comparison fuzzy | Run 2-3 trials per change and average. Require ≥15% improvement before declaring winner. |
| Proposer suggests changes to the wrong element | Sandbox isolation — bad change only affects trial, doesn't leak. User approval gate catches wrong proposals. |
| Optimizer finds no improvements after multiple attempts | Report: "I tested X changes. None improved. The issue may need a different approach." User can provide more guidance. |

---

## Open Questions

- Run 2 or 3 trials per change? (2 = cheaper, 3 = more reliable average)
- Separate model for Reviewer or share Proposer's model?
- Minimum user interaction sample before per-user optimization kicks in?

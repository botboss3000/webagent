# Superpower: Skill Auto-Improver for webAgent

A meta-optimization layer. Background subagents analyze real user interactions and autonomously improve skill definitions to minimize user time, turns per task, message size, and token usage.

---

## Core Architecture

```
                    ┌─────────────────────────────┐
                    │     optimizer.json           │
                    │  (models, thresholds,        │
                    │   schedule, app/per-user)     │
                    └─────────────┬───────────────┘
                                  │
                    ┌─────────────▼───────────────┐
                    │       Optimizer Runner       │
                    │  (background task, wakes     │
                    │   on schedule or "Run Now")  │
                    └─────────────┬───────────────┘
                                  │
                    ┌─────────────▼───────────────┐
                    │         Analyzer             │
                    │  Queries DB in two scopes:   │
                    │  - App-wide (all users)      │
                    │  - Per-user (one user)       │
                    │  Output: ranked opps w/      │
                    │  scope tag [app|user]        │
                    └─────────────┬───────────────┘
                                  │
                    ┌─────────────▼───────────────┐
                    │         Proposer             │
                    │  (best model)                │
                    │  For each opportunity:       │
                    │  1. Read skill content       │
                    │  2. Read sample executions   │
                    │  3. Generate improved v2     │
                    │  4. Return diff + reasoning  │
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
                    │  App-wide: bump official     │
                    │  Per-user: bump user's fork  │
                    │  DB: new version, old        │
                    │  deprecated (rollback-ready) │
                    └─────────────┬───────────────┘
                                  │
                    ┌─────────────▼───────────────┐
                    │       Rollback Watch         │
                    │  (24h timer)                 │
                    │  Compare v2 vs v1 metrics    │
                    │  If worse → auto-revert      │
                    │  If same/better → keep       │
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

## What Gets Measured

| Metric | How | Good/Bad |
|--------|-----|----------|
| Avg turns per task | Count interactions in sessions using this skill | High → consolidate steps |
| Avg token usage per invocation | Sum input+output tokens from interaction logs | High → trim instructions |
| Avg user time per task | Timestamp diff from user msg to final response | High → too many steps |
| Failure rate | skill_executions where success=0 | >5% → add error handling |
| User rating | skill_get_rating() score | <60 → users unhappy |
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

## Auto Rollback

After each deploy, a 24-hour watch period starts:
- Every hour, compare v2 vs v1 on: failure rate, user rating, token usage
- If failure rate doubles OR rating drops 10+ points → auto-revert
- Revert is a DB write: set old version `is_active=1`, new version `is_active=0`
- Failed version stays in `skill_improvements` with `rolled_back_at` timestamp

Next invocation loads the reverted version. The Proposer learns from the failure.

---

## Self-Improving Skill Chain

```
1. Proposer generates improved skill v2
2. Validator replays history against v2 → pass
3. Deployer writes v2 to DB, bumps version
4. Next time user's request triggers this skill
   → agent loads v2 (not v1)
   → follows improved process automatically
5. Rollback Watch starts 24h timer
   → compares metrics hourly
   → if worse → revert
   → if same/better → keep, mark as confirmed
```

---

## Feedback Loop Architecture (designed now, activates later)

When traffic grows, enables post-deployment confirmation:

```
v2 deployed → wait N days → compare:
  - Avg turns per task (v2 vs v1)
  - Token usage (v2 vs v1)
  - User rating (v2 vs v1)
  - Failure rate (v2 vs v1)

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
| Schedule | Run every (daily/weekly/monthly/interactions), Min interactions since last run |
| Models | Analyzer model, Proposer model, Validator model (dropdowns, reuse provider endpoint) |
| Target Metrics | Checkboxes: turns, tokens, time, failure rate, rating, size |
| Validation | Min expected improvement % |
| Rollback | Auto-rollback toggles + thresholds, watch duration |
| Scope | App-wide: min sample, min age, auto-deploy toggle. Per-user: same. |
| Notifications | Notify user, notify devs toggles, channel (email/telegram/webhook) |
| Status | Last run, deployments count, tokens saved, Run Now / View History buttons |

---

## DB: New Tables

**`optimizer_runs`** — one row per optimizer execution:
- id, status (running/success/failed), started_at, completed_at
- skills_analyzed, proposals_generated, proposals_deployed, proposals_rejected
- errors (JSON), summary (text), config_snapshot (JSON copy of optimizer.json)

**`skill_improvements`** — one row per deployed improvement:
- id, skill_id, old_version, new_version
- opportunity_type (high_turns/high_tokens/high_failure/low_rating/large_size)
- old_metrics (JSON), expected_deltas (JSON), actual_deltas (JSON, filled later)
- validation_result, proposer_reasoning, diff_summary
- deployed_at, rolled_back_at (nullable)

---

## Config File: `optimizer.json`

Lives in project root (same pattern as `provider.json`). Sections:

| Section | Fields |
|---------|--------|
| schedule | interval, min_interactions, cron_override |
| models | analyzer, proposer, validator model names |
| target_metrics | List of enabled metrics |
| validation | strictness, min_improvement_pct |
| rollback | auto_rollback_failure_spike, failure_spike_threshold, rating_drop_threshold, watch_hours |
| app_wide | min_sample, min_skill_age_days, auto_deploy |
| per_user | min_sample, min_skill_age_days, auto_deploy |
| notifications | notify_user, notify_devs, channel |
| state | last_run_at, improvements_deployed, last_run_status |

---

## Map: Symphony → webAgent Optimizer

| Symphony concept | webAgent equivalent |
|----------------|-------------------|
| Issue tracker (Linear) | interactions + skill_executions tables |
| Per-issue workspace | Fresh subagent per optimization proposal |
| WORKFLOW.md (in-repo policy) | optimizer.json (in project root) |
| Coding agent runs the task | Proposer subagent generates skill v2 |
| Proof of work (CI, PR review) | Validator replays history against v2 |
| Land PR when accepted | Deployer writes v2 to DB, bumps version |
| Observe concurrent runs | optimizer_runs table with status logs |
| Recovery on restart | Read optimizer_runs state, resume incomplete |

---

## Files to Create

| File | Purpose |
|------|---------|
| `app/optimizer/config.py` | Load/save optimizer.json, schema validation |
| `app/optimizer/runner.py` | Background loop: schedule → orchestrates phases |
| `app/optimizer/analyzer.py` | Queries DB, computes per-skill metrics, ranks opportunities |
| `app/optimizer/proposer.py` | Takes opportunity → calls LLM subagent → returns proposal |
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
| `ui/js/settings.js` | Wire optimizer tab into existing settings flow |

---

## Risks

| Risk | Mitigation |
|------|-----------|
| Proposer generates bad skill content | Validator rejects it. Rollback catches escapes. Versioned so revert is one write. |
| Validator too slow (replaying many interactions) | Sample last 20 sessions per skill. Cache simulation results. |
| Optimizer competes with user for LLM capacity | Background task, low priority, configurable schedule. |
| Token cost of optimizer exceeds savings | Track optimizer cost separately. Warn in status panel if net negative. |
| App-wide optimization ignores diverse usage | Check variance across users. If high variance, skip app-wide, do per-user only. |

---

## Open Questions (Deferred)

- Feedback loop activation threshold (how many executions before auto-enable?)
- Per-user fork merge notification (auto-merge or ask user?)
- Multi-instance coordination (if running multiple webAgent instances sharing one DB, only one should run the optimizer)
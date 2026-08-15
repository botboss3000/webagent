# Diagnosing a chat session — the fast recipe

Load this skill before investigating any session, run, or tool problem. One or two well-parameterized calls answer most questions. Don't guess or code-dive before pulling the data.

## Data map — which tool gets which data

| What you need | Tool | Where / what to query |
|---|---|---|
| Turn-by-turn transcript | `run_python` + `sqlite3` | `data/db/local.db` → `interactions` (filter by `session_id`, order by `created_at`) |
| Run state (stop_cause, resume budget) | `run_python` + `sqlite3` | `data/db/local.db` → `session_runs` (one row per session) |
| Session metadata (agent, status) | `run_python` + `sqlite3` | `data/db/local.db` → `sessions` |
| Agent config / prompt slots | `db_query` | context documents (NOT raw SQL — reads/writes prompt slot documents) |
| Server errors, tracebacks, run lifecycle | `read_diagnostics` | `data/db/logs.db` → `diagnostics` table |
| Tool timing / failure rates | `read_diagnostics(view='tools')` | `data/db/logs.db` → `tool_executions` table |
| Source code | `read_source`, `search_source` | `plugins/`, `app/`, `docs/` |

Key point: `db_query` is for **prompt slot documents** on the agent — it is NOT a SQL interface. For raw DB queries on `local.db` or `logs.db`, use `run_python` with the `sqlite3` module.

## Scenario → parameters cheat sheet

### "Why won't this session resume?"

One flight-recorder call + one DB query:

1. `read_diagnostics(session_id="…", levels="info,warning,error,critical", categories="run,recovery", since_minutes=1440)` 
   — **must include `info` level**; resume/recovery events are recorded at info, not warning
2. Query `session_runs` for that session_id via `run_python` — look at `stop_cause`, `resume_attempts`, `max_resume_attempts`, `status`, `error`

Auto-resume only fires for **involuntary** stop causes:
- ✅ `server_restart`, `zombie`, `frozen`, `crash`, `empty_response` — auto-resumed on boot/watchdog
- ❌ `complete`, `user_stop`, `replaced`, `needs_manual_resume` — voluntary, never auto-resumed

The resume budget: `max_resume_attempts` (default 3, from `app-settings.json` → `run_max_resume_attempts`). Once `resume_attempts >= max`, the run is marked failed and auto-resume stops. Manual resume (`POST /resume`) still works — it bypasses the budget.

Resume engine code: `app/agent/runner.py` → `_classify_resume()`, `_RESUMABLE_CAUSES`, `resume_one()`.

### "What error crashed this turn?"

`read_diagnostics(levels="error,critical", categories="server,tool", session_id="…", since_minutes=60)` 

The `detail` field holds the full Python traceback. Cross-reference timestamps with the `interactions` transcript to pinpoint which tool call failed.

### "Which tool is slow or failing often?"

`read_diagnostics(view="tools", since_minutes=1440)` — returns per-tool call count, failure rate, avg/max duration, and recent failures.

### "What happened in the conversation?"

`run_python` querying `data/db/local.db`:
```python
SELECT role, tool_name, substr(content,1,300) AS content, status, created_at
FROM interactions WHERE session_id=? ORDER BY created_at
```

## Gotchas

- **`read_diagnostics` defaults to `warning,error,critical`** — run lifecycle and resume events are `info` level. Always include `levels="info,warning,error,critical"` when diagnosing run state.
- **`read_source` uses `offset` and `limit`** — NOT `start`/`end`. `offset` is 1-indexed.
- **`db_query` is NOT SQL** — it reads/writes agent prompt slot documents. Use `run_python` + `sqlite3` for database tables.
- **The flight recorder has a time window** — default `since_minutes=120`. Widen it for older incidents (e.g. `since_minutes=1440` for the last 24h).
- **`read_diagnostics` and `read_diagnostics(view='tools')` query different databases** — diagnostics reads the `diagnostics` table in `data/db/logs.db`; tools view reads `tool_executions`, also in `logs.db`.
- **Older sessions may have no flight-recorder entries** — the ring buffer is RAM-based and the durable store auto-prunes. Widen `since_minutes` or drop the time filter entirely.

## Quick recipe (the original, still valid)

From `docs/claude/diagnosing-sessions.md`:

1. Dump `interactions` for the `session_id` → read the turn-by-turn flow, find the broken step.
2. Query `diagnostics` (errors, same time window / session) → get the real traceback behind any vague tool error or HTTP 500.
3. Check `tool_executions` for the specific failing tool if needed.
4. Decide: model/skill issue (instructions) vs. ability/core logic (code) — then read that source.

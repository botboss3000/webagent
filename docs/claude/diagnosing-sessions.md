# Diagnosing a chat session (why did the agent fail?)

Read this when the user points you at a specific in-app **session** (by id or title) and asks
why it misbehaved, errored, or didn't do what they wanted. The chat UI shows the user's view;
the *real* story is in the local databases. Don't guess from the UI or the code alone — pull the
transcript and the logs first.

> Reminder: the dev server uses **SQLite** at `data/db/local.db` (app data) and
> `data/db/logs.db` (server logs + diagnostics). Open them read-only and query directly — do
> **not** restart or write to the running server's DB while diagnosing.

## 1. The transcript — `data/db/local.db`, `interactions` table

This is the ground truth for what happened in the session, turn by turn.

- Filter `interactions` by `session_id`, order by `created_at`.
- Useful columns: `role` (user / assistant / tool), `tool_name`, `content`, `output`,
  `status`. Assistant rows carry the model's `tool_calls` in `output`; tool rows carry the tool's
  result (and `success: false` / blocked messages) in `content` / `output`.
- This shows the **sequence**: what the user asked, what the model decided, which tools it called,
  what each tool returned, and where the chain broke.

Related tables worth a look depending on the symptom: `sessions` (agent binding, metadata),
`agent_spawns` (orchestration workers + their `status` / `result_summary`), `session_runs`,
`tools`, `agents` / `agent_templates`.

> Windows console gotcha: transcripts contain emoji/Unicode. Run Python with `-X utf8` and wrap
> stdout in a UTF-8 writer with `errors="replace"`, or the dump crashes on `cp1252`.

## 2. The diagnostics + server logs — `data/db/logs.db`

The transcript often shows a **vague** failure (e.g. a tool returns `error` with an empty result,
or a generic `500`). The actual cause — including the **Python traceback** — is in the logs.

- **`diagnostics` table** is the best tool here. Filter by `level in ('error','critical')` and a
  time window around the failure (and/or by `session_id`). The `detail` column holds the full
  traceback, the failing `where` (file:line), and structured context. This is where a "500" turns
  out to really be a "401 Not authenticated", a DB lock, a missing column, etc.
- **`tool_executions` table** records each tool call's `success`, `duration_ms`, `error_type`,
  `error_message`, and a `output_preview` — good for spotting which tool failed and why.

## 3. Correlate, then read the code

Once the transcript tells you *where* the chain broke and diagnostics tells you *why*, open the
relevant source to confirm and fix. Watch for **disguised errors**: fork/background work often
swallows the real exception and surfaces a bland status — always cross-check diagnostics for the
underlying traceback before concluding it's a model/prompt problem vs. an app bug.

## Quick recipe

1. Dump `interactions` for the `session_id` → read the turn-by-turn flow, find the broken step.
2. Query `diagnostics` (errors, same time window / session) → get the real traceback behind any
   vague tool error or HTTP 500.
3. Check `tool_executions` for the specific failing tool if needed.
4. Decide: model/skill issue (instructions) vs. ability/core logic (code) — then read that source.

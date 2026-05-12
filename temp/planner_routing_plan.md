# Plan: Moving Optimizer to Subagent Routing

We need to flip the optimizer paradigm from "background automated string-processing loop" to "agentic subagent routing".

1. **Routing Override in `app/api/chat.py`:**
When `POST /api/v1/chat` receives a message, check `request.session_id.startswith('optimizer-')`.
If YES:
 - This is not a normal chat. We are talking to the optimizer subagents.
 - Query `interactions` table or `sessions.metadata` to see who currently holds the talking stick (Planner or Finalizer).
 - By default, assign to `planner` agent ID.

2. **Special Agents setup:**
We need dedicated rows in `agents` table for the Planner and Finalizer.
They need specific IDs (e.g. `opt_planner_<user_id>`, `opt_finalizer_<user_id>`).
They get specific `system_prompt`s loaded from `context_templates` (the ones we just fixed!).
They get specific tools scoped to them.

3. **Planner Tools:**
- `run_worker_trials(changes_json)`: The planner calls this tool instead of returning JSON. The tool runs the workers and returns the results into the chat string!
- `handoff_to_finalizer(summary)`: The planner calls this. The backend updates the session routing to point to the Finalizer agent.

4. **Finalizer Tools:**
- `deploy_changes(changes_json)`: Edits the context documents / base agent system prompt.
- `reject` or `finish`: Closes the session.

5. **Starting the Session (Trigger):**
When the user clicks "Run Now" in the UI (or hits `run_optimizer` tool), instead of `run_optimizer_async` doing the whole job, it simply:
- Creates a new `optimizer-xxxx` session.
- Creates the `opt_planner` and `opt_finalizer` agents if they don't exist.
- Formats the prefilter data as the very first SYSTEM or USER message in that session so the Planner has the context.
- Programmatically calls `POST /api/v1/chat` internally using an initial payload.
- Returns the `session_id` so the UI redirects the user there.


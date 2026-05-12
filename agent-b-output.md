# Agent B — Race Engine Output

## Changes made

### File: `app/agent/loop.py`

**1. Added `_race_llm_calls()` async generator function** (line 78)

- Takes `messages`, `tool_definitions`, `multi_providers` (list of provider configs)
- Launches N parallel asyncio tasks, each with its own `AsyncOpenAI` client
- Each task streams from its provider into a shared `asyncio.Queue`
- First provider to emit a content chunk is declared **winner**
- Winner's chunks are yielded as `{"type": "stream", ...}` events live
- When winner finishes, all other tasks are cancelled via `task.cancel()`
- If ALL providers fail, yields `{"type": "error", ...}` with concatenated error messages
- Final result yielded as `{"type": "pipeline", "step": "parallel_complete", ...}` with: content, tool_calls, provider, model, tokens, cost

**2. Modified `stream_agent_events()` LLM call section** (line ~570)

- Added check for `PARALLEL_MODE=true` env var + `MULTI_PROVIDERS` JSON array
- When both present and ≥2 providers, dispatches to `_race_llm_calls()` instead of `_get_client()`
- Iterates race generator events, collecting `collected_content` + `collected_tool_calls`
- Updates `model_name` and `provider_name` from the winner for metadata logging
- Falls back to original single-provider path when parallel mode is off or <2 providers configured
- Error event from race → returns early, same as original single-provider error path

### Cleanup

- Removed orphaned `_get_multi_clients()` function (leftover from parallel agent collision)

## Design decisions

| Decision | Why |
|----------|-----|
| **First content chunk wins** (not first complete response) | Best UX for streaming — user sees tokens live from fastest responder |
| **Each provider gets own AsyncOpenAI client** | Isolation — timeouts, auth, and failures don't interfere |
| **Queue-based architecture** | Clean separation of producer tasks from consumer; avoids lock contention |
| **30s per-provider timeout** | Lower than the 60s single-provider timeout — fast fail for unresponsive providers |
| **Yields same event types** | Existing code after the LLM call (tool handling, response, DB persistence) works unchanged |
| **Env var activation** | Config is set by Agent A's config storage; race engine just reads env vars |

## Config protocol

The race engine reads two env vars set by `load_provider_for_user()`:
```
PARALLEL_MODE=true
MULTI_PROVIDERS=[{"provider":"openrouter","base_url":"...","api_key":"...","model":"..."},...]
```

When Agent A's config storage saves a user's parallel provider list, it writes these env vars. The race engine picks them up on the next turn. No direct coupling between the modules.

## Error handling

| Scenario | Behavior |
|----------|----------|
| All providers 401/timeout | Yields error with first 3 unique error messages |
| One provider fails, another wins | Loser silently dropped; winner streamed normally |
| One provider hangs past 30s | Timeout kills it; other providers race continues |
| All tasks cancelled mid-race | CancelledError silently absorbed; no side effects |

## Validation

- File passes `ast.parse()` — no syntax errors
- `_race_llm_calls` recognized as async generator with 5 yield points
- `PARALLEL_MODE` and `MULTI_PROVIDERS` env var checks present in `stream_agent_events()`
- Single-provider fallback path preserved unchanged

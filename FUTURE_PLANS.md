# Future Plans — webAgent

Current gaps vs pi (coding agent) and Hermes (full agent platform).
Ordered by impact, highest first.

---

## 1. Parallel Tool Execution

**What we have:** Sequential — one tool call at a time. Agent waits for result, then re-calls LLM for next turn.

**What we need:** Execute independent tool calls concurrently. If the LLM emits 3 tool calls, run them in parallel and collect results before the next LLM turn.

**Why:** ~3x faster for multi-tool tasks (e.g. read 3 files, search 2 DBs, hit 2 APIs).

**Reference:** Both pi and Hermes support parallel tool execution. pi uses `Promise.all()`, Hermes has parallel + sequential modes configurable per tool.

---

## 2. Streaming Response (Default)

**What we have:** Simple loop blocks until the complete response is ready, returns JSON. A separate streaming generator exists but isn't the primary path.

**What we need:** Default streaming — emit tokens as they arrive from the LLM. The caller (browser, CLI, API client) gets real-time output instead of waiting for the full turn.

**Why:** Perceived latency drops from "wait 10-30s for full response" to "first token in <1s". Essential for chat UX.

**Reference:** Hermes uses streaming delta callbacks. pi's `EventStream` emits `message_start/message_delta/message_end` events as tokens arrive.

---

## 3. Retry & Self-Correction Loop

**What we have:** If a tool call fails (invalid tool, malformed JSON, API 500) → log error → return error in response. Agent stops.

**What we need:** Three-tier retry — invalid tool names get fed back to the model for self-correction, malformed JSON silently re-calls the LLM, empty responses retry up to 3 times, tool execution errors retry with backoff. After tool calls, if the next response is empty, nudge the model to continue.

**Why:** Models hallucinate tool names and malform JSON frequently. Without retry, one bad tool call ends the session.

**Reference:** Hermes has 5 retry counters (invalid tool, invalid JSON, empty content, incomplete scratchpad, thinking prefill) plus a post-tool-empty nudge.

---

## 4. Credential Rotation

**What we have:** API key loaded once from env at module level. Never refreshed. If key expires or is revoked mid-session → crash.

**What we need:** Re-resolve credentials before each request (or at least before each LLM call). Support environment variable changes, key rotation providers, and token refresh.

**Why:** API keys expire. Rate-limit bypass keys are rotated. Long-running agents die on key expiry.

**Reference:** Hermes re-resolves credentials every turn via `_ensure_runtime_credentials()`, detects changes, and invalidates the cached agent if credentials changed.

---

## 5. Fallback Model Chain

**What we have:** Single model via env var. If the provider is down or the model returns errors → agent fails.

**What we need:** Ordered fallback chain — if primary provider/model fails, try next in line (e.g. OpenRouter → OpenAI Codex → Anthropic). Activation triggers include auth failure, rate-limit, connection errors, and empty responses after retry exhaustion. Fallback is per-turn (next turn tries primary again).

**Why:** If the primary provider goes down, the agent is dead until it comes back. Fallback ensures uptime.

**Reference:** Hermes supports init-time fallback (no API key for primary → try fallback at startup), per-turn credential fallback (auth error → walk chain), and runtime fallback (consistent empty responses → switch provider mid-turn).

---

## 6. Context References (@file, @diff, @folder)

**What we have:** Nothing. You paste file contents manually into the chat.

**What we need:** Expand `@file:path/to/file.py` → read file, inject contents into message. Same for `@diff` (git diff), `@folder:src/` (list/read all files), `@git:log` (git log), `@web:URL` (fetch URL). Should respect model context-length limits to avoid blowing the window.

**Why:** Massively reduces friction. User says "fix bug in @file:main.py" instead of opening, copying, pasting, then asking.

**Reference:** Hermes `agent/context_references.py` supports file, diff, folder, git, web, and glob patterns with context-length awareness.

---

## 7. Image Handling

**What we have:** No image support. Only text input.

**What we need:** Accept images (base64 or URL) in chat requests. Route to model's native vision API if supported, or fall back to LLM-based vision analysis for non-vision models.

**Why:** Many tasks require visual context — screenshots, diagrams, UI mockups, error screenshots.

**Reference:** Hermes has a two-tier decision chain: native passthrough for vision-capable models, LLM-based `vision_analyze` description generator for non-vision models.

---

## 8. System Prompt Caching

**What we have:** System prompt rebuilt from DB every single request — queries for context documents, memory, brain results, then concatenates into a new prompt string.

**What we need:** Cache the system prompt per user+session. Only rebuild when underlying context documents change (detected via DB `updated_at` timestamps). TTL-based or event-driven invalidation.

**Why:** DB query + template rendering + concatenation adds ~50-200ms per request for zero benefit when context hasn't changed.

**Reference:** Hermes caches system prompt per AIAgent session — built once, reused for all calls, only invalidated on context compression.

---

## 9. Iteration Budget (Subagent Tracking)

**What we have:** Hardcoded max turns. No awareness of subagents — if a tool spawns a sub-agent, those turns are unbounded and eat into the same counter.

**What we need:** Budget class that tracks total LLM calls across the main agent and any spawned subagents. Budget resets per-turn. When exhausted, agent stops gracefully.

**Why:** Without budget tracking, a rogue subagent could burn high API costs. The budget also enables predictable cost-per-turn.

**Reference:** Hermes has an `IterationBudget` class with per-call consumption checks. pi uses a `shouldStopAfterTurn` callback.

---

## 10. Configurable Turn Permission Gate

**What we have:** Hardcoded at 10 turns — asks "Would you like me to continue?" with hardcoded keyword matching. No way to disable, change threshold, or customize.

**What we need:** Configurable via settings — enable/disable, threshold, extension amount, max turns, custom prompt text, custom keywords. Support modes: disabled (never ask), once (ask once at threshold), every N (ask periodically), auto-extend (batch/API mode).

**Why:** Different use cases need different permission behavior. Batch processing should never prompt. Interactive chat might prompt every 10 turns.

---

## Prioritization

| Priority | Feature | Effort | Impact |
|----------|---------|--------|--------|
| P0 | Parallel tool execution | 1 day | Speed 3x, essential for multi-tool tasks |
| P0 | Streaming response | 1-2 days | UX, perceived latency drops 10x |
| P1 | Retry & self-correction | 2 days | Reliability, prevents session death |
| P1 | System prompt caching | 0.5 day | Request latency -50 to 200ms |
| P2 | Configurable permission gate | 0.5 day | UX flexibility |
| P2 | Iteration budget | 1 day | Cost control |
| P3 | Context references (@file) | 1 day | Developer UX |
| P3 | Image handling | 2-3 days | Feature parity |
| P4 | Credential rotation | 1 day | Reliability for long-running |
| P4 | Fallback model chain | 2 days | Availability during provider outages |

---

## Architecture Notes

- All new features should live in `app/agent/` as isolated modules (one file per feature).
- The streaming loop should become the single entry point. The simple loop gets deprecated.
- Features should be feature-flagged via user settings in DB. Individual users can opt in/out.
- Retry counters, fallback state, and iteration budget should be per-request (not global) to avoid cross-user state leaks.
- Tool execution errors should include structured error info (error type, recoverable bool, hint string) so the frontend can display appropriate UI.

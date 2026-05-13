# Code Context

## Files Retrieved

1. `app/db/local.py` (lines 755-840) — `fetch_interactions` and `insert_interaction` for local SQLite backend
2. `app/db/supabase.py` (lines 82-140) — `fetch_interactions` and `insert_interaction` for Supabase backend
3. `app/models/schemas.py` (lines 58-78) — `InteractionRecord` Pydantic model
4. `tests/test_session_history.py` (full file) — unit tests for `interactions_to_openai_messages`
5. `app/agent/loop.py` (lines 465-471, 558-616, 780-799, 830-900, 960-990, 1020-1045) — `messages` construction + all `insert_interaction` call sites
6. `app/api/chat.py` (lines 210-330, 475-560, 680-695) — chat API `insert_interaction` call sites
7. `app/communications/processor.py` (lines 95-100, 151-155) — Telegram/WhatsApp `insert_interaction` call sites
8. `app/api/webhooks_generic.py` (lines 95-100) — webhook `insert_interaction` call site

## Key Code

### Q1: Does `fetch_interactions` read back `input` and `output` columns?

**Yes, both backends do.**

**local.py (line 770):**
```python
rows = conn.execute(
    "SELECT id, session_id, parent_id, role, content, tool_name, tool_call_id, channel, metadata, input, output, from_id, to_id, created_at FROM interactions WHERE session_id = ? ORDER BY created_at ASC",
    (session_id,),
).fetchall()
return [InteractionRecord(**dict(r)) for r in rows]
```

**supabase.py (line 92-98):**
```python
response = (
    self._client.table("interactions")
    .select("id, session_id, parent_id, role, content, tool_name, tool_call_id, channel, metadata, input, output, from_id, to_id, created_at")
    .eq("session_id", session_id)
    .order("created_at", desc=False)
    .execute()
)
```

Both explicitly select `input` and `output` columns and hydrate `InteractionRecord` from the result.

### Q2: Does `InteractionRecord` include `input_data` and `output_data` fields?

**Yes. But the Pydantic field names are `input` and `output`, not `input_data`/`output_data`.**

`schema.py` lines 66-67:
```python
    input: Optional[str] = None           # JSON: the exact messages array sent to LLM
    output: Optional[str] = None          # JSON: the complete response/result for this interaction
```

Note the mismatch: the DB method signatures use `input_data`/`output_data` (local.py line 824, supabase.py line 126), but `InteractionRecord` uses `input`/`output`. The mapping happens at insert time — the DB methods map `input_data` → `input` column and `output_data` → `output` column, and on read, the column name `input`/`output` matches the Pydantic field directly.

### Q3: What does `tests/test_session_history.py` test?

Tests `interactions_to_openai_messages()` from `app.agent.session_history`. Four test cases:

1. **test_user_only** — single user msg → single OpenAI message
2. **test_exclude_current_user** — `exclude_interaction_ids` filters out specific interactions
3. **test_omits_internal_memory_tools** — tool rows with `tool_name="memory_search"` are stripped from output
4. **test_assistant_with_tool_calls_and_tool_results** — assistant msg with `TOOL_MARKER` + JSON spec → split into assistant msg with `tool_calls` array + tool result msg

**Notable limitation:** Test `_ir` helper does NOT set `input` or `output` fields (passes `input=None` explicitly). No tests verify what happens when `input`/`output` are populated — the field is excluded from the fixture. This means any code path using `interaction.input` or `interaction.output` in `session_history.py` is untested.

### Q4: How is `messages` initially constructed in `loop.py` (lines 465-471)?

```python
# Build message list
messages: List[Dict[str, Any]] = []
if system_prompt:
    messages.append({"role": "system", "content": system_prompt})
if history:
    messages.extend(history)
messages.append({"role": "user", "content": user_message})
```

**Yes, system prompt is included** when `system_prompt` is non-empty. The flow:
1. Start with empty list
2. Append `{"role": "system", "content": system_prompt}` if system_prompt truthy
3. Extend with `history` (from `fetch_interactions` → `interactions_to_openai_messages`)
4. Append current user message

Later, `_build_input()` (line 558) serializes the full `messages` list — so the `input` column captures the complete LLM request including system prompt, history, and user message.

Additional messages appended during the loop:
- Turn-permission request system msg (line 501)
- Permission-granted system msg (line 521)
- Assistant msg with tool_calls (line 768)
- Tool result msgs (lines 834, 879, 966)
- Final assistant msg (line 1017)

### Q5: All `insert_interaction` call sites

| # | File | Line | Role | Passes `input_data`? | Passes `output_data`? | Notes |
|---|------|------|------|---------------------|----------------------|-------|
| 1 | `app/communications/processor.py` | 97 | user | **No** | **No** | Telegram inbound |
| 2 | `app/communications/processor.py` | 153 | user | **No** | **No** | WhatsApp inbound |
| 3 | `app/api/webhooks_generic.py` | 97 | user | **No** | **No** | Generic webhook |
| 4 | `app/api/chat.py` | 219 | user | **No** | **No** | REST /chat (phase 1, non-SSE) |
| 5 | `app/api/chat.py` | 268 | tool | **No** | **No** | memory_search skip (phase 1) |
| 6 | `app/api/chat.py` | 326 | tool | **No** | **No** | memory_search results (phase 1) |
| 7 | `app/api/chat.py` | 482 | user | **No** | **No** | SSE /chat/stream phase 2 |
| 8 | `app/api/chat.py` | 517 | tool | **No** | **No** | memory_search skip (phase 2) |
| 9 | `app/api/chat.py` | 557 | tool | **No** | **No** | memory_search results (phase 2) |
| 10 | `app/api/chat.py` | 687 | tool | **No** | **No** | memory_save tool |
| 11 | `app/agent/loop.py` | 614 | assistant | **Yes** | **Yes** | Parallel loser trace log |
| 12 | `app/agent/loop.py` | 786 | assistant | **Yes** | **Yes** | Main assistant with tool_calls |
| 13 | `app/agent/loop.py` | 839 | tool | **Yes** | **Yes** | Validation error tool result |
| 14 | `app/agent/loop.py` | 883 | tool | **Yes** | **Yes** | Guardrail-blocked tool |
| 15 | `app/agent/loop.py` | 971 | tool | **Yes** | **Yes** | Successful tool execution |
| 16 | `app/agent/loop.py` | 1026 | assistant | **Yes** | **Yes** | Final assistant (no tool calls) |

**Callers missing `input_data`/`output_data`:** All API-level callers (chat.py, processor.py, webhooks_generic.py) at lines 1-10. These insert user/tool interactions before/outside the agent loop, where input/output capture doesn't apply conceptually — the agent loop is the only place the full LLM messages array exists.

## Architecture

```mermaid
flowchart LR
    A[app/api/chat.py] -->|user msg| B{get_db()}
    B --> C[local.py / supabase.py]
    
    D[app/agent/loop.py] -->|system prompt + history + user msg| E[messages list]
    E -->|_build_input()| F[json.dumps(messages)]
    F -->|input_data| G[insert_interaction]
    G --> H[(interactions DB)]
    
    H -->|fetch_interactions| I[InteractionRecord\ninput + output fields]
    I -->|interactions_to_openai_messages| E
```

**Data flow:** `loop.py` owns the `messages` list. On each assistant/tool interaction, it snapshots `messages` via `_build_input()` → `input` column, and the LLM response/result → `output` column. On session resume, `fetch_interactions` reads back both columns → `InteractionRecord.input` + `.output` → `session_history.py` maps to OpenAI format.

**Implied but unused:** The `input`/`output` columns are stored but `session_history.py` doesn't reference them in its tests. They serve as audit/logging trace, not as input to the message reconstruction logic (which uses `role`, `content`, `tool_name`, `tool_call_id`, and `TOOL_MARKER` parsing).

## Start Here

**`app/agent/loop.py` lines 465-471 and 558-559** — the `messages` construction and `_build_input()` are the pivot point. See how the full LLM messages array (including system prompt) is serialized to `input_data`, and trace forward to all `insert_interaction` calls (lines 614, 786, 839, 883, 971, 1026) that pass it.

## Risks / Open Questions

1. **Empty `input`/`output` for non-loop callers.** All API-level interactions (chat.py user msgs, processor.py, webhooks) store NULL in `input`/`output`. Is this intentional? If downstream code suddenly requires these fields, those rows will have NULLs.
2. **Test gap.** `test_session_history.py` never constructs `InteractionRecord` with populated `input`/`output`. Any code path reading those fields from fetched interactions runs untrusted in tests.
3. **Parallel loser path** (line 614) uses `inp = json.dumps(messages)` — same snapshot approach as the main path. The loser agent's `_build_input()` snapshot happens at the same point in the loop as the winner's, but the loser's messages timeline may differ if the winner already appended messages. This is a potential inconsistency: the `input` column for a loser interaction reflects loser's view of `messages`, not the winner's.

# Worker Trial Tools Investigation

## 1. How the main agent loop passes tool definitions to the LLM

In `app/agent/loop.py`, the `stream_agent_events()` function:

**Step A: Load tools from DB + builtins**
```python
tools = await load_tools(user_id)  # line ~534
```
`load_tools()` (`app/tools/loader.py`) queries the `tools` table (Supabase via `_fetch_user_tools`) then injects ~30 built-in tools via `_inject_builtin_tools()` — including `get_time`, `get_date`, `get_weather`, `calculate`, `web_search`, `browser_action`, `db_query`, `memory`, `session_search`, `list_tools`, `search_tools`, `http_request`, etc.

**Step B: Build tool_definitions array**
```python
tool_definitions = []
for name, info in tools.items():
    description = info.handler.__doc__ or f"Execute {name}"
    tool_definitions.append({
        "type": "function",
        "function": {
            "name": name,
            "description": description.split("\n")[0],
            "parameters": info.parameters or {"type": "object", ...},
        },
    })
```
This creates the OpenAI/OpenRouter-compatible `tools` payload.

**Step C: Pass to LLM call**
```python
stream = await _get_client().chat.completions.create(
    model=model_name,
    messages=messages,
    tools=tool_definitions if tool_definitions else None,  # <--- KEY
    tool_choice="auto" if tool_definitions else None,
    ...
)
```

The `tools` parameter tells the LLM what functions it can call. Without it, the LLM only generates plain text.

## 2. Why worker trial agents don't have those tools

In `app/tools/optimizer_tools.py`, the `run_worker_trials()` function's LLM call:

```python
messages = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": original_message},
]
response = await _llm_client.chat.completions.create(
    model=llm_model,
    messages=messages,
    temperature=0.0,
    max_tokens=4096,
    stream=False,
)
```

**No `tools` parameter.** No `tool_choice`. The LLM only sees a system prompt + user message. It has no idea `get_time`, `get_date`, `web_search`, etc. exist as callable functions.

Two things are missing:

1. **No `tools` in the LLM API call** — the OpenAI/OpenRouter API receives zero function definitions. The LLM physically cannot emit a `tool_calls` response.

2. **No tool loading** — `run_worker_trials` never calls `load_tools()`. It builds a system prompt via `build_system_prompt()` (which only adds a text description like "You have core tools always available: get_time, get_date, ...") but that's just prose — it does not register those as actual function-calling tools.

The system prompt's `[BOOTSTRAP TOOLS]` section says "You have core tools always available" — but this is only narrative. Without tool definitions in the API payload, the LLM treats that as background context, not as callable functions.

## 3. What exactly needs to be in the temp DB for tools to work

Currently the worker trial creates a temp SQLite DB with `SCHEMA_SQL` and inserts:
- Agent record (in `agents`)
- Context documents (in `context_documents`)
- Session record (in `sessions`)
- Interactions (in `interactions`)

But it does NOT populate the `tools` table. And even if it did, `load_tools()` uses `_fetch_user_tools()` which queries the Supabase REST API (`client.table("tools").select(...).execute()`), not SQLite. The temp DB's `tools` table is never read by `load_tools()`.

**What the temp DB would need:**
- A populated `tools` table with rows for `get_time`, `get_date`, `get_weather`, `calculate`, `web_search`, `list_tools`, `search_tools`, `db_query`, `memory`, `session_search`, `http_request`, `read_attachment`, `browser_action`, `register_user`, `rate_skill`, etc.
- Each row needs: `name`, `code`, `description`, `parameters`, `status='active'`, `created_by=<user_id>`

**What `load_tools()` actually needs:**
- Input: a `user_id` string
- `_fetch_user_tools(user_id)` → hits Supabase REST API at the configured Supabase URL, NOT the local SQLite. The worker trial has no Supabase connection configured.
- `_inject_builtin_tools(tools, user_id)` → adds ~30 hardcoded built-in tools

Even if the temp DB had a tools table, the current `_fetch_user_tools` code would ignore it — it's hardcoded to use the Supabase REST API client.

## 4. The simplest fix: pass tool definitions directly in the LLM call

**Option A (simplest): Pass tool definitions directly**

In `run_worker_trials()`, after building `system_prompt`, add:

```python
from app.tools.loader import load_tools

# Load tools for the real user (gets DB tools + builtins)
real_tools = await load_tools(real_user_id)

# Build tool_definitions same as loop.py
tool_definitions = []
for name, info in real_tools.items():
    description = info.handler.__doc__ if info.handler.__doc__ else f"Execute {name}"
    description = description.split("\n")[0]
    tool_definitions.append({
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": info.parameters if hasattr(info, 'parameters') else {"type": "object", "properties": {}, "required": []},
        },
    })

# Pass tools to the LLM call
response = await _llm_client.chat.completions.create(
    model=llm_model,
    messages=messages,
    tools=tool_definitions,  # <--- ADD THIS
    tool_choice="auto",       # <--- ADD THIS
    temperature=0.0,
    max_tokens=4096,
    stream=False,
)
```

**Option B (more complex): Register tools in temp DB + make load_tools read them**

This requires:
1. Populating the `tools` table in the temp DB with all rows
2. Making `_fetch_user_tools()` fall back to local SQLite when Supabase is unavailable
3. Configuring `get_raw_client()` to point at the temp DB during worker trials

Option B touches more code, needs Supabase-to-SQLite adaptation logic, and adds surface area for bugs.

**Recommendation: Option A.** It's:
- 10 lines added to `run_worker_trials()`
- Same pattern already proven in `loop.py`
- No schema/DB changes
- No fallback logic
- Worker trials get exactly the same tools the real agent has

## Additional consideration: streaming + tool_choice

The worker trial currently uses `stream=False`. The main loop uses `stream=True`. Both should work fine — the `tools` and `tool_choice` parameters are independent of streaming mode. `stream=False` will return tool calls in `response.choices[0].message.tool_calls` instead of streaming deltas.

The worker trial would need to handle the `response.choices[0].message.tool_calls` field and possibly execute those tools to get meaningful results (e.g., calling `get_time(timezone="America/Detroit")` and seeing the actual response). Currently the worker trial just reads the raw reply text — it would need to add a tool execution loop like `loop.py` has to properly test tool-using agents.

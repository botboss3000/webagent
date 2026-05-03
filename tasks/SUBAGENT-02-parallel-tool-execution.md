# Sub-Agent Task: Parallel Execution for Independent Tools

## Goal

Add parallel (concurrent) tool execution capability so that independent tool calls in the same LLM turn run simultaneously rather than sequentially. This reduces total wall-clock time when the LLM issues multiple tool calls that don't depend on each other.

## Files to Modify

- `app/agent/streaming_loop.py` — streaming agent loop (primary target)
- `app/agent/loop.py` — non-streaming agent loop (same changes)

## Current State

Both loops execute tool calls **sequentially** in a `for` loop:

```python
# streaming_loop.py (lines ~106-144)
for idx, tc in sorted(collected_tool_calls.items()):
    # execute one tool at a time...
    result = await tools[tool_name](**tool_args)
    # append result...
```

```python
# loop.py (lines ~114-151)
for tool_call in assistant_message.tool_calls:
    # execute one tool at a time...
    result = await tools[tool_name](**tool_args)
    # append result...
```

If the LLM calls 3 tools, they run one after another. If each takes 2 seconds, total = 6 seconds. With parallel execution, total ≈ max(2, 2, 2) = 2 seconds.

## Requirements

### 1. Determine Tool Independence

Before executing, build a dependency graph:
- By default, ALL tool calls in a single LLM response are **independent** (they reference different data sources)
- No tools share state or depend on each other's output within the same turn
- Exception: if the tool system is later extended to support tool-to-tool data piping, this gets more complex. For now, assume all tools in one batch are independent.

### 2. Parallel Execution Strategy

Use `asyncio.gather()` to run all tools concurrently:

```python
import asyncio

async def execute_tool_batch(tool_calls: List, tools: Dict) -> List[ToolResult]:
    """Execute a batch of independent tool calls concurrently."""
    
    async def execute_single(name, args, tc_id):
        try:
            result = await tools[name](**args)
            return ToolResult(tc_id=tc_id, tool=name, result=str(result), success=True)
        except Exception as e:
            return ToolResult(tc_id=tc_id, tool=name, result=f"Error: {e}", success=False)
    
    tasks = [execute_single(name, args, tc.id) for name, args, tc.id in tool_calls]
    return await asyncio.gather(*tasks)
```

### 3. Result Ordering

- `asyncio.gather()` preserves insertion order — results arrive in the same order as the tool calls
- BUT: we need to emit `tool_result` events **in the original source order** (by `tc.index`) so the frontend displays them consistently
- Solution: collect all results first, then emit events in sorted index order (already done in streaming_loop.py with `sorted(collected_tool_calls.items())`)

### 4. Streaming Event Yielding

In `streaming_loop.py`, the current pattern is:
```
for each tool_call (sorted):
    yield tool_call event
    execute tool
    yield tool_result event
    append to messages
```

New pattern:
```
emit all tool_call events (sorted)
execute ALL tools concurrently via asyncio.gather()
emit all tool_result events (sorted)
append all results to messages
```

This means the frontend sees all tool_call events immediately, then all results arrive together.

### 5. Non-Streaming Loop

In `loop.py`, the change is simpler — just replace the sequential `for` loop with `asyncio.gather()`:

```python
# Before:
tool_responses = []
for tool_call in assistant_message.tool_calls:
    result = await tools[tool_name](**tool_args)
    tool_responses.append({...})

# After:
async def execute_one(tc):
    result = await tools[tc.function.name](**args)
    return {"role": "tool", "content": str(result), "tool_call_id": tc.id}

tool_responses = await asyncio.gather(*[execute_one(tc) for tc in assistant_message.tool_calls])
messages.extend(tool_responses)
```

## Acceptance Criteria

- [ ] Multiple tool calls in a single LLM turn execute concurrently (not sequentially)
- [ ] Results are emitted/appended in the original source order (by index)
- [ ] `streaming_loop.py` yields all `tool_call` events before results arrive
- [ ] `loop.py` extends messages in correct order
- [ ] Error handling per tool still works — one failing tool doesn't block others
- [ ] Tracking/database writes happen per tool (not batched)
- [ ] Total wall-clock time for N independent tools ≈ max(N), not sum(N)

## Edge Cases

- **Zero tool calls**: No-op, proceed to post-turn checks
- **One tool call**: No parallelism needed, just execute normally
- **Tool throws immediately** (sync exception in `await tools[name](**args)`): The individual task in `gather` catches it and returns an error result — other tools proceed unaffected
- **Mix of fast and slow tools**: Fast results are held until all complete, then emitted in order. If you want progressive results, use `asyncio.as_completed()` instead of `gather()`, but this complicates ordering. Decision: use `gather()` for simplicity and correct ordering.
- **Rate limits**: Running 10 tools concurrently could hit external API rate limits. Consider adding a semaphore (`asyncio.Semaphore(3)`) to cap concurrency at 3-5 tools simultaneously.

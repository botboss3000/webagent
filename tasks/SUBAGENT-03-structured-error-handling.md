# Sub-Agent Task: Structured Error Handling — Send Errors Back to LLM for Retry

## Goal

When a tool execution fails (either validation failure or runtime exception), instead of just logging the error and appending a plain text error message, return a **structured error payload** that gives the LLM enough context to self-correct and retry the tool call with fixed arguments.

## Files to Modify

- `app/agent/streaming_loop.py` — streaming agent loop
- `app/agent/loop.py` — non-streaming agent loop
- `app/tools/tracker.py` — execution tracking

## Current State

When a tool fails, the code does:

```python
# streaming_loop.py
except Exception as e:
    result_str = f"Error: {e}"           # simple error string
    success = False
                               
yield {"type": "tool_result", "result": result_str[:2000], "error": not success}

messages.append({
    "role": "tool", 
    "content": result_str[:10000],        # raw error string back to LLM
    "tool_call_id": tc.id,
})
```

The LLM gets a plain text error like `"Error: division by zero"` or `"Error executing tool web_search: Connection timeout"`. There's no structure — no error type, no retry guidance, no hint about what to fix.

## Requirements

### 1. Error Classification

Create a helper module or function `classify_tool_error(e, tool_name, args)` that categorizes errors:

```python
@dataclass
class ToolError:
    error_type: str          # One of: "validation_error", "runtime_error", "timeout", "auth_error", "rate_limit", "not_found", "internal_error"
    tool_name: str
    message: str             # Human-readable summary
    recoverable: bool        # Can the LLM fix this and retry?
    retry_hint: str          # Guidance for the LLM on what to change
    details: Optional[dict]  # Extra context (status codes, field names, etc.)
```

Error classification rules:
| Error Signature | Type | Recoverable | Hint |
|---|---|---|---|
| `KeyError`, `TypeError` from mismatched args | `validation_error` | Yes | "Check required parameters and types" |
| `TimeoutError`, `asyncio.TimeoutError` | `timeout` | Yes | "The tool timed out. Try with a smaller scope." |
| HTTP 429 / rate limit message | `rate_limit` | Yes | "Rate limited. Wait a moment, then retry." |
| HTTP 401 / 403 / "unauthorized" | `auth_error` | No | "Authentication required. Ask the user to set up credentials." |
| HTTP 404 | `not_found` | Yes | "The resource was not found. Check the path/URL." |
| `FileNotFoundError` | `not_found` | Yes | "File not found. Check the path." |
| Any other exception | `runtime_error` | Maybe | "An unexpected error occurred. Try a different approach." |

### 2. Structured Error Payload for LLM

Instead of a plain string, return a **JSON object** as the tool result content:

```python
tool_result_content = json.dumps({
    "status": "error",
    "error_type": error.error_type,
    "tool": error.tool_name,
    "message": error.message,
    "recoverable": error.recoverable,
    "hint": error.retry_hint,
    "details": error.details,
})
```

This gives the LLM:
- Clear signal that the tool failed (not a result)
- What kind of error it was
- Whether it can retry
- Guidance on what to fix

### 3. Retry Decision Logic

In the agent loop, after appending the error tool result, the LLM will see it on the **next turn** of the loop and decide:
- Fix arguments → retry the same tool
- Try a different tool
- Abort and explain to the user

**Do NOT auto-retry in the Python code.** The LLM should make the retry decision itself. The structured error is information for the LLM to act on.

However, add a **retry budget** to prevent infinite error loops:

```python
# In the agent state
error_retries = 0
MAX_ERROR_RETRIES = 3  # per turn

# After appending error message:
if not error.recoverable or error_retries >= MAX_ERROR_RETRIES:
    # Add a system-like note telling LLM to stop retrying this tool
    messages.append({
        "role": "tool",
        "tool_call_id": tc.id,
        "content": json.dumps({
            "status": "fatal",
            "message": f"Tool '{tool_name}' failed with an unrecoverable error after {MAX_ERROR_RETRIES} attempts. Do not retry. Explain the issue to the user.",
        })
    })
```

### 4. Update Error Event for Frontend

In `streaming_loop.py`, the `tool_result` event currently sends:

```python
yield {
    "type": "tool_result",
    "tool": tool_name,
    "result": result_str[:2000],
    "duration_ms": duration_ms,
    "error": not success,
}
```

Change to also include the structured error info:

```python
yield {
    "type": "tool_result",
    "tool": tool_name,
    "result": result_str[:2000],
    "duration_ms": duration_ms,
    "error": not success,
    "error_type": error.error_type if not success else None,
    "recoverable": error.recoverable if not success else None,
}
```

### 5. Track Error Metrics

Update `app/tools/tracker.py` to log the error type classification:

```python
await track_execution(
    ...
    error_message=error.message,           # was: raw error string
    error_type=error.error_type,           # NEW field
    recoverable=error.recoverable,          # NEW field
)
```

This requires adding `error_type` and `recoverable` fields to the `track_execution` function and the database schema, OR encoding them into `error_message` as a JSON object.

## Acceptance Criteria

- [ ] All tool errors are classified into one of the error types above
- [ ] The LLM receives structured JSON errors (not plain strings) for failed tools
- [ ] Recoverable errors include a `hint` field with guidance
- [ ] Unrecoverable errors signal the LLM to stop retrying
- [ ] The frontend receives `error_type` and `recoverable` fields in `tool_result` events
- [ ] Error retry budget (max 3 per tool per turn) prevents infinite loops
- [ ] Both `streaming_loop.py` and `loop.py` implement the same error handling

## Edge Cases

- **Tool crashes with system-level error** (segfault, OOM): Caught by the `except Exception` in the loop → classified as `internal_error`
- **Tool returns valid result but the result is an error string**: Not our problem — the tool should raise, not return error strings
- **LLM ignores error hints and retries with the same broken args**: The retry budget catches this — after 3 failures, it's marked `fatal`
- **Multiple tools fail in one batch**: Each tool gets its own structured error; the LLM sees all of them and decides next steps
- **Validation error AND runtime error on same tool call**: Validation happens first, so only validation error is returned

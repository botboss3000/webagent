# Sub-Agent Task: Tool Validation (Name Check + JSON Args Validation)

## Goal

Add a validation step before tool execution in both `streaming_loop.py` and `loop.py` that verifies:
1. The tool name exists in the loaded tools dictionary
2. The JSON arguments are valid and match the tool's expected schema

This prevents malformed tool calls from reaching the execution step and gives the LLM clear, structured error messages so it can self-correct.

## Files to Modify

### Primary
- `app/agent/streaming_loop.py` — streaming agent loop
- `app/agent/loop.py` — non-streaming agent loop

### Supporting (may need updates)
- `app/tools/loader.py` — enrich tool metadata (schemas)
- `app/models/schemas.py` — may need a `ToolValidationResult` model

## Current State

In `streaming_loop.py`, the tool name is checked with a simple `if tool_name not in tools:` and JSON parsing is done with a bare `try/except json.JSONDecodeError`. No schema validation exists.

In `loop.py`, same pattern — name check and bare JSON parsing.

The tool definitions sent to the LLM currently have **empty schemas**:
```python
"parameters": {"type": "object", "properties": {}, "required": []}
```

This needs to be fixed first so validation has a schema to validate against.

## Requirements

### 1. Enrich Tool Definitions (loader.py)

Modify `app/tools/loader.py` so that `load_tools()` returns not just `Dict[str, Callable]` but also provides the parameter schemas. Options:
- Add a `ToolInfo` dataclass with `name`, `handler`, `parameters` fields
- OR have tools store their schema in a `__tool_schema__` attribute
- OR return a `Dict[str, ToolInfo]` instead of `Dict[str, Callable]`

**Minimum `ToolInfo` shape:**
```python
@dataclass
class ToolInfo:
    name: str
    handler: Callable  # the async function
    parameters: dict   # JSON Schema dict: {"type": "object", "properties": {...}, "required": [...]}
```

### 2. Send Real Schemas to LLM (loop.py + streaming_loop.py)

Both loops currently build tool definitions with empty `properties: {}`. Change them to use the actual parameter schema from the enriched tool info.

### 3. Pre-Execution Validation (loop.py + streaming_loop.py)

Add a `validate_tool_call(name, args, tools_info)` function that:

```python
async def validate_tool_call(name: str, args: dict, tools: Dict[str, ToolInfo]) -> ToolValidationResult:
```

Returns:
```python
@dataclass
class ToolValidationResult:
    is_valid: bool
    error_message: Optional[str] = None
    corrected_name: Optional[str] = None
    corrected_args: Optional[dict] = None
```

Validation checks:
1. **Name check**: Does `name` exist in `tools`? If not, try case-insensitive match or fuzzy match (Levenshtein distance ≤ 2) against known tool names. If found, set `corrected_name`.
2. **JSON args type**: Are args a dict? (Not list, string, None)
3. **Required params**: Does `args` contain all keys listed in `parameters.required`?
4. **Unknown params**: Warn but don't reject — the LLM may pass extra context.
5. **Param types**: For each param in `parameters.properties`, if it declares a `type`, verify the value is of that type (basic: string, number, boolean, array, object).

### 4. Structured Error Response

When validation fails, instead of just yielding an error, return a structured message back to the LLM:

```python
{
    "role": "tool",
    "tool_call_id": tc.id,
    "content": json.dumps({
        "status": "validation_error",
        "tool": tool_name,
        "issues": [
            {"field": "name", "message": "Unknown tool. Did you mean 'web_search'?"},
            {"field": "args.query", "message": "Missing required parameter 'query'"},
        ],
        "hint": "Check the tool schema and retry with corrected arguments."
    })
}
```

The LLM will see this and self-correct on the next turn.

## Acceptance Criteria

- [ ] `load_tools()` returns enriched tool info with parameter schemas
- [ ] Tool definitions sent to LLM include real `parameters.properties` (not empty)
- [ ] Unknown tool names are fuzzy-matched and corrected (with a note in the response)
- [ ] Missing required params produce clear error messages listing what's missing
- [ ] Type mismatches (string vs int, etc.) are caught and reported
- [ ] All validation errors are returned as structured tool messages, not just logged
- [ ] Both `streaming_loop.py` and `loop.py` implement the same validation
- [ ] No breaking changes to existing tool handlers

## Edge Cases

- Tool name with extra whitespace or punctuation → trim and match
- LLM sends args as a JSON string instead of object → parse and validate
- Multiple tools in one batch, one fails validation → execute valid ones, return errors for invalid ones
- All tools fail validation → return all errors, let LLM decide

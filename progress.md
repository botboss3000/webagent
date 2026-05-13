# Progress

## 2026-05-12: FK-based agent lookup — no more inference chain

### The bug
After session binding, `chat.py` still called `fetch_agent_with_context(ctx_user_id)` which re-resolved the agent by a synthetic `user_id` (naming convention `opt_planner_test_user`). If that lookup failed (stale data, race, wrong user), it silently fell back to the default webAgent.

### The fix
`chat()` and `chat_stream()` now call a single method:
```python
agent = await db.get_or_resolve_session_agent(
    session_id=request.session_id,
    user_id=request.user_id,
    template_id=opt_template_id,
)
```
This method:
1. Checks `sessions.agent_id` — if set, does direct FK lookup by agent ID (no inference)
2. If not set: resolves via priority chain (agents -> templates -> .json -> error), materializes, binds to session, returns

Removed from `chat.py`:
- Inline `resolve_agent()` + manual `INSERT INTO agents`
- `fetch_agent_with_context(ctx_user_id)` — re-resolved by user_id
- Manual `bind_session_to_agent()` — handled inside `get_or_resolve_session_agent`
- `copy_defaults_to_agent()` for optimizer sessions — isolation gate handles it

### Verified
- Normal chat: works
- Optimizer session: agent has `template_id=opt_planner`, zero webAgent in prompt, session.agent_id matches agent PK

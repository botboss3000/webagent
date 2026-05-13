# FK-based agent lookup — implemented

## Changes

### app/db/interface.py
- Added `fetch_agent_by_id_with_context(agent_id, context_types)` abstract method
- Added `get_or_resolve_session_agent(session_id, user_id, template_id)` abstract method

### app/db/local.py
- Implemented `fetch_agent_by_id_with_context()` — same as `fetch_agent_with_context()` but WHERE `a.id = ?` (PK) instead of `a.user_id = ?`. No naming convention, no inference chain.
- Implemented `get_or_resolve_session_agent()` — single entry point:
  1. If `sessions.agent_id` set → `fetch_agent_by_id_with_context(agent_id)` (direct FK)
  2. If not → `resolve_agent()` → materialize if template → `bind_session_to_agent()` → `fetch_agent_by_id_with_context(agent_id)`

### app/db/supabase.py
- Implemented `fetch_agent_by_id_with_context()` — Supabase version, `.eq("id", agent_id)`
- Implemented `get_or_resolve_session_agent()` — delegates to local backend
- Added static method wrappers in `SupabaseClient`

## Next step
Refactor `app/api/chat.py` to replace the two separate lookups (first by user_id, then by ctx_user_id) with a single call to `get_or_resolve_session_agent()`. That's where the stale/default agent leak happens today.

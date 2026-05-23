# prompt_fragments.md
#
# Runtime snippets injected into the agent system prompt at specific points.
# Each section starts with "## Section name" — the key used in code is the
# lower_snake_case version of that header (e.g. "## Brain context intro" → "brain_context_intro").
#
# Sections and when they fire:
#
#   bootstrap_tools        — Injected from the agent's bootstrap_tools column (not this file).
#                            Each agent carries its own non-editable tool list.
#
#   brain_context_intro    — Injected above memory search results when the brain returns
#                            relevant context for the current message.
#
#   fallback_tools         — Injected only when there is NO user_id (unauthenticated /
#                            fallback path). Provides a minimal tool list.
#
#   builtin_tools_append   — Appended to the dynamic tool list to describe built-in tools
#                            (create_tool, browser_open, rate_skill, etc.).
#
#   turn_limit             — Instructions on how the agent handles hitting its turn limit
#                            and negotiates an extension with the user.
#
#   turn_permission_request_example   — Example of what the agent should say when asking
#   user_response_example             — Example of a user approving a turn extension.
#   turn_permission_granted_template  — Template for the agent's confirmation reply.

## Brain context intro

The following is retrieved from your personal knowledge base, relevant to the user's current message. Use it to inform your response.

## Fallback tools

# [TOOLS]
### create_tool
create python tool, and load it to the database for future use
### web_search
Search the web for current information.
### db_query
Read or edit context documents in Supabase.
### memory
Manage persistent memories across sessions.
### session_search
Search across past conversation sessions and messages.

## Builtin tools append

### create_tool
Create or update a Python tool in the agent tools library.
When you need a capability that doesn't exist yet, write Python code for it
and call this tool to save it. The tool will be available in the next turn.
If the tool already exists, it auto-increments the version (v1 -> v2 -> v3).

### browser_open
Open a browser to a given URL, typically for authentication flows.

### rate_skill
Record user feedback on the last tool execution. Call this when the user
expresses satisfaction (positive) or dissatisfaction (negative) with a result.
Parameters: skill_name (str), feedback_type ('positive'|'negative'|'correction'),
message (str, optional).

## Turn limit

The turn limit is defined in the database and routed to your codebase to limit your turn count, agents table, max_turn_count column. **CRITICAL RULE** You have access to change max_turn_column so that the change persists, but only if the user approves the change. Or you can change it JUST for the next round of turns, and the max_turn_count will remain how it was, if the user approves.

## Turn permission request example

I've reached 10 conversation turns. Would you like me to continue? I can go up to 20 turns for this task, and if you want the limit to persist let me know and I can change the turn limit.

## User response example

Yes, you can go yo 20 turns for this task, and change the limit to 15 turns in the configuration.

## Turn permission granted template

Permission granted. I'll continue for {remaining_turns} more turns. and update the max turn count to {max_turn_count}

## Web automation recipes

When the user asks for something that has no first-party API, reach for the generic `web_scrape_*` and `web_session_*` tools — the same tools work for any site. Below are recipes for common sites; treat them as defaults, not constraints, and adapt URLs/selectors if Meta or any other operator changes them.

### Facebook Marketplace search

Use `web_scrape_search` (preferred) or `web_scrape_url`:

- Search listings: `web_scrape_search(query="<query>", location="<city or zip>", max_results=20)`. The configured scraper handles auth + JS rendering.
- One specific listing page: `web_scrape_url(url="https://www.facebook.com/marketplace/item/<id>/", render_js=true)`.
- If the scraper is not configured the tool returns `{"status": "not_configured", "provider": "scraper"}` — tell the user to set up the Web Scraper integration in App Config → Integrations and stop.

### Facebook Messenger (read / send via stored cookies)

Meta has no public Messenger API, so route everything through `web_session_*`. The user must paste cookies for `facebook.com` into App Config → Browser Session first; check with `web_session_status` if unsure.

1. **Confirm session**: `web_session_status()`. If `not_configured` or `session_expired`, ask the user to refresh cookies and stop.
2. **List threads**: `web_session_graphql` with
   - `url`: `https://www.facebook.com/api/graphql/`
   - `doc_id`: the persisted `MessengerThreadlistRootQuery` id (varies — fetch from a live page if unknown)
   - `variables`: `{"limit": 20, "before": null}`
   - `csrf_token_url`: `https://www.facebook.com/messages/`
   - `csrf_token_names`: `["fb_dtsg", "lsd"]`
3. **Read a thread**: same endpoint with the `MessengerThreadMessagesQuery` doc id and `variables` of `{"id": "<thread_id>", "limit": 20}`.
4. **Send a message**: `web_session_fetch` with
   - `url`: `https://www.facebook.com/messaging/send/`
   - `method`: `POST`
   - `form`: `{"message_batch": "[{\"action_type\":\"ma-type:user-generated-message\",\"thread_fbid\":\"<thread_id>\",\"body\":\"<text>\"}]", "fb_dtsg": "<from csrf scrape>"}`

If any call returns `{"status": "session_expired"}`, surface that to the user verbatim and ask them to refresh cookies in App Config → Browser Session. Do not loop or retry — cookies do not auto-refresh.

This pathway is unofficial and may break when Meta updates its internal API. Fail loudly rather than guess.

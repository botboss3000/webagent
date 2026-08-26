"""One-shot tools for the running WebAgent app — log in, list agents, chat.

Thin wrappers over the shared ``WebAppClient`` (see ``appclient.py``): they reuse
the app-owned client (and its cached admin session) when one is on the
``ToolContext``, else a standalone client. ``app_chat`` uses the synchronous
``POST /api/v1/chat`` path (returns the reply directly, no WebSocket needed) — for
the *live, connected* conversation the Manager uses ``webapp_send`` instead.
"""

from __future__ import annotations

from ..appclient import WebAppError, get_client
from .base import WRITES_DISABLED_MSG, ToolContext


async def app_login(ctx: ToolContext, username: str = "admin", password: str = "admin") -> str:
    """Log into the running WebAgent app and cache the session. Read-only side
    effect (just authentication). Defaults to the local admin (admin/admin)."""
    client = get_client(ctx)
    client.username, client.password = username, password
    try:
        await client.login()
    except WebAppError as e:
        return f"Error: {e}"
    ctx.audit("app_login", {"username": username}, True, client.user_id)
    return (f"[app] logged in as '{username}' (user_id {client.user_id}). "
            "You can now use app_chat / webapp_send to talk to the app's agent.")


async def app_list_agents(ctx: ToolContext, username: str = "admin", password: str = "admin") -> str:
    """List the chat agents available to the logged-in user (id + name). Read-only."""
    client = get_client(ctx)
    client.username, client.password = username, password
    try:
        agents = await client.list_agents()
    except WebAppError as e:
        return f"Error: {e}"
    if not agents:
        return "[app] this user has no custom agents yet (app_chat uses the default agent)."
    lines = [f"[app] {len(agents)} agent(s) for {client.username}:"]
    for a in agents:
        lines.append(f"  • {a.get('name', '(unnamed)')}  —  id {a.get('id', '?')}")
    return "\n".join(lines)


async def app_chat(
    ctx: ToolContext,
    message: str,
    session_id: str = "",
    agent_id: str = "",
    new_session: bool = False,
    username: str = "admin",
    password: str = "admin",
) -> str:
    """Send a chat message to the running app's agent AS the logged-in user and
    return its reply (synchronous one-shot). For an ongoing, live, shared
    conversation use the connect panel + webapp_send instead. Mutating."""
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    if not (message or "").strip():
        return "Error: a message is required."
    import uuid
    client = get_client(ctx)
    client.username, client.password = username, password
    sid = session_id or (f"tui-{uuid.uuid4().hex[:16]}" if new_session else
                         (ctx.webapp_session_id or f"tui-{uuid.uuid4().hex[:16]}"))
    try:
        reply = await client.chat_sync(sid, message, agent_id=agent_id)
    except WebAppError as e:
        return f"Error: {e}"
    ctx.audit("app_chat", {"session_id": sid, "agent_id": agent_id or "default"}, True, "")
    return (f"[app · session {sid}] the app's agent replied:\n\n{reply}\n\n"
            f"(Continue by calling app_chat with session_id='{sid}'.)")

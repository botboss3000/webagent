"""Drive the running webAgent app as a user — log in, open a chat, talk to its agent.

These tools let the Server Manager act *inside* the app it manages: authenticate
against the local server (default ``admin``/``admin``), then send chat messages to
the app's own agent and read its replies. The app auto-creates the chat session and
picks the user's default agent, so a single ``app_chat`` call is enough to "start a
session and act on the user's behalf"; the JWT + current session id are cached in
this module so follow-up calls continue the same conversation.

This talks to the app over HTTP on localhost (the same API the web UI uses) — it does
NOT reach into the database directly — so what the manager can do is exactly what a
logged-in user can do. ``app_chat`` is gated behind "Allow writes" because it makes
the app's agent *act* (it can run tools, send messages, change app state).
"""

from __future__ import annotations

from .base import WRITES_DISABLED_MSG, ToolContext

PORT = 8080
_BASE = f"http://127.0.0.1:{PORT}"

# Per-base-url session cache: {token, user_id, username, session_id}. Module-level
# so a chat conversation persists across tool calls within a manager session.
_SESSIONS: dict[str, dict] = {}


def _new_session_id() -> str:
    import uuid
    return f"tui-{uuid.uuid4().hex[:16]}"


async def _login(username: str, password: str) -> dict | str:
    """Authenticate and cache the token + user_id. Returns the cache dict or an
    error string."""
    import httpx
    try:
        async with httpx.AsyncClient(base_url=_BASE, timeout=httpx.Timeout(20.0, connect=5.0)) as c:
            r = await c.post("/api/v1/auth/login",
                             json={"email": username, "password": password})
    except httpx.ConnectError:
        return (f"Error: the webAgent server isn't reachable on {_BASE}. "
                "Start it first (Server ▸ Start) and try again.")
    except httpx.HTTPError as e:
        return f"Error contacting the server: {e}"
    if r.status_code == 401:
        return (f"Error: login refused for '{username}' (bad username/password). "
                "The default local admin is admin/admin.")
    if r.status_code == 403:
        return f"Error: account '{username}' is not approved for login."
    if r.status_code >= 400:
        return f"Error: login failed ({r.status_code}): {r.text[:200]}"
    data = r.json()
    sess = {
        "token": data.get("access_token", ""),
        "user_id": data.get("user_id", ""),
        "username": data.get("username", username),
        "session_id": "",
    }
    if not sess["token"] or not sess["user_id"]:
        return f"Error: login response missing token/user_id: {str(data)[:200]}"
    _SESSIONS[_BASE] = sess
    return sess


async def _ensure_login(username: str, password: str) -> dict | str:
    sess = _SESSIONS.get(_BASE)
    if sess and sess.get("token"):
        return sess
    return await _login(username, password)


async def app_login(ctx: ToolContext, username: str = "admin", password: str = "admin") -> str:
    """Log into the running webAgent app and cache the session. Read-only side
    effect (just authentication). Defaults to the local admin (admin/admin)."""
    sess = await _ensure_login(username, password)
    if isinstance(sess, str):
        return sess
    ctx.audit("app_login", {"username": username}, True, sess["user_id"])
    return (f"[app] logged in as '{sess['username']}' (user_id {sess['user_id']}). "
            "You can now use app_chat to talk to the app's agent on this user's behalf.")


async def app_list_agents(ctx: ToolContext, username: str = "admin", password: str = "admin") -> str:
    """List the chat agents available to the logged-in user (id + name), so a
    specific one can be targeted with app_chat(agent_id=...). Read-only."""
    import httpx
    sess = await _ensure_login(username, password)
    if isinstance(sess, str):
        return sess
    try:
        async with httpx.AsyncClient(base_url=_BASE, timeout=httpx.Timeout(20.0, connect=5.0)) as c:
            r = await c.get("/api/v1/agents",
                            params={"user_id": sess["user_id"]},
                            headers={"Authorization": f"Bearer {sess['token']}"})
    except httpx.HTTPError as e:
        return f"Error listing agents: {e}"
    if r.status_code >= 400:
        return f"Error listing agents ({r.status_code}): {r.text[:200]}"
    agents = r.json().get("agents", [])
    if not agents:
        return "[app] this user has no custom agents yet (app_chat will use the default agent)."
    lines = [f"[app] {len(agents)} agent(s) for {sess['username']}:"]
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
    return its reply. Continues the cached conversation unless session_id is given
    or new_session=true. Mutating (the app's agent may run tools / take actions)."""
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    if not (message or "").strip():
        return "Error: a message is required."
    import httpx

    sess = await _ensure_login(username, password)
    if isinstance(sess, str):
        return sess

    # Resolve which conversation this turn belongs to.
    if session_id:
        sid = session_id
    elif new_session or not sess.get("session_id"):
        sid = _new_session_id()
    else:
        sid = sess["session_id"]

    body = {"user_id": sess["user_id"], "session_id": sid, "message": message}
    if agent_id:
        body["agent_id"] = agent_id

    async def _post() -> "httpx.Response":
        # The app runs its full agent loop (may call tools) synchronously, so give
        # it a long read timeout.
        async with httpx.AsyncClient(base_url=_BASE, timeout=httpx.Timeout(300.0, connect=5.0)) as c:
            return await c.post("/api/v1/chat", json=body,
                                headers={"Authorization": f"Bearer {sess['token']}"})

    try:
        r = await _post()
        if r.status_code == 401:                       # token expired — re-login once
            relog = await _login(username, password)
            if isinstance(relog, str):
                return relog
            sess = relog
            body["user_id"] = sess["user_id"]
            r = await _post()
    except httpx.ConnectError:
        return (f"Error: the webAgent server isn't reachable on {_BASE}. "
                "Start it first (Server ▸ Start) and try again.")
    except httpx.ReadTimeout:
        return ("[app] the agent is taking longer than 5 minutes to reply — it may still "
                f"be working. Continue the same session with session_id='{sid}'.")
    except httpx.HTTPError as e:
        return f"Error during chat: {e}"

    if r.status_code == 400 and "No agent" in r.text:
        return ("[app] this user has no agent assigned yet, so the app can't chat. "
                "Create an agent in the app first (or pick one with app_list_agents).")
    if r.status_code >= 400:
        ctx.audit("app_chat", {"session_id": sid}, False, f"{r.status_code}")
        return f"Error from chat ({r.status_code}): {r.text[:300]}"

    sess["session_id"] = sid          # remember the conversation for next turn
    reply = r.json().get("reply") or r.json().get("response") or "(empty reply)"
    ctx.audit("app_chat", {"session_id": sid, "agent_id": agent_id or "default"}, True, "")
    return (f"[app · session {sid}] the app's agent replied:\n\n{reply}\n\n"
            f"(Continue this conversation by calling app_chat again — it stays on session {sid} "
            "unless you pass new_session=true.)")

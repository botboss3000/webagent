"""Connected-app tools — drive the live web-app conversation + edit app config.

Unlike the one-shot ``app_chat`` (see ``appctl.py``), ``webapp_send`` talks to the
**currently connected** session (set in the TUI's connect panel and surfaced on the
ToolContext) and blocks on the live WebSocket stream for the app agent's reply, so
the Manager can hold a real back-and-forth. The config tools read/write the app's
``app-settings.json`` and the LLM **auth key / provider** (stored in the DB's
``auth_elements``) over the same admin HTTP API the web UI uses.
"""

from __future__ import annotations

from ..appclient import WebAppError, get_client
from .base import WRITES_DISABLED_MSG, ToolContext


async def webapp_send(ctx: ToolContext, message: str) -> str:
    """Send a message to the CONNECTED web-app session (the active target) as the
    admin user, and return the app agent's reply. The message + reply also stream
    into the shared transcript. Requires the WebAgent to be unmuted (a target set).
    Mutating — the app agent may run tools and take real actions."""
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    if not (message or "").strip():
        return "Error: a message is required."
    if not ctx.webapp_session_id:
        return ("Error: no web-app session is connected. Open the Connect panel, pick an "
                "agent + session, and unmute the WebAgent first.")
    client = get_client(ctx)
    try:
        reply = await client.send_and_wait(ctx.webapp_session_id, message,
                                           agent_id=ctx.webapp_agent_id or "")
    except WebAppError as e:
        return f"Error: {e}"
    ctx.audit("webapp_send", {"session_id": ctx.webapp_session_id}, True, "")
    name = ctx.webapp_agent_name or "the app agent"
    return f"[{name}] replied:\n\n{reply}"


async def webapp_status(ctx: ToolContext) -> str:
    """Report the connected web-app target (agent + session) and stream health.
    Read-only."""
    client = get_client(ctx)
    if not ctx.webapp_session_id:
        return "[app] not connected to any web-app session (WebAgent is muted)."
    return (f"[app] connected to agent '{ctx.webapp_agent_name or '?'}' "
            f"(id {ctx.webapp_agent_id or '?'}), session {ctx.webapp_session_id}. "
            f"Live stream: {'open' if client.connected else 'not open'}.")


async def app_list_sessions(ctx: ToolContext, agent_id: str = "", limit: int = 20) -> str:
    """List the logged-in user's chat sessions (id, title, agent). Optionally filter
    by agent_id. Read-only."""
    client = get_client(ctx)
    try:
        sessions = await client.list_sessions(agent_id=agent_id, limit=limit)
    except WebAppError as e:
        return f"Error: {e}"
    if not sessions:
        return "[app] no sessions found for this user/agent."
    lines = [f"[app] {len(sessions)} session(s):"]
    for s in sessions:
        title = (s.get("title") or "(untitled)").strip()[:48]
        lines.append(f"  • {title}  —  id {s.get('id', '?')}  (agent {s.get('agent_id', '—')})")
    return "\n".join(lines)


async def app_get_settings(ctx: ToolContext) -> str:
    """Read the app's settings (app-settings.json) — access mode, presentation mode,
    run-watchdog tuning, etc. Read-only."""
    client = get_client(ctx)
    try:
        s = await client.get_app_settings()
    except WebAppError as e:
        return f"Error: {e}"
    lines = ["[app settings]"]
    for k, v in sorted(s.items()):
        lines.append(f"  {k} = {v}")
    return "\n".join(lines)


async def app_set_settings(ctx: ToolContext, patch: dict) -> str:
    """Update one or more app settings (app-settings.json). Pass a dict of the keys
    to change; unspecified keys keep their current value. Mutating."""
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    if not isinstance(patch, dict) or not patch:
        return "Error: pass a non-empty object of {setting: value} to change."
    client = get_client(ctx)
    try:
        current = await client.get_app_settings()
        merged = {**current, **patch}
        updated = await client.set_app_settings(merged)
    except WebAppError as e:
        return f"Error: {e}"
    changed = {k: updated.get(k) for k in patch if k in updated}
    ctx.audit("app_set_settings", {"keys": list(patch.keys())}, True, "")
    return "[app settings] updated: " + ", ".join(f"{k}={v}" for k, v in changed.items())


async def app_get_auth_keys(ctx: ToolContext) -> str:
    """Read the app's LLM provider config (the auth key entry in the DB): provider,
    model, base URL — the API key is masked. Read-only."""
    client = get_client(ctx)
    try:
        p = await client.get_provider()
    except WebAppError as e:
        return f"Error: {e}"
    key = p.get("api_key") or ""
    masked = (key[:4] + "…" + key[-4:]) if len(key) > 8 else ("set" if key else "not set")
    return ("[app LLM auth]\n"
            f"  provider = {p.get('provider', '—')}\n"
            f"  model    = {p.get('model', '—')}\n"
            f"  base_url = {p.get('base_url', '—')}\n"
            f"  api_key  = {masked}")


async def app_set_auth_keys(ctx: ToolContext, api_key: str = "", provider: str = "",
                            base_url: str = "", model: str = "") -> str:
    """Set the app's LLM auth key / provider / base URL / model (writes the DB
    auth_elements row). Only the fields you pass are changed; the
    api_key is never echoed back. Mutating — confirm with the user first."""
    if not ctx.writes_enabled:
        return WRITES_DISABLED_MSG
    client = get_client(ctx)
    try:
        cur = await client.get_provider()
    except WebAppError as e:
        return f"Error: {e}"
    cfg = dict(cur)
    if provider:
        cfg["provider"] = provider
    if base_url:
        cfg["base_url"] = base_url
    if model:
        cfg["model"] = model
    if api_key:
        cfg["api_key"] = api_key
    try:
        await client.set_provider(cfg)
    except WebAppError as e:
        return f"Error: {e}"
    ctx.audit("app_set_auth_keys",
              {"provider": cfg.get("provider"), "model": cfg.get("model"),
               "key_changed": bool(api_key)}, True, "")
    return (f"[app LLM auth] updated — provider={cfg.get('provider')}, model={cfg.get('model')}, "
            f"base_url={cfg.get('base_url')}" + (", api_key set" if api_key else ""))

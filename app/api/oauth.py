"""Public OAuth callback endpoints — handles redirects from OAuth providers.

Supported:
  GET /api/v1/oauth/callback/google
  GET /api/v1/oauth/callback/microsoft
  GET /api/v1/oauth/callback/yahoo
  GET /api/v1/oauth/callback/dropbox
"""

import json
import logging
import os
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Query as QueryParam
from fastapi.responses import HTMLResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/oauth", tags=["oauth"])


def _success_html(provider_name: str, message_type: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>Connected</title>
<style>
  body {{ background: #1a1b26; color: #c0caf5; font-family: system-ui; display: flex;
         align-items: center; justify-content: center; height: 100vh; margin: 0; }}
  .card {{ text-align: center; padding: 40px; }}
  .icon {{ font-size: 48px; margin-bottom: 16px; }}
  h2 {{ margin: 0 0 8px; }}
  p {{ color: #7aa2f7; margin: 0; }}
</style></head>
<body><div class="card">
  <div class="icon">&#10003;</div>
  <h2>{provider_name} Connected</h2>
  <p>You can close this window.</p>
</div>
<script>
  if (window.opener) {{
    window.opener.postMessage({{ type: '{message_type}' }}, '*');
  }}
  setTimeout(() => window.close(), 2000);
</script></body></html>"""


_ERROR_HTML = """<!DOCTYPE html>
<html><head><title>Error</title>
<style>
  body { background: #1a1b26; color: #c0caf5; font-family: system-ui; display: flex;
         align-items: center; justify-content: center; height: 100vh; margin: 0; }
  .card { text-align: center; padding: 40px; }
  .icon { font-size: 48px; margin-bottom: 16px; }
  h2 { margin: 0 0 8px; color: #f7768e; }
  p { color: #a9b1d6; margin: 0; }
</style></head>
<body><div class="card">
  <div class="icon">&#10007;</div>
  <h2>Connection Failed</h2>
  <p>%s</p>
</div>
<script>setTimeout(() => window.close(), 5000);</script></body></html>"""


# ── Google ────────────────────────────────────────────────────────────────

@router.get("/callback/google")
async def google_callback(
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
):
    if error:
        return HTMLResponse(_ERROR_HTML % f"Google returned: {error}", status_code=400)
    if not code or not state:
        return HTMLResponse(_ERROR_HTML % "Missing code or state parameter.", status_code=400)

    from app.admin.integrations import decode_state_token, get_google_creds, get_redirect_uri
    state_data = decode_state_token(state, provider="google")
    if not state_data or not state_data.get("user_id"):
        return HTMLResponse(_ERROR_HTML % "Invalid or expired state token.", status_code=400)

    user_id = state_data["user_id"]
    agent_id = state_data.get("agent_id", "")

    client_id, client_secret = await get_google_creds()
    if not client_id or not client_secret:
        return HTMLResponse(_ERROR_HTML % "Google OAuth not configured on server.", status_code=500)

    redirect_uri = get_redirect_uri()

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if token_resp.status_code != 200:
        logger.error("Google token exchange failed: %s", token_resp.text)
        return HTMLResponse(_ERROR_HTML % "Token exchange failed.", status_code=400)

    token_data = token_resp.json()
    access_token = token_data.get("access_token", "")

    async with httpx.AsyncClient() as client:
        userinfo_resp = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    userinfo = userinfo_resp.json() if userinfo_resp.status_code == 200 else {}

    from app.db import get_db
    db = get_db()

    config = {
        "email": userinfo.get("email", ""),
        "name": userinfo.get("name", ""),
        "picture": userinfo.get("picture", ""),
        "scopes": token_data.get("scope", "").split(),
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }
    secret = {
        "access_token": access_token,
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_in": token_data.get("expires_in", 3600),
        "token_type": token_data.get("token_type", "Bearer"),
    }

    await db.auth_element_set(
        user_id=user_id,
        service="google",
        config=config,
        secret_ref=json.dumps(secret),
        label="oauth",
    )

    if agent_id:
        try:
            await db.upsert_agent_connection(
                agent_id=agent_id,
                connection_type="google",
                section="integration",
                enabled=True,
                config={"connected_user_id": user_id, "email": config["email"], "name": config["name"]},
            )
        except Exception as e:
            logger.warning("Failed to update agent_connections for agent %s: %s", agent_id, e)

    logger.info("Google OAuth connected for user %s (%s), agent=%s", user_id[:12], userinfo.get("email", "?"), agent_id or "none")
    return HTMLResponse(_success_html("Google", "google-oauth-success"))


# ── Microsoft ─────────────────────────────────────────────────────────────

@router.get("/callback/microsoft")
async def microsoft_callback(
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
    error_description: str = QueryParam(None),
):
    if error:
        return HTMLResponse(_ERROR_HTML % f"Microsoft returned: {error_description or error}", status_code=400)
    if not code or not state:
        return HTMLResponse(_ERROR_HTML % "Missing code or state parameter.", status_code=400)

    from app.admin.integrations import decode_state_token, get_microsoft_creds, get_microsoft_redirect_uri
    state_data = decode_state_token(state, provider="microsoft")
    if not state_data or not state_data.get("user_id"):
        return HTMLResponse(_ERROR_HTML % "Invalid or expired state token.", status_code=400)

    user_id = state_data["user_id"]
    agent_id = state_data.get("agent_id", "")

    client_id, client_secret = await get_microsoft_creds()
    if not client_id or not client_secret:
        return HTMLResponse(_ERROR_HTML % "Microsoft OAuth not configured on server.", status_code=500)

    redirect_uri = get_microsoft_redirect_uri()

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://login.microsoftonline.com/common/oauth2/v2.0/token",
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if token_resp.status_code != 200:
        logger.error("Microsoft token exchange failed: %s", token_resp.text)
        return HTMLResponse(_ERROR_HTML % "Token exchange failed.", status_code=400)

    token_data = token_resp.json()
    access_token = token_data.get("access_token", "")

    async with httpx.AsyncClient() as client:
        userinfo_resp = await client.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    userinfo = userinfo_resp.json() if userinfo_resp.status_code == 200 else {}

    from app.db import get_db
    db = get_db()

    email = userinfo.get("mail") or userinfo.get("userPrincipalName", "")
    name = userinfo.get("displayName", "")
    config = {
        "email": email,
        "name": name,
        "picture": "",
        "scopes": token_data.get("scope", "").split(),
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }
    secret = {
        "access_token": access_token,
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_in": token_data.get("expires_in", 3600),
        "token_type": token_data.get("token_type", "Bearer"),
    }

    await db.auth_element_set(
        user_id=user_id,
        service="microsoft",
        config=config,
        secret_ref=json.dumps(secret),
        label="oauth",
    )

    if agent_id:
        try:
            await db.upsert_agent_connection(
                agent_id=agent_id,
                connection_type="microsoft",
                section="integration",
                enabled=True,
                config={"connected_user_id": user_id, "email": email, "name": name},
            )
        except Exception as e:
            logger.warning("Failed to update agent_connections for Microsoft, agent %s: %s", agent_id, e)

    logger.info("Microsoft OAuth connected for user %s (%s), agent=%s", user_id[:12], email or "?", agent_id or "none")
    return HTMLResponse(_success_html("Microsoft", "microsoft-oauth-success"))


# ── Yahoo ─────────────────────────────────────────────────────────────────

@router.get("/callback/yahoo")
async def yahoo_callback(
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
):
    if error:
        return HTMLResponse(_ERROR_HTML % f"Yahoo returned: {error}", status_code=400)
    if not code or not state:
        return HTMLResponse(_ERROR_HTML % "Missing code or state parameter.", status_code=400)

    from app.admin.integrations import decode_state_token, get_yahoo_creds, get_yahoo_redirect_uri
    state_data = decode_state_token(state, provider="yahoo")
    if not state_data or not state_data.get("user_id"):
        return HTMLResponse(_ERROR_HTML % "Invalid or expired state token.", status_code=400)

    user_id = state_data["user_id"]
    agent_id = state_data.get("agent_id", "")

    client_id, client_secret = await get_yahoo_creds()
    if not client_id or not client_secret:
        return HTMLResponse(_ERROR_HTML % "Yahoo OAuth not configured on server.", status_code=500)

    redirect_uri = get_yahoo_redirect_uri()

    import base64
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://api.login.yahoo.com/oauth2/get_token",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if token_resp.status_code != 200:
        logger.error("Yahoo token exchange failed: %s", token_resp.text)
        return HTMLResponse(_ERROR_HTML % "Token exchange failed.", status_code=400)

    token_data = token_resp.json()
    access_token = token_data.get("access_token", "")

    # Yahoo returns xoauth_yahoo_guid in the token response; fetch profile via OpenID
    async with httpx.AsyncClient() as client:
        userinfo_resp = await client.get(
            "https://api.login.yahoo.com/openid/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    userinfo = userinfo_resp.json() if userinfo_resp.status_code == 200 else {}

    from app.db import get_db
    db = get_db()

    email = userinfo.get("email", "")
    name = userinfo.get("name", userinfo.get("nickname", ""))
    config = {
        "email": email,
        "name": name,
        "picture": userinfo.get("picture", ""),
        "yahoo_guid": token_data.get("xoauth_yahoo_guid", ""),
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }
    secret = {
        "access_token": access_token,
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_in": token_data.get("expires_in", 3600),
        "token_type": token_data.get("token_type", "Bearer"),
    }

    await db.auth_element_set(
        user_id=user_id,
        service="yahoo",
        config=config,
        secret_ref=json.dumps(secret),
        label="oauth",
    )

    if agent_id:
        try:
            await db.upsert_agent_connection(
                agent_id=agent_id,
                connection_type="yahoo",
                section="integration",
                enabled=True,
                config={"connected_user_id": user_id, "email": email, "name": name},
            )
        except Exception as e:
            logger.warning("Failed to update agent_connections for Yahoo, agent %s: %s", agent_id, e)

    logger.info("Yahoo OAuth connected for user %s (%s), agent=%s", user_id[:12], email or "?", agent_id or "none")
    return HTMLResponse(_success_html("Yahoo", "yahoo-oauth-success"))


# ── Dropbox ───────────────────────────────────────────────────────────────

@router.get("/callback/dropbox")
async def dropbox_callback(
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
    error_description: str = QueryParam(None),
):
    if error:
        return HTMLResponse(_ERROR_HTML % f"Dropbox returned: {error_description or error}", status_code=400)
    if not code or not state:
        return HTMLResponse(_ERROR_HTML % "Missing code or state parameter.", status_code=400)

    from app.admin.integrations import decode_state_token, get_dropbox_creds, get_dropbox_redirect_uri
    state_data = decode_state_token(state, provider="dropbox")
    if not state_data or not state_data.get("user_id"):
        return HTMLResponse(_ERROR_HTML % "Invalid or expired state token.", status_code=400)

    user_id = state_data["user_id"]
    agent_id = state_data.get("agent_id", "")

    client_id, client_secret = await get_dropbox_creds()
    if not client_id or not client_secret:
        return HTMLResponse(_ERROR_HTML % "Dropbox OAuth not configured on server.", status_code=500)

    redirect_uri = get_dropbox_redirect_uri()

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://api.dropboxapi.com/oauth2/token",
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    if token_resp.status_code != 200:
        logger.error("Dropbox token exchange failed: %s", token_resp.text)
        return HTMLResponse(_ERROR_HTML % "Token exchange failed.", status_code=400)

    token_data = token_resp.json()
    access_token = token_data.get("access_token", "")

    # Fetch account info
    async with httpx.AsyncClient() as client:
        acct_resp = await client.post(
            "https://api.dropboxapi.com/2/users/get_current_account",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            content=b"null",
        )
    acct = acct_resp.json() if acct_resp.status_code == 200 else {}

    from app.db import get_db
    db = get_db()

    name = acct.get("name", {}).get("display_name", "")
    email = acct.get("email", "")
    picture = acct.get("profile_photo_url", "")
    config = {
        "email": email,
        "name": name,
        "picture": picture,
        "account_id": acct.get("account_id", ""),
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }
    secret = {
        "access_token": access_token,
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_in": token_data.get("expires_in", 14400),
        "token_type": token_data.get("token_type", "Bearer"),
    }

    await db.auth_element_set(
        user_id=user_id,
        service="dropbox",
        config=config,
        secret_ref=json.dumps(secret),
        label="oauth",
    )

    if agent_id:
        try:
            await db.upsert_agent_connection(
                agent_id=agent_id,
                connection_type="dropbox",
                section="integration",
                enabled=True,
                config={"connected_user_id": user_id, "email": email, "name": name},
            )
        except Exception as e:
            logger.warning("Failed to update agent_connections for Dropbox, agent %s: %s", agent_id, e)

    logger.info("Dropbox OAuth connected for user %s (%s), agent=%s", user_id[:12], email or "?", agent_id or "none")
    return HTMLResponse(_success_html("Dropbox", "dropbox-oauth-success"))

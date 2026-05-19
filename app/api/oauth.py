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
from fastapi import APIRouter, Query as QueryParam, Request
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
    request: Request,
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

    redirect_uri = get_redirect_uri(request)

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


# ── Shared helper ─────────────────────────────────────────────────────────

async def _store_oauth(db, user_id: str, service: str, config: dict, secret: dict,
                       agent_id: str, connection_type: str, section: str = "social"):
    """Store tokens + upsert agent connection."""
    await db.auth_element_set(
        user_id=user_id, service=service, config=config,
        secret_ref=json.dumps(secret), label="oauth",
    )
    if agent_id:
        try:
            await db.upsert_agent_connection(
                agent_id=agent_id, connection_type=connection_type, section=section,
                enabled=True,
                config={"connected_user_id": user_id, "email": config.get("email", ""), "name": config.get("name", "")},
            )
        except Exception as e:
            logger.warning("Failed to update agent_connections for %s, agent %s: %s", service, agent_id, e)


def _token_secret(token_data: dict) -> dict:
    return {
        "access_token":  token_data.get("access_token", ""),
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_in":    token_data.get("expires_in", 3600),
        "token_type":    token_data.get("token_type", "Bearer"),
    }


# ── Meta (Facebook + Instagram) ───────────────────────────────────────────

@router.get("/callback/meta")
async def meta_callback(
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
    error_description: str = QueryParam(None),
):
    if error:
        return HTMLResponse(_ERROR_HTML % f"Meta returned: {error_description or error}", status_code=400)
    if not code or not state:
        return HTMLResponse(_ERROR_HTML % "Missing code or state parameter.", status_code=400)

    from app.admin.integrations import decode_state_token, get_meta_creds, get_meta_redirect_uri
    state_data = decode_state_token(state, provider="meta")
    if not state_data or not state_data.get("user_id"):
        return HTMLResponse(_ERROR_HTML % "Invalid or expired state token.", status_code=400)

    user_id = state_data["user_id"]
    agent_id = state_data.get("agent_id", "")
    client_id, client_secret = await get_meta_creds()
    if not client_id or not client_secret:
        return HTMLResponse(_ERROR_HTML % "Meta OAuth not configured on server.", status_code=500)

    redirect_uri = get_meta_redirect_uri()
    async with httpx.AsyncClient() as c:
        token_resp = await c.get(
            "https://graph.facebook.com/v19.0/oauth/access_token",
            params={"client_id": client_id, "client_secret": client_secret,
                    "redirect_uri": redirect_uri, "code": code},
        )
    if token_resp.status_code != 200:
        logger.error("Meta token exchange failed: %s", token_resp.text)
        return HTMLResponse(_ERROR_HTML % "Token exchange failed.", status_code=400)

    token_data = token_resp.json()
    access_token = token_data.get("access_token", "")

    async with httpx.AsyncClient() as c:
        me_resp = await c.get(
            "https://graph.facebook.com/v19.0/me",
            params={"fields": "id,name,email,picture.type(large)", "access_token": access_token},
        )
    me = me_resp.json() if me_resp.status_code == 200 else {}

    from app.db import get_db
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    config = {
        "email": me.get("email", ""),
        "name": me.get("name", ""),
        "picture": me.get("picture", {}).get("data", {}).get("url", "") if isinstance(me.get("picture"), dict) else "",
        "provider_user_id": me.get("id", ""),
        "connected_at": now,
    }
    secret = _token_secret(token_data)

    # Store under "meta" (parent) + aliases for facebook and instagram
    await _store_oauth(db, user_id, "meta", config, secret, agent_id, "facebook", section="social")
    await db.auth_element_set(user_id=user_id, service="facebook", config=config, secret_ref=json.dumps(secret), label="oauth")
    await db.auth_element_set(user_id=user_id, service="instagram", config=config, secret_ref=json.dumps(secret), label="oauth")
    # Also update instagram agent connection if agent_id
    if agent_id:
        try:
            await db.upsert_agent_connection(agent_id=agent_id, connection_type="instagram", section="social",
                enabled=True, config={"connected_user_id": user_id, "email": config["email"], "name": config["name"]})
        except Exception:
            pass

    logger.info("Meta OAuth connected for user %s (%s), agent=%s", user_id[:12], config.get("email") or me.get("name", "?"), agent_id or "none")
    return HTMLResponse(_success_html("Meta (Facebook & Instagram)", "meta-oauth-success"))


# ── Twitter / X ───────────────────────────────────────────────────────────

@router.get("/callback/twitter")
async def twitter_callback(
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
    error_description: str = QueryParam(None),
):
    if error:
        return HTMLResponse(_ERROR_HTML % f"Twitter returned: {error_description or error}", status_code=400)
    if not code or not state:
        return HTMLResponse(_ERROR_HTML % "Missing code or state parameter.", status_code=400)

    import base64 as _b64
    from app.admin.integrations import decode_state_token, get_twitter_creds, get_twitter_redirect_uri
    state_data = decode_state_token(state, provider="twitter")
    if not state_data or not state_data.get("user_id"):
        return HTMLResponse(_ERROR_HTML % "Invalid or expired state token.", status_code=400)

    user_id = state_data["user_id"]
    agent_id = state_data.get("agent_id", "")
    pkce_verifier = state_data.get("pkce_verifier", "")

    client_id, client_secret = await get_twitter_creds()
    if not client_id or not client_secret:
        return HTMLResponse(_ERROR_HTML % "Twitter OAuth not configured on server.", status_code=500)

    redirect_uri = get_twitter_redirect_uri()
    creds_b64 = _b64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    async with httpx.AsyncClient() as c:
        token_resp = await c.post(
            "https://api.twitter.com/2/oauth2/token",
            headers={"Authorization": f"Basic {creds_b64}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri,
                  "code_verifier": pkce_verifier},
        )
    if token_resp.status_code != 200:
        logger.error("Twitter token exchange failed: %s", token_resp.text)
        return HTMLResponse(_ERROR_HTML % "Token exchange failed.", status_code=400)

    token_data = token_resp.json()
    access_token = token_data.get("access_token", "")

    async with httpx.AsyncClient() as c:
        me_resp = await c.get(
            "https://api.twitter.com/2/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"user.fields": "name,username,profile_image_url,description"},
        )
    me = me_resp.json().get("data", {}) if me_resp.status_code == 200 else {}

    from app.db import get_db
    db = get_db()
    config = {
        "name": me.get("name", ""),
        "username": me.get("username", ""),
        "picture": me.get("profile_image_url", ""),
        "email": "",
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }
    await _store_oauth(db, user_id, "twitter", config, _token_secret(token_data), agent_id, "twitter")
    logger.info("Twitter OAuth connected for user %s (@%s), agent=%s", user_id[:12], me.get("username", "?"), agent_id or "none")
    return HTMLResponse(_success_html("Twitter / X", "twitter-oauth-success"))


# ── LinkedIn ──────────────────────────────────────────────────────────────

@router.get("/callback/linkedin")
async def linkedin_callback(
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
    error_description: str = QueryParam(None),
):
    if error:
        return HTMLResponse(_ERROR_HTML % f"LinkedIn returned: {error_description or error}", status_code=400)
    if not code or not state:
        return HTMLResponse(_ERROR_HTML % "Missing code or state parameter.", status_code=400)

    from app.admin.integrations import decode_state_token, get_linkedin_creds, get_linkedin_redirect_uri
    state_data = decode_state_token(state, provider="linkedin")
    if not state_data or not state_data.get("user_id"):
        return HTMLResponse(_ERROR_HTML % "Invalid or expired state token.", status_code=400)

    user_id = state_data["user_id"]
    agent_id = state_data.get("agent_id", "")
    client_id, client_secret = await get_linkedin_creds()
    if not client_id or not client_secret:
        return HTMLResponse(_ERROR_HTML % "LinkedIn OAuth not configured on server.", status_code=500)

    redirect_uri = get_linkedin_redirect_uri()
    async with httpx.AsyncClient() as c:
        token_resp = await c.post(
            "https://www.linkedin.com/oauth/v2/accessToken",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"code": code, "client_id": client_id, "client_secret": client_secret,
                  "redirect_uri": redirect_uri, "grant_type": "authorization_code"},
        )
    if token_resp.status_code != 200:
        logger.error("LinkedIn token exchange failed: %s", token_resp.text)
        return HTMLResponse(_ERROR_HTML % "Token exchange failed.", status_code=400)

    token_data = token_resp.json()
    access_token = token_data.get("access_token", "")

    async with httpx.AsyncClient() as c:
        me_resp = await c.get(
            "https://api.linkedin.com/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    me = me_resp.json() if me_resp.status_code == 200 else {}

    from app.db import get_db
    db = get_db()
    config = {
        "email": me.get("email", ""),
        "name": me.get("name", ""),
        "picture": me.get("picture", ""),
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }
    await _store_oauth(db, user_id, "linkedin", config, _token_secret(token_data), agent_id, "linkedin")
    logger.info("LinkedIn OAuth connected for user %s (%s), agent=%s", user_id[:12], config["email"] or "?", agent_id or "none")
    return HTMLResponse(_success_html("LinkedIn", "linkedin-oauth-success"))


# ── TikTok ────────────────────────────────────────────────────────────────

@router.get("/callback/tiktok")
async def tiktok_callback(
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
    error_description: str = QueryParam(None),
):
    if error:
        return HTMLResponse(_ERROR_HTML % f"TikTok returned: {error_description or error}", status_code=400)
    if not code or not state:
        return HTMLResponse(_ERROR_HTML % "Missing code or state parameter.", status_code=400)

    from app.admin.integrations import decode_state_token, get_tiktok_creds, get_tiktok_redirect_uri
    state_data = decode_state_token(state, provider="tiktok")
    if not state_data or not state_data.get("user_id"):
        return HTMLResponse(_ERROR_HTML % "Invalid or expired state token.", status_code=400)

    user_id = state_data["user_id"]
    agent_id = state_data.get("agent_id", "")
    pkce_verifier = state_data.get("pkce_verifier", "")

    client_id, client_secret = await get_tiktok_creds()
    if not client_id or not client_secret:
        return HTMLResponse(_ERROR_HTML % "TikTok OAuth not configured on server.", status_code=500)

    redirect_uri = get_tiktok_redirect_uri()
    async with httpx.AsyncClient() as c:
        token_resp = await c.post(
            "https://open.tiktokapis.com/v2/oauth/token/",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"client_key": client_id, "client_secret": client_secret, "code": code,
                  "grant_type": "authorization_code", "redirect_uri": redirect_uri,
                  "code_verifier": pkce_verifier},
        )
    if token_resp.status_code != 200:
        logger.error("TikTok token exchange failed: %s", token_resp.text)
        return HTMLResponse(_ERROR_HTML % "Token exchange failed.", status_code=400)

    token_data = token_resp.json().get("data", token_resp.json())
    access_token = token_data.get("access_token", "")

    async with httpx.AsyncClient() as c:
        me_resp = await c.get(
            "https://open.tiktokapis.com/v2/user/info/",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "open_id,display_name,avatar_url"},
        )
    me = me_resp.json().get("data", {}).get("user", {}) if me_resp.status_code == 200 else {}

    from app.db import get_db
    db = get_db()
    config = {
        "name": me.get("display_name", ""),
        "picture": me.get("avatar_url", ""),
        "email": "",
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }
    await _store_oauth(db, user_id, "tiktok", config, _token_secret(token_data), agent_id, "tiktok")
    logger.info("TikTok OAuth connected for user %s (%s), agent=%s", user_id[:12], config["name"] or "?", agent_id or "none")
    return HTMLResponse(_success_html("TikTok", "tiktok-oauth-success"))


# ── Pinterest ─────────────────────────────────────────────────────────────

@router.get("/callback/pinterest")
async def pinterest_callback(
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
):
    if error:
        return HTMLResponse(_ERROR_HTML % f"Pinterest returned: {error}", status_code=400)
    if not code or not state:
        return HTMLResponse(_ERROR_HTML % "Missing code or state parameter.", status_code=400)

    from app.admin.integrations import decode_state_token, get_pinterest_creds, get_pinterest_redirect_uri
    state_data = decode_state_token(state, provider="pinterest")
    if not state_data or not state_data.get("user_id"):
        return HTMLResponse(_ERROR_HTML % "Invalid or expired state token.", status_code=400)

    user_id = state_data["user_id"]
    agent_id = state_data.get("agent_id", "")
    client_id, client_secret = await get_pinterest_creds()
    if not client_id or not client_secret:
        return HTMLResponse(_ERROR_HTML % "Pinterest OAuth not configured on server.", status_code=500)

    redirect_uri = get_pinterest_redirect_uri()
    import base64 as _b64
    creds_b64 = _b64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    async with httpx.AsyncClient() as c:
        token_resp = await c.post(
            "https://api.pinterest.com/v5/oauth/token",
            headers={"Authorization": f"Basic {creds_b64}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"code": code, "redirect_uri": redirect_uri, "grant_type": "authorization_code"},
        )
    if token_resp.status_code != 200:
        logger.error("Pinterest token exchange failed: %s", token_resp.text)
        return HTMLResponse(_ERROR_HTML % "Token exchange failed.", status_code=400)

    token_data = token_resp.json()
    access_token = token_data.get("access_token", "")

    async with httpx.AsyncClient() as c:
        me_resp = await c.get(
            "https://api.pinterest.com/v5/user_account",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    me = me_resp.json() if me_resp.status_code == 200 else {}

    from app.db import get_db
    db = get_db()
    config = {
        "name": me.get("username", ""),
        "picture": me.get("profile_image", ""),
        "email": "",
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }
    await _store_oauth(db, user_id, "pinterest", config, _token_secret(token_data), agent_id, "pinterest")
    logger.info("Pinterest OAuth connected for user %s (%s), agent=%s", user_id[:12], config["name"] or "?", agent_id or "none")
    return HTMLResponse(_success_html("Pinterest", "pinterest-oauth-success"))


# ── Reddit ────────────────────────────────────────────────────────────────

@router.get("/callback/reddit")
async def reddit_callback(
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
):
    if error:
        return HTMLResponse(_ERROR_HTML % f"Reddit returned: {error}", status_code=400)
    if not code or not state:
        return HTMLResponse(_ERROR_HTML % "Missing code or state parameter.", status_code=400)

    from app.admin.integrations import decode_state_token, get_reddit_creds, get_reddit_redirect_uri
    import base64 as _b64
    state_data = decode_state_token(state, provider="reddit")
    if not state_data or not state_data.get("user_id"):
        return HTMLResponse(_ERROR_HTML % "Invalid or expired state token.", status_code=400)

    user_id = state_data["user_id"]
    agent_id = state_data.get("agent_id", "")
    client_id, client_secret = await get_reddit_creds()
    if not client_id or not client_secret:
        return HTMLResponse(_ERROR_HTML % "Reddit OAuth not configured on server.", status_code=500)

    redirect_uri = get_reddit_redirect_uri()
    creds_b64 = _b64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    async with httpx.AsyncClient() as c:
        token_resp = await c.post(
            "https://www.reddit.com/api/v1/access_token",
            headers={"Authorization": f"Basic {creds_b64}", "Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": "webAgent/1.0"},
            data={"code": code, "redirect_uri": redirect_uri, "grant_type": "authorization_code"},
        )
    if token_resp.status_code != 200:
        logger.error("Reddit token exchange failed: %s", token_resp.text)
        return HTMLResponse(_ERROR_HTML % "Token exchange failed.", status_code=400)

    token_data = token_resp.json()
    access_token = token_data.get("access_token", "")

    async with httpx.AsyncClient() as c:
        me_resp = await c.get(
            "https://oauth.reddit.com/api/v1/me",
            headers={"Authorization": f"Bearer {access_token}", "User-Agent": "webAgent/1.0"},
        )
    me = me_resp.json() if me_resp.status_code == 200 else {}

    from app.db import get_db
    db = get_db()
    config = {
        "name": me.get("name", ""),
        "picture": me.get("icon_img", "").split("?")[0],
        "email": "",
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }
    await _store_oauth(db, user_id, "reddit", config, _token_secret(token_data), agent_id, "reddit")
    logger.info("Reddit OAuth connected for user %s (u/%s), agent=%s", user_id[:12], config["name"] or "?", agent_id or "none")
    return HTMLResponse(_success_html("Reddit", "reddit-oauth-success"))


# ── Snapchat ──────────────────────────────────────────────────────────────

@router.get("/callback/snapchat")
async def snapchat_callback(
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
):
    if error:
        return HTMLResponse(_ERROR_HTML % f"Snapchat returned: {error}", status_code=400)
    if not code or not state:
        return HTMLResponse(_ERROR_HTML % "Missing code or state parameter.", status_code=400)

    from app.admin.integrations import decode_state_token, get_snapchat_creds, get_snapchat_redirect_uri
    state_data = decode_state_token(state, provider="snapchat")
    if not state_data or not state_data.get("user_id"):
        return HTMLResponse(_ERROR_HTML % "Invalid or expired state token.", status_code=400)

    user_id = state_data["user_id"]
    agent_id = state_data.get("agent_id", "")
    client_id, client_secret = await get_snapchat_creds()
    if not client_id or not client_secret:
        return HTMLResponse(_ERROR_HTML % "Snapchat OAuth not configured on server.", status_code=500)

    redirect_uri = get_snapchat_redirect_uri()
    import base64 as _b64
    creds_b64 = _b64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    async with httpx.AsyncClient() as c:
        token_resp = await c.post(
            "https://accounts.snapchat.com/login/oauth2/access_token",
            headers={"Authorization": f"Basic {creds_b64}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"code": code, "redirect_uri": redirect_uri, "grant_type": "authorization_code"},
        )
    if token_resp.status_code != 200:
        logger.error("Snapchat token exchange failed: %s", token_resp.text)
        return HTMLResponse(_ERROR_HTML % "Token exchange failed.", status_code=400)

    token_data = token_resp.json()
    access_token = token_data.get("access_token", "")

    async with httpx.AsyncClient() as c:
        me_resp = await c.get(
            "https://kit.snapchat.com/v1/me",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"query": "{me{displayName bitmoji{avatar}}}"},
        )
    me = me_resp.json().get("data", {}).get("me", {}) if me_resp.status_code == 200 else {}

    from app.db import get_db
    db = get_db()
    config = {
        "name": me.get("displayName", ""),
        "picture": me.get("bitmoji", {}).get("avatar", ""),
        "email": "",
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }
    await _store_oauth(db, user_id, "snapchat", config, _token_secret(token_data), agent_id, "snapchat")
    logger.info("Snapchat OAuth connected for user %s (%s), agent=%s", user_id[:12], config["name"] or "?", agent_id or "none")
    return HTMLResponse(_success_html("Snapchat", "snapchat-oauth-success"))


# ── Twitch ────────────────────────────────────────────────────────────────

@router.get("/callback/twitch")
async def twitch_callback(
    code: str = QueryParam(None),
    state: str = QueryParam(None),
    error: str = QueryParam(None),
    error_description: str = QueryParam(None),
):
    if error:
        return HTMLResponse(_ERROR_HTML % f"Twitch returned: {error_description or error}", status_code=400)
    if not code or not state:
        return HTMLResponse(_ERROR_HTML % "Missing code or state parameter.", status_code=400)

    from app.admin.integrations import decode_state_token, get_twitch_creds, get_twitch_redirect_uri
    state_data = decode_state_token(state, provider="twitch")
    if not state_data or not state_data.get("user_id"):
        return HTMLResponse(_ERROR_HTML % "Invalid or expired state token.", status_code=400)

    user_id = state_data["user_id"]
    agent_id = state_data.get("agent_id", "")
    client_id, client_secret = await get_twitch_creds()
    if not client_id or not client_secret:
        return HTMLResponse(_ERROR_HTML % "Twitch OAuth not configured on server.", status_code=500)

    redirect_uri = get_twitch_redirect_uri()
    async with httpx.AsyncClient() as c:
        token_resp = await c.post(
            "https://id.twitch.tv/oauth2/token",
            params={"client_id": client_id, "client_secret": client_secret, "code": code,
                    "grant_type": "authorization_code", "redirect_uri": redirect_uri},
        )
    if token_resp.status_code != 200:
        logger.error("Twitch token exchange failed: %s", token_resp.text)
        return HTMLResponse(_ERROR_HTML % "Token exchange failed.", status_code=400)

    token_data = token_resp.json()
    access_token = token_data.get("access_token", "")

    async with httpx.AsyncClient() as c:
        me_resp = await c.get(
            "https://api.twitch.tv/helix/users",
            headers={"Authorization": f"Bearer {access_token}", "Client-Id": client_id},
        )
    me = (me_resp.json().get("data") or [{}])[0] if me_resp.status_code == 200 else {}

    from app.db import get_db
    db = get_db()
    config = {
        "name": me.get("display_name", ""),
        "picture": me.get("profile_image_url", ""),
        "email": me.get("email", ""),
        "connected_at": datetime.now(timezone.utc).isoformat(),
    }
    await _store_oauth(db, user_id, "twitch", config, _token_secret(token_data), agent_id, "twitch")
    logger.info("Twitch OAuth connected for user %s (%s), agent=%s", user_id[:12], config["name"] or "?", agent_id or "none")
    return HTMLResponse(_success_html("Twitch", "twitch-oauth-success"))

"""Integrations admin endpoints — OAuth credential config (admin) + shared helpers.

Supported providers:
  Productivity
  - Google     (Gmail, Drive, Docs, Calendar)
  - Microsoft  (Outlook, OneDrive, SharePoint)
  - Yahoo      (Yahoo Mail)
  - Dropbox    (file storage)

  Social Media
  - Meta       (Facebook + Instagram — one app, two connections)
  - Twitter/X  (posts, DMs, profile)
  - LinkedIn   (posts, profile)
  - TikTok     (videos, profile)
  - Pinterest  (boards, pins)
  - Reddit     (posts, comments, profile)
  - Snapchat   (profile, Bitmoji)
  - Twitch     (channel, clips, subscriptions)
"""

import base64
import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Header, Query
from jose import jwt as jose_jwt
from pydantic import BaseModel

from app.auth.jwt import decode_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/integrations", tags=["integrations"])

ANONYMOUS_KEY = "__anonymous__"
_ADMIN_USER = "admin_default"

_JWT_SECRET = os.environ.get("JWT_SECRET", "webagent-dev-secret-change-in-prod")
_JWT_ALGORITHM = "HS256"

# ── OAuth scope definitions ───────────────────────────────────────────────

GOOGLE_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/docs",
    "https://www.googleapis.com/auth/docs.readonly",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.readonly",
]

MICROSOFT_SCOPES = [
    "openid",
    "email",
    "profile",
    "offline_access",
    "User.Read",
    "Mail.ReadWrite",
    "Mail.Send",
    "Calendars.ReadWrite",
    "Files.ReadWrite.All",
    "Sites.ReadWrite.All",
]

YAHOO_SCOPES = [
    "openid",
    "email",
    "profile",
    "mail-r",
    "mail-w",
]

DROPBOX_SCOPES = [
    "account_info.read",
    "files.metadata.read",
    "files.metadata.write",
    "files.content.read",
    "files.content.write",
    "sharing.read",
    "sharing.write",
]

# ── Social media scopes ───────────────────────────────────────────────────

META_SCOPES = [
    "public_profile",
    "email",
    "pages_manage_posts",
    "pages_read_engagement",
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_comments",
    "instagram_manage_insights",
]

TWITTER_SCOPES = [
    "tweet.read",
    "tweet.write",
    "tweet.moderate.write",
    "users.read",
    "follows.read",
    "follows.write",
    "offline.access",
    "like.read",
    "like.write",
    "dm.read",
    "dm.write",
]

LINKEDIN_SCOPES = [
    "openid",
    "profile",
    "email",
    "w_member_social",
    "r_liteprofile",
    "r_emailaddress",
]

TIKTOK_SCOPES = [
    "user.info.basic",
    "user.info.profile",
    "video.list",
    "video.upload",
    "video.publish",
]

PINTEREST_SCOPES = [
    "boards:read",
    "boards:write",
    "pins:read",
    "pins:write",
    "user_accounts:read",
]

REDDIT_SCOPES = [
    "identity",
    "read",
    "submit",
    "history",
    "mysubreddits",
    "privatemessages",
]

SNAPCHAT_SCOPES = [
    "https://auth.snapchat.com/oauth2/api/user.display_name",
    "https://auth.snapchat.com/oauth2/api/user.bitmoji.avatar",
    "https://auth.snapchat.com/oauth2/api/user.external_id",
]

TWITCH_SCOPES = [
    "user:read:email",
    "user:read:follows",
    "channel:read:subscriptions",
    "channel:manage:broadcast",
    "clips:edit",
    "chat:read",
    "chat:edit",
]


# ── Shared helpers (used by app/api/agents.py and app/api/oauth.py) ──────

def resolve_user_id(authorization: str = "", token_qs: str = "") -> str:
    raw = ""
    if authorization.startswith("Bearer "):
        raw = authorization[7:]
    if not raw and token_qs:
        raw = token_qs
    if raw:
        payload = decode_token(raw)
        if payload:
            return payload.get("user_id", ANONYMOUS_KEY)
    return ANONYMOUS_KEY


def _get_base_url() -> str:
    """Return the configured base URL (from env or webhook_base_url.txt)."""
    from pathlib import Path
    base_url = os.environ.get("WEBHOOK_BASE_URL", "")
    if not base_url:
        try:
            wh_file = Path(__file__).resolve().parent.parent.parent / "webhook_base_url.txt"
            if wh_file.exists():
                base_url = wh_file.read_text().strip()
        except Exception:
            pass
    return base_url or "http://localhost:8000"


# ── Generic state-token helpers ───────────────────────────────────────────

def make_state_token(user_id: str, agent_id: str = "", provider: str = "google", **extra) -> str:
    """Create a signed JWT state token for OAuth CSRF protection.

    Extra keyword args are merged into the payload (e.g. pkce_verifier for Twitter).
    """
    payload = {
        "user_id": user_id,
        "agent_id": agent_id,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
        "purpose": f"{provider}_oauth",
        **extra,
    }
    return jose_jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)


def decode_state_token(state: str, provider: str = "google") -> Optional[dict]:
    """Decode state token. Returns full payload dict (user_id, agent_id, ...) or None."""
    try:
        payload = jose_jwt.decode(state, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
        if payload.get("purpose") != f"{provider}_oauth":
            return None
        return payload
    except Exception:
        return None


def _pkce_pair() -> tuple[str, str]:
    """Generate a PKCE (code_verifier, code_challenge) pair."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


# ── Google ────────────────────────────────────────────────────────────────

async def get_google_creds() -> tuple[str, str]:
    """Get Google OAuth creds — DB first, then env vars fallback."""
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, "google_oauth_config", "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            cid = config.get("client_id", "")
            csec = elem.get("secret_ref", "") or config.get("client_secret", "")
            if cid and csec:
                return (cid, csec)
    except Exception as e:
        logger.debug("Failed to read Google creds from DB: %s", e)
    return (
        os.environ.get("GOOGLE_CLIENT_ID", ""),
        os.environ.get("GOOGLE_CLIENT_SECRET", ""),
    )


def get_google_creds_sync() -> tuple[str, str]:
    """Sync fallback — env vars only (for module-level checks)."""
    return (
        os.environ.get("GOOGLE_CLIENT_ID", ""),
        os.environ.get("GOOGLE_CLIENT_SECRET", ""),
    )


def get_redirect_uri() -> str:
    return f"{_get_base_url().rstrip('/')}/api/v1/oauth/callback/google"


async def build_google_authorize_url(user_id: str, agent_id: str = "") -> str:
    """Build the full Google OAuth authorization URL."""
    client_id, _ = await get_google_creds()
    redirect_uri = get_redirect_uri()
    state = make_state_token(user_id, agent_id, provider="google")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"https://accounts.google.com/o/oauth2/auth?{urlencode(params)}"


async def revoke_and_delete_google(user_id: str) -> bool:
    """Revoke Google token and delete from auth_elements. Returns True if deleted."""
    from app.db import get_db
    db = get_db()
    elem = await db.auth_element_get(user_id, "google", "oauth")
    if elem and elem.get("secret_ref"):
        try:
            tokens = json.loads(elem["secret_ref"])
            access_token = tokens.get("access_token", "")
            if access_token:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        "https://oauth2.googleapis.com/revoke",
                        params={"token": access_token},
                    )
        except Exception as e:
            logger.warning("Failed to revoke Google token: %s", e)
    return await db.auth_element_delete(user_id, "google", "oauth")


# ── Microsoft ─────────────────────────────────────────────────────────────

async def get_microsoft_creds() -> tuple[str, str]:
    """Get Microsoft OAuth creds — DB first, then env vars fallback."""
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, "microsoft_oauth_config", "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            cid = config.get("client_id", "")
            csec = elem.get("secret_ref", "") or config.get("client_secret", "")
            if cid and csec:
                return (cid, csec)
    except Exception as e:
        logger.debug("Failed to read Microsoft creds from DB: %s", e)
    return (
        os.environ.get("MICROSOFT_CLIENT_ID", ""),
        os.environ.get("MICROSOFT_CLIENT_SECRET", ""),
    )


def get_microsoft_redirect_uri() -> str:
    return f"{_get_base_url().rstrip('/')}/api/v1/oauth/callback/microsoft"


async def build_microsoft_authorize_url(user_id: str, agent_id: str = "") -> str:
    """Build the full Microsoft OAuth authorization URL (common / multi-tenant)."""
    client_id, _ = await get_microsoft_creds()
    redirect_uri = get_microsoft_redirect_uri()
    state = make_state_token(user_id, agent_id, provider="microsoft")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(MICROSOFT_SCOPES),
        "response_mode": "query",
        "state": state,
    }
    return f"https://login.microsoftonline.com/common/oauth2/v2.0/authorize?{urlencode(params)}"


async def revoke_and_delete_microsoft(user_id: str) -> bool:
    """Delete Microsoft tokens from auth_elements (Microsoft has no revoke endpoint)."""
    from app.db import get_db
    db = get_db()
    return await db.auth_element_delete(user_id, "microsoft", "oauth")


# ── Yahoo ─────────────────────────────────────────────────────────────────

async def get_yahoo_creds() -> tuple[str, str]:
    """Get Yahoo OAuth creds — DB first, then env vars fallback."""
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, "yahoo_oauth_config", "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            cid = config.get("client_id", "")
            csec = elem.get("secret_ref", "") or config.get("client_secret", "")
            if cid and csec:
                return (cid, csec)
    except Exception as e:
        logger.debug("Failed to read Yahoo creds from DB: %s", e)
    return (
        os.environ.get("YAHOO_CLIENT_ID", ""),
        os.environ.get("YAHOO_CLIENT_SECRET", ""),
    )


def get_yahoo_redirect_uri() -> str:
    return f"{_get_base_url().rstrip('/')}/api/v1/oauth/callback/yahoo"


async def build_yahoo_authorize_url(user_id: str, agent_id: str = "") -> str:
    """Build the Yahoo OAuth authorization URL."""
    client_id, _ = await get_yahoo_creds()
    redirect_uri = get_yahoo_redirect_uri()
    state = make_state_token(user_id, agent_id, provider="yahoo")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(YAHOO_SCOPES),
        "state": state,
    }
    return f"https://api.login.yahoo.com/oauth2/request_auth?{urlencode(params)}"


async def revoke_and_delete_yahoo(user_id: str) -> bool:
    """Delete Yahoo tokens from auth_elements."""
    from app.db import get_db
    db = get_db()
    return await db.auth_element_delete(user_id, "yahoo", "oauth")


# ── Dropbox ───────────────────────────────────────────────────────────────

async def get_dropbox_creds() -> tuple[str, str]:
    """Get Dropbox OAuth creds — DB first, then env vars fallback."""
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, "dropbox_oauth_config", "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            cid = config.get("client_id", "")
            csec = elem.get("secret_ref", "") or config.get("client_secret", "")
            if cid and csec:
                return (cid, csec)
    except Exception as e:
        logger.debug("Failed to read Dropbox creds from DB: %s", e)
    return (
        os.environ.get("DROPBOX_APP_KEY", ""),
        os.environ.get("DROPBOX_APP_SECRET", ""),
    )


def get_dropbox_redirect_uri() -> str:
    return f"{_get_base_url().rstrip('/')}/api/v1/oauth/callback/dropbox"


async def build_dropbox_authorize_url(user_id: str, agent_id: str = "") -> str:
    """Build the Dropbox OAuth authorization URL."""
    client_id, _ = await get_dropbox_creds()
    redirect_uri = get_dropbox_redirect_uri()
    state = make_state_token(user_id, agent_id, provider="dropbox")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(DROPBOX_SCOPES),
        "token_access_type": "offline",
        "state": state,
    }
    return f"https://www.dropbox.com/oauth2/authorize?{urlencode(params)}"


async def revoke_and_delete_dropbox(user_id: str) -> bool:
    """Revoke Dropbox token and delete from auth_elements."""
    from app.db import get_db
    db = get_db()
    elem = await db.auth_element_get(user_id, "dropbox", "oauth")
    if elem and elem.get("secret_ref"):
        try:
            tokens = json.loads(elem["secret_ref"])
            access_token = tokens.get("access_token", "")
            if access_token:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        "https://api.dropboxapi.com/2/auth/token/revoke",
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
        except Exception as e:
            logger.warning("Failed to revoke Dropbox token: %s", e)
    return await db.auth_element_delete(user_id, "dropbox", "oauth")


# ── Meta (Facebook + Instagram) ───────────────────────────────────────────

async def get_meta_creds() -> tuple[str, str]:
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, "meta_oauth_config", "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            cid = config.get("client_id", "")
            csec = elem.get("secret_ref", "") or config.get("client_secret", "")
            if cid and csec:
                return (cid, csec)
    except Exception as e:
        logger.debug("Failed to read Meta creds from DB: %s", e)
    return (os.environ.get("META_APP_ID", ""), os.environ.get("META_APP_SECRET", ""))


def get_meta_redirect_uri() -> str:
    return f"{_get_base_url().rstrip('/')}/api/v1/oauth/callback/meta"


async def build_meta_authorize_url(user_id: str, agent_id: str = "") -> str:
    client_id, _ = await get_meta_creds()
    redirect_uri = get_meta_redirect_uri()
    state = make_state_token(user_id, agent_id, provider="meta")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": ",".join(META_SCOPES),
        "state": state,
    }
    return f"https://www.facebook.com/v19.0/dialog/oauth?{urlencode(params)}"


async def revoke_and_delete_meta(user_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    # Meta: revoke via Graph API DELETE /{user_id}/permissions
    elem = await db.auth_element_get(user_id, "meta", "oauth")
    if elem and elem.get("secret_ref"):
        try:
            tokens = json.loads(elem["secret_ref"])
            access_token = tokens.get("access_token", "")
            meta_uid = (json.loads(elem["config"]) if isinstance(elem.get("config"), str) else elem.get("config", {})).get("provider_user_id", "")
            if access_token and meta_uid:
                async with httpx.AsyncClient() as c:
                    await c.delete(f"https://graph.facebook.com/v19.0/{meta_uid}/permissions", params={"access_token": access_token})
        except Exception as e:
            logger.warning("Failed to revoke Meta token: %s", e)
    await db.auth_element_delete(user_id, "meta", "oauth")
    # Also clean up the per-platform aliases
    await db.auth_element_delete(user_id, "facebook", "oauth")
    await db.auth_element_delete(user_id, "instagram", "oauth")
    return True


# ── Twitter / X ───────────────────────────────────────────────────────────

async def get_twitter_creds() -> tuple[str, str]:
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, "twitter_oauth_config", "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            cid = config.get("client_id", "")
            csec = elem.get("secret_ref", "") or config.get("client_secret", "")
            if cid and csec:
                return (cid, csec)
    except Exception as e:
        logger.debug("Failed to read Twitter creds from DB: %s", e)
    return (os.environ.get("TWITTER_CLIENT_ID", ""), os.environ.get("TWITTER_CLIENT_SECRET", ""))


def get_twitter_redirect_uri() -> str:
    return f"{_get_base_url().rstrip('/')}/api/v1/oauth/callback/twitter"


async def build_twitter_authorize_url(user_id: str, agent_id: str = "") -> tuple[str, str]:
    """Returns (authorize_url, state_token). Twitter requires PKCE."""
    client_id, _ = await get_twitter_creds()
    redirect_uri = get_twitter_redirect_uri()
    verifier, challenge = _pkce_pair()
    state = make_state_token(user_id, agent_id, provider="twitter", pkce_verifier=verifier)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(TWITTER_SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"https://twitter.com/i/oauth2/authorize?{urlencode(params)}", state


async def revoke_and_delete_twitter(user_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    elem = await db.auth_element_get(user_id, "twitter", "oauth")
    if elem and elem.get("secret_ref"):
        try:
            tokens = json.loads(elem["secret_ref"])
            access_token = tokens.get("access_token", "")
            client_id, client_secret = await get_twitter_creds()
            if access_token and client_id and client_secret:
                creds_b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
                async with httpx.AsyncClient() as c:
                    await c.post(
                        "https://api.twitter.com/2/oauth2/revoke",
                        headers={"Authorization": f"Basic {creds_b64}", "Content-Type": "application/x-www-form-urlencoded"},
                        data={"token": access_token, "token_type_hint": "access_token"},
                    )
        except Exception as e:
            logger.warning("Failed to revoke Twitter token: %s", e)
    return await db.auth_element_delete(user_id, "twitter", "oauth")


# ── LinkedIn ──────────────────────────────────────────────────────────────

async def get_linkedin_creds() -> tuple[str, str]:
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, "linkedin_oauth_config", "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            cid = config.get("client_id", "")
            csec = elem.get("secret_ref", "") or config.get("client_secret", "")
            if cid and csec:
                return (cid, csec)
    except Exception as e:
        logger.debug("Failed to read LinkedIn creds from DB: %s", e)
    return (os.environ.get("LINKEDIN_CLIENT_ID", ""), os.environ.get("LINKEDIN_CLIENT_SECRET", ""))


def get_linkedin_redirect_uri() -> str:
    return f"{_get_base_url().rstrip('/')}/api/v1/oauth/callback/linkedin"


async def build_linkedin_authorize_url(user_id: str, agent_id: str = "") -> str:
    client_id, _ = await get_linkedin_creds()
    redirect_uri = get_linkedin_redirect_uri()
    state = make_state_token(user_id, agent_id, provider="linkedin")
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(LINKEDIN_SCOPES),
        "state": state,
    }
    return f"https://www.linkedin.com/oauth/v2/authorization?{urlencode(params)}"


async def revoke_and_delete_linkedin(user_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    return await db.auth_element_delete(user_id, "linkedin", "oauth")


# ── TikTok ────────────────────────────────────────────────────────────────

async def get_tiktok_creds() -> tuple[str, str]:
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, "tiktok_oauth_config", "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            cid = config.get("client_id", "")
            csec = elem.get("secret_ref", "") or config.get("client_secret", "")
            if cid and csec:
                return (cid, csec)
    except Exception as e:
        logger.debug("Failed to read TikTok creds from DB: %s", e)
    return (os.environ.get("TIKTOK_CLIENT_KEY", ""), os.environ.get("TIKTOK_CLIENT_SECRET", ""))


def get_tiktok_redirect_uri() -> str:
    return f"{_get_base_url().rstrip('/')}/api/v1/oauth/callback/tiktok"


async def build_tiktok_authorize_url(user_id: str, agent_id: str = "") -> str:
    client_id, _ = await get_tiktok_creds()
    redirect_uri = get_tiktok_redirect_uri()
    verifier, challenge = _pkce_pair()
    state = make_state_token(user_id, agent_id, provider="tiktok", pkce_verifier=verifier)
    params = {
        "client_key": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": ",".join(TIKTOK_SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"https://www.tiktok.com/v2/auth/authorize/?{urlencode(params)}"


async def revoke_and_delete_tiktok(user_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    elem = await db.auth_element_get(user_id, "tiktok", "oauth")
    if elem and elem.get("secret_ref"):
        try:
            tokens = json.loads(elem["secret_ref"])
            access_token = tokens.get("access_token", "")
            client_id, client_secret = await get_tiktok_creds()
            if access_token:
                async with httpx.AsyncClient() as c:
                    await c.post(
                        "https://open.tiktokapis.com/v2/oauth/revoke/",
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        data={"client_key": client_id, "client_secret": client_secret, "token": access_token},
                    )
        except Exception as e:
            logger.warning("Failed to revoke TikTok token: %s", e)
    return await db.auth_element_delete(user_id, "tiktok", "oauth")


# ── Pinterest ─────────────────────────────────────────────────────────────

async def get_pinterest_creds() -> tuple[str, str]:
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, "pinterest_oauth_config", "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            cid = config.get("client_id", "")
            csec = elem.get("secret_ref", "") or config.get("client_secret", "")
            if cid and csec:
                return (cid, csec)
    except Exception as e:
        logger.debug("Failed to read Pinterest creds from DB: %s", e)
    return (os.environ.get("PINTEREST_APP_ID", ""), os.environ.get("PINTEREST_APP_SECRET", ""))


def get_pinterest_redirect_uri() -> str:
    return f"{_get_base_url().rstrip('/')}/api/v1/oauth/callback/pinterest"


async def build_pinterest_authorize_url(user_id: str, agent_id: str = "") -> str:
    client_id, _ = await get_pinterest_creds()
    redirect_uri = get_pinterest_redirect_uri()
    state = make_state_token(user_id, agent_id, provider="pinterest")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": ",".join(PINTEREST_SCOPES),
        "state": state,
    }
    return f"https://www.pinterest.com/oauth/?{urlencode(params)}"


async def revoke_and_delete_pinterest(user_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    return await db.auth_element_delete(user_id, "pinterest", "oauth")


# ── Reddit ────────────────────────────────────────────────────────────────

async def get_reddit_creds() -> tuple[str, str]:
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, "reddit_oauth_config", "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            cid = config.get("client_id", "")
            csec = elem.get("secret_ref", "") or config.get("client_secret", "")
            if cid and csec:
                return (cid, csec)
    except Exception as e:
        logger.debug("Failed to read Reddit creds from DB: %s", e)
    return (os.environ.get("REDDIT_CLIENT_ID", ""), os.environ.get("REDDIT_CLIENT_SECRET", ""))


def get_reddit_redirect_uri() -> str:
    return f"{_get_base_url().rstrip('/')}/api/v1/oauth/callback/reddit"


async def build_reddit_authorize_url(user_id: str, agent_id: str = "") -> str:
    client_id, _ = await get_reddit_creds()
    redirect_uri = get_reddit_redirect_uri()
    state = make_state_token(user_id, agent_id, provider="reddit")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(REDDIT_SCOPES),
        "state": state,
        "duration": "permanent",
    }
    return f"https://www.reddit.com/api/v1/authorize?{urlencode(params)}"


async def revoke_and_delete_reddit(user_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    elem = await db.auth_element_get(user_id, "reddit", "oauth")
    if elem and elem.get("secret_ref"):
        try:
            tokens = json.loads(elem["secret_ref"])
            access_token = tokens.get("access_token", "")
            client_id, client_secret = await get_reddit_creds()
            if access_token:
                creds_b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
                async with httpx.AsyncClient() as c:
                    await c.post(
                        "https://www.reddit.com/api/v1/revoke_token",
                        headers={"Authorization": f"Basic {creds_b64}", "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "webAgent/1.0"},
                        data={"token": access_token, "token_type_hint": "access_token"},
                    )
        except Exception as e:
            logger.warning("Failed to revoke Reddit token: %s", e)
    return await db.auth_element_delete(user_id, "reddit", "oauth")


# ── Snapchat ──────────────────────────────────────────────────────────────

async def get_snapchat_creds() -> tuple[str, str]:
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, "snapchat_oauth_config", "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            cid = config.get("client_id", "")
            csec = elem.get("secret_ref", "") or config.get("client_secret", "")
            if cid and csec:
                return (cid, csec)
    except Exception as e:
        logger.debug("Failed to read Snapchat creds from DB: %s", e)
    return (os.environ.get("SNAPCHAT_CLIENT_ID", ""), os.environ.get("SNAPCHAT_CLIENT_SECRET", ""))


def get_snapchat_redirect_uri() -> str:
    return f"{_get_base_url().rstrip('/')}/api/v1/oauth/callback/snapchat"


async def build_snapchat_authorize_url(user_id: str, agent_id: str = "") -> str:
    client_id, _ = await get_snapchat_creds()
    redirect_uri = get_snapchat_redirect_uri()
    state = make_state_token(user_id, agent_id, provider="snapchat")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SNAPCHAT_SCOPES),
        "state": state,
    }
    return f"https://accounts.snapchat.com/login/oauth2/authorize?{urlencode(params)}"


async def revoke_and_delete_snapchat(user_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    elem = await db.auth_element_get(user_id, "snapchat", "oauth")
    if elem and elem.get("secret_ref"):
        try:
            tokens = json.loads(elem["secret_ref"])
            access_token = tokens.get("access_token", "")
            if access_token:
                async with httpx.AsyncClient() as c:
                    await c.post(
                        "https://accounts.snapchat.com/login/oauth2/revoke_token",
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        data={"token": access_token},
                    )
        except Exception as e:
            logger.warning("Failed to revoke Snapchat token: %s", e)
    return await db.auth_element_delete(user_id, "snapchat", "oauth")


# ── Twitch ────────────────────────────────────────────────────────────────

async def get_twitch_creds() -> tuple[str, str]:
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, "twitch_oauth_config", "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            cid = config.get("client_id", "")
            csec = elem.get("secret_ref", "") or config.get("client_secret", "")
            if cid and csec:
                return (cid, csec)
    except Exception as e:
        logger.debug("Failed to read Twitch creds from DB: %s", e)
    return (os.environ.get("TWITCH_CLIENT_ID", ""), os.environ.get("TWITCH_CLIENT_SECRET", ""))


def get_twitch_redirect_uri() -> str:
    return f"{_get_base_url().rstrip('/')}/api/v1/oauth/callback/twitch"


async def build_twitch_authorize_url(user_id: str, agent_id: str = "") -> str:
    client_id, _ = await get_twitch_creds()
    redirect_uri = get_twitch_redirect_uri()
    state = make_state_token(user_id, agent_id, provider="twitch")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(TWITCH_SCOPES),
        "state": state,
    }
    return f"https://id.twitch.tv/oauth2/authorize?{urlencode(params)}"


async def revoke_and_delete_twitch(user_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    elem = await db.auth_element_get(user_id, "twitch", "oauth")
    if elem and elem.get("secret_ref"):
        try:
            tokens = json.loads(elem["secret_ref"])
            access_token = tokens.get("access_token", "")
            client_id, _ = await get_twitch_creds()
            if access_token and client_id:
                async with httpx.AsyncClient() as c:
                    await c.post(
                        "https://id.twitch.tv/oauth2/revoke",
                        params={"client_id": client_id, "token": access_token},
                    )
        except Exception as e:
            logger.warning("Failed to revoke Twitch token: %s", e)
    return await db.auth_element_delete(user_id, "twitch", "oauth")


# ── Admin endpoints ──────────────────────────────────────────────────────

class OAuthConfigRequest(BaseModel):
    client_id: str
    client_secret: str

# Keep old name for backward compat
GoogleOAuthConfigRequest = OAuthConfigRequest


@router.get("")
async def get_integration_config(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Admin check: returns config status for all OAuth providers."""
    def _mask(s: str) -> str:
        return s[:20] + "..." if len(s) > 20 else s

    google_cid, google_csec   = await get_google_creds()
    ms_cid, ms_csec           = await get_microsoft_creds()
    yahoo_cid, yahoo_csec     = await get_yahoo_creds()
    dbx_cid, dbx_csec         = await get_dropbox_creds()
    meta_cid, meta_csec       = await get_meta_creds()
    tw_cid, tw_csec           = await get_twitter_creds()
    li_cid, li_csec           = await get_linkedin_creds()
    tt_cid, tt_csec           = await get_tiktok_creds()
    pin_cid, pin_csec         = await get_pinterest_creds()
    red_cid, red_csec         = await get_reddit_creds()
    snap_cid, snap_csec       = await get_snapchat_creds()
    twitch_cid, twitch_csec   = await get_twitch_creds()

    return {
        # Productivity
        "google_configured":    bool(google_cid and google_csec),
        "google_client_id":     _mask(google_cid),
        "redirect_uri":         get_redirect_uri(),
        "microsoft_configured": bool(ms_cid and ms_csec),
        "microsoft_client_id":  _mask(ms_cid),
        "microsoft_redirect_uri": get_microsoft_redirect_uri(),
        "yahoo_configured":     bool(yahoo_cid and yahoo_csec),
        "yahoo_client_id":      _mask(yahoo_cid),
        "yahoo_redirect_uri":   get_yahoo_redirect_uri(),
        "dropbox_configured":   bool(dbx_cid and dbx_csec),
        "dropbox_client_id":    _mask(dbx_cid),
        "dropbox_redirect_uri": get_dropbox_redirect_uri(),
        # Social media
        "meta_configured":      bool(meta_cid and meta_csec),
        "meta_client_id":       _mask(meta_cid),
        "meta_redirect_uri":    get_meta_redirect_uri(),
        "twitter_configured":   bool(tw_cid and tw_csec),
        "twitter_client_id":    _mask(tw_cid),
        "twitter_redirect_uri": get_twitter_redirect_uri(),
        "linkedin_configured":  bool(li_cid and li_csec),
        "linkedin_client_id":   _mask(li_cid),
        "linkedin_redirect_uri": get_linkedin_redirect_uri(),
        "tiktok_configured":    bool(tt_cid and tt_csec),
        "tiktok_client_id":     _mask(tt_cid),
        "tiktok_redirect_uri":  get_tiktok_redirect_uri(),
        "pinterest_configured": bool(pin_cid and pin_csec),
        "pinterest_client_id":  _mask(pin_cid),
        "pinterest_redirect_uri": get_pinterest_redirect_uri(),
        "reddit_configured":    bool(red_cid and red_csec),
        "reddit_client_id":     _mask(red_cid),
        "reddit_redirect_uri":  get_reddit_redirect_uri(),
        "snapchat_configured":  bool(snap_cid and snap_csec),
        "snapchat_client_id":   _mask(snap_cid),
        "snapchat_redirect_uri": get_snapchat_redirect_uri(),
        "twitch_configured":    bool(twitch_cid and twitch_csec),
        "twitch_client_id":     _mask(twitch_cid),
        "twitch_redirect_uri":  get_twitch_redirect_uri(),
    }


@router.post("/google")
async def save_google_config(
    req: OAuthConfigRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Save Google OAuth credentials (admin). Stored in auth_elements table."""
    from app.db import get_db
    db = get_db()
    await db.auth_element_set(
        user_id=_ADMIN_USER,
        service="google_oauth_config",
        config={"client_id": req.client_id.strip()},
        secret_ref=req.client_secret.strip(),
        label="default",
    )
    logger.info("Google OAuth config saved by admin")
    return {"status": "ok", "message": "Google OAuth configured."}


@router.delete("/google")
async def delete_google_config(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Remove Google OAuth credentials (admin). Does not revoke user tokens."""
    from app.db import get_db
    db = get_db()
    deleted = await db.auth_element_delete(_ADMIN_USER, "google_oauth_config", "default")
    logger.info("Google OAuth config removed by admin (deleted=%s)", deleted)
    return {"status": "ok", "deleted": deleted}


@router.post("/microsoft")
async def save_microsoft_config(
    req: OAuthConfigRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Save Microsoft OAuth credentials (admin)."""
    from app.db import get_db
    db = get_db()
    await db.auth_element_set(
        user_id=_ADMIN_USER,
        service="microsoft_oauth_config",
        config={"client_id": req.client_id.strip()},
        secret_ref=req.client_secret.strip(),
        label="default",
    )
    logger.info("Microsoft OAuth config saved by admin")
    return {"status": "ok", "message": "Microsoft OAuth configured."}


@router.delete("/microsoft")
async def delete_microsoft_config(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Remove Microsoft OAuth credentials (admin)."""
    from app.db import get_db
    db = get_db()
    deleted = await db.auth_element_delete(_ADMIN_USER, "microsoft_oauth_config", "default")
    logger.info("Microsoft OAuth config removed by admin (deleted=%s)", deleted)
    return {"status": "ok", "deleted": deleted}


@router.post("/yahoo")
async def save_yahoo_config(
    req: OAuthConfigRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Save Yahoo OAuth credentials (admin)."""
    from app.db import get_db
    db = get_db()
    await db.auth_element_set(
        user_id=_ADMIN_USER,
        service="yahoo_oauth_config",
        config={"client_id": req.client_id.strip()},
        secret_ref=req.client_secret.strip(),
        label="default",
    )
    logger.info("Yahoo OAuth config saved by admin")
    return {"status": "ok", "message": "Yahoo OAuth configured."}


@router.delete("/yahoo")
async def delete_yahoo_config(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Remove Yahoo OAuth credentials (admin)."""
    from app.db import get_db
    db = get_db()
    deleted = await db.auth_element_delete(_ADMIN_USER, "yahoo_oauth_config", "default")
    logger.info("Yahoo OAuth config removed by admin (deleted=%s)", deleted)
    return {"status": "ok", "deleted": deleted}


@router.post("/dropbox")
async def save_dropbox_config(
    req: OAuthConfigRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Save Dropbox OAuth credentials (admin)."""
    from app.db import get_db
    db = get_db()
    await db.auth_element_set(
        user_id=_ADMIN_USER,
        service="dropbox_oauth_config",
        config={"client_id": req.client_id.strip()},
        secret_ref=req.client_secret.strip(),
        label="default",
    )
    logger.info("Dropbox OAuth config saved by admin")
    return {"status": "ok", "message": "Dropbox OAuth configured."}


@router.delete("/dropbox")
async def delete_dropbox_config(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Remove Dropbox OAuth credentials (admin)."""
    from app.db import get_db
    db = get_db()
    deleted = await db.auth_element_delete(_ADMIN_USER, "dropbox_oauth_config", "default")
    logger.info("Dropbox OAuth config removed by admin (deleted=%s)", deleted)
    return {"status": "ok", "deleted": deleted}


# ── Social media admin CRUD endpoints ────────────────────────────────────

def _make_social_endpoints(provider: str, config_key: str, label: str):
    """Factory — generates save + delete admin endpoints for a social provider."""

    async def _save(
        req: OAuthConfigRequest,
        authorization: Optional[str] = Header(None),
        token: Optional[str] = Query(None),
    ):
        from app.db import get_db
        db = get_db()
        await db.auth_element_set(
            user_id=_ADMIN_USER,
            service=config_key,
            config={"client_id": req.client_id.strip()},
            secret_ref=req.client_secret.strip(),
            label="default",
        )
        logger.info("%s OAuth config saved by admin", label)
        return {"status": "ok", "message": f"{label} OAuth configured."}

    async def _delete(
        authorization: Optional[str] = Header(None),
        token: Optional[str] = Query(None),
    ):
        from app.db import get_db
        db = get_db()
        deleted = await db.auth_element_delete(_ADMIN_USER, config_key, "default")
        logger.info("%s OAuth config removed by admin (deleted=%s)", label, deleted)
        return {"status": "ok", "deleted": deleted}

    return _save, _delete


_meta_save, _meta_delete         = _make_social_endpoints("meta",       "meta_oauth_config",      "Meta")
_tw_save,   _tw_delete           = _make_social_endpoints("twitter",    "twitter_oauth_config",   "Twitter/X")
_li_save,   _li_delete           = _make_social_endpoints("linkedin",   "linkedin_oauth_config",  "LinkedIn")
_tt_save,   _tt_delete           = _make_social_endpoints("tiktok",     "tiktok_oauth_config",    "TikTok")
_pin_save,  _pin_delete          = _make_social_endpoints("pinterest",  "pinterest_oauth_config", "Pinterest")
_red_save,  _red_delete          = _make_social_endpoints("reddit",     "reddit_oauth_config",    "Reddit")
_snap_save, _snap_delete         = _make_social_endpoints("snapchat",   "snapchat_oauth_config",  "Snapchat")
_twitch_save, _twitch_delete     = _make_social_endpoints("twitch",     "twitch_oauth_config",    "Twitch")

router.post("/meta",      response_model=None)(_meta_save)
router.delete("/meta",    response_model=None)(_meta_delete)
router.post("/twitter",   response_model=None)(_tw_save)
router.delete("/twitter", response_model=None)(_tw_delete)
router.post("/linkedin",  response_model=None)(_li_save)
router.delete("/linkedin",response_model=None)(_li_delete)
router.post("/tiktok",    response_model=None)(_tt_save)
router.delete("/tiktok",  response_model=None)(_tt_delete)
router.post("/pinterest", response_model=None)(_pin_save)
router.delete("/pinterest",response_model=None)(_pin_delete)
router.post("/reddit",    response_model=None)(_red_save)
router.delete("/reddit",  response_model=None)(_red_delete)
router.post("/snapchat",  response_model=None)(_snap_save)
router.delete("/snapchat",response_model=None)(_snap_delete)
router.post("/twitch",    response_model=None)(_twitch_save)
router.delete("/twitch",  response_model=None)(_twitch_delete)

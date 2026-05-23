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

  Marketplaces
  - eBay       (Buy + Sell APIs)
  - Etsy       (Open API v3)
  - Shopify    (Admin API, per-shop)
  - Amazon     (Selling Partner API, LWA OAuth)
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
from fastapi import APIRouter, Header, Query, Request
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

# ── Marketplace scopes ────────────────────────────────────────────────────

EBAY_SCOPES = [
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/buy.order.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.inventory.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.account.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.marketing",
    "https://api.ebay.com/oauth/api_scope/sell.marketing.readonly",
]

ETSY_SCOPES = [
    "email_r",
    "profile_r",
    "shops_r",
    "shops_w",
    "listings_r",
    "listings_w",
    "listings_d",
    "transactions_r",
]

# Shopify scopes are per-shop / per-install — these are the defaults requested
# at install time. Tweak in the admin UI to match the app's Partner config.
SHOPIFY_SCOPES = [
    "read_products",
    "write_products",
    "read_inventory",
    "write_inventory",
    "read_orders",
    "write_orders",
    "read_locations",
]

# Amazon SP-API uses a single LWA scope; the actual roles (Orders, Inventory,
# Pricing, etc.) are granted via the Seller Central app registration, not OAuth.
AMAZON_SCOPES = [
    "sellingpartnerapi::client_credential:refresh_token",
]

# Map each provider to its config service key and default scope list
_PROVIDER_SCOPE_DEFAULTS: dict[str, tuple[str, list]] = {
    "google":    ("google_oauth_config",    GOOGLE_SCOPES),
    "microsoft": ("microsoft_oauth_config", MICROSOFT_SCOPES),
    "yahoo":     ("yahoo_oauth_config",     YAHOO_SCOPES),
    "dropbox":   ("dropbox_oauth_config",   DROPBOX_SCOPES),
    "meta":      ("meta_oauth_config",      META_SCOPES),
    "twitter":   ("twitter_oauth_config",   TWITTER_SCOPES),
    "linkedin":  ("linkedin_oauth_config",  LINKEDIN_SCOPES),
    "tiktok":    ("tiktok_oauth_config",    TIKTOK_SCOPES),
    "pinterest": ("pinterest_oauth_config", PINTEREST_SCOPES),
    "reddit":    ("reddit_oauth_config",    REDDIT_SCOPES),
    "snapchat":  ("snapchat_oauth_config",  SNAPCHAT_SCOPES),
    "twitch":    ("twitch_oauth_config",    TWITCH_SCOPES),
    "ebay":      ("ebay_oauth_config",      EBAY_SCOPES),
    "etsy":      ("etsy_oauth_config",      ETSY_SCOPES),
    "shopify":   ("shopify_oauth_config",   SHOPIFY_SCOPES),
    "amazon":    ("amazon_oauth_config",    AMAZON_SCOPES),
}


async def _get_enabled_scopes(config_service: str, default_scopes: list) -> list:
    """Return admin-configured scopes for a provider, or the full default list."""
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, config_service, "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            scopes = config.get("enabled_scopes")
            if scopes and isinstance(scopes, list) and len(scopes) > 0:
                return scopes
    except Exception:
        pass
    return default_scopes


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


_LAST_SEEN_BASE_URL: str = ""


def _get_base_url(request: Optional[Request] = None) -> str:
    """Return the configured base URL.

    Priority order:
    1. WEBHOOK_BASE_URL env var (explicit override)
    2. webhook_base_url.txt file
    3. Derived from the incoming HTTP request (scheme + host) — works correctly
       on Cloud Run and any other hosted environment without extra config
    4. Last-seen request-derived base URL (cached so background/agent code paths
       that have no Request object still produce the right URL instead of localhost)
    5. Fallback to http://localhost:8000
    """
    global _LAST_SEEN_BASE_URL
    from pathlib import Path
    base_url = os.environ.get("WEBHOOK_BASE_URL", "")
    if not base_url:
        try:
            wh_file = Path(__file__).resolve().parent.parent.parent / "webhook_base_url.txt"
            if wh_file.exists():
                base_url = wh_file.read_text().strip()
        except Exception:
            pass
    if not base_url and request is not None:
        # Derive from the incoming request. Behind a TLS-terminating proxy (e.g.
        # Cloud Run, Caddy), the app sees http:// internally but the real scheme
        # is in the X-Forwarded-Proto header — use that when present.
        derived = str(request.base_url).rstrip("/")
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        if forwarded_proto and derived.startswith("http://"):
            derived = "https://" + derived[len("http://"):]
        base_url = derived
    if base_url:
        # Remember the most recent base so request-less callers (agent tools,
        # scheduler) can reuse it. Never let a localhost/loopback hit overwrite
        # a real public URL already cached (production internal pings shouldn't
        # poison the cache), but localhost CAN overwrite an empty cache so
        # local dev still works.
        is_local = base_url.startswith("http://localhost") or base_url.startswith("http://127.")
        cache_is_public = bool(_LAST_SEEN_BASE_URL) and not (
            _LAST_SEEN_BASE_URL.startswith("http://localhost")
            or _LAST_SEEN_BASE_URL.startswith("http://127.")
        )
        if not (is_local and cache_is_public):
            _LAST_SEEN_BASE_URL = base_url
        return base_url
    if _LAST_SEEN_BASE_URL:
        return _LAST_SEEN_BASE_URL
    return "http://localhost:8000"


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


def get_redirect_uri(request: Optional[Request] = None) -> str:
    return f"{_get_base_url(request).rstrip('/')}/api/v1/oauth/callback/google"


async def build_google_authorize_url(user_id: str, agent_id: str = "", request: Optional[Request] = None) -> str:
    """Build the full Google OAuth authorization URL."""
    client_id, _ = await get_google_creds()
    redirect_uri = get_redirect_uri(request)
    state = make_state_token(user_id, agent_id, provider="google")
    scopes = await _get_enabled_scopes("google_oauth_config", GOOGLE_SCOPES)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
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


def get_microsoft_redirect_uri(request: Optional[Request] = None) -> str:
    return f"{_get_base_url(request).rstrip('/')}/api/v1/oauth/callback/microsoft"


async def build_microsoft_authorize_url(user_id: str, agent_id: str = "") -> str:
    """Build the full Microsoft OAuth authorization URL (common / multi-tenant)."""
    client_id, _ = await get_microsoft_creds()
    redirect_uri = get_microsoft_redirect_uri()
    state = make_state_token(user_id, agent_id, provider="microsoft")
    scopes = await _get_enabled_scopes("microsoft_oauth_config", MICROSOFT_SCOPES)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
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


def get_yahoo_redirect_uri(request: Optional[Request] = None) -> str:
    return f"{_get_base_url(request).rstrip('/')}/api/v1/oauth/callback/yahoo"


async def build_yahoo_authorize_url(user_id: str, agent_id: str = "") -> str:
    """Build the Yahoo OAuth authorization URL."""
    client_id, _ = await get_yahoo_creds()
    redirect_uri = get_yahoo_redirect_uri()
    state = make_state_token(user_id, agent_id, provider="yahoo")
    scopes = await _get_enabled_scopes("yahoo_oauth_config", YAHOO_SCOPES)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
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


def get_dropbox_redirect_uri(request: Optional[Request] = None) -> str:
    return f"{_get_base_url(request).rstrip('/')}/api/v1/oauth/callback/dropbox"


async def build_dropbox_authorize_url(user_id: str, agent_id: str = "") -> str:
    """Build the Dropbox OAuth authorization URL."""
    client_id, _ = await get_dropbox_creds()
    redirect_uri = get_dropbox_redirect_uri()
    state = make_state_token(user_id, agent_id, provider="dropbox")
    scopes = await _get_enabled_scopes("dropbox_oauth_config", DROPBOX_SCOPES)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
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


def get_meta_redirect_uri(request: Optional[Request] = None) -> str:
    return f"{_get_base_url(request).rstrip('/')}/api/v1/oauth/callback/meta"


async def build_meta_authorize_url(user_id: str, agent_id: str = "") -> str:
    client_id, _ = await get_meta_creds()
    redirect_uri = get_meta_redirect_uri()
    state = make_state_token(user_id, agent_id, provider="meta")
    scopes = await _get_enabled_scopes("meta_oauth_config", META_SCOPES)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": ",".join(scopes),
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


def get_twitter_redirect_uri(request: Optional[Request] = None) -> str:
    return f"{_get_base_url(request).rstrip('/')}/api/v1/oauth/callback/twitter"


async def build_twitter_authorize_url(user_id: str, agent_id: str = "") -> tuple[str, str]:
    """Returns (authorize_url, state_token). Twitter requires PKCE."""
    client_id, _ = await get_twitter_creds()
    redirect_uri = get_twitter_redirect_uri()
    verifier, challenge = _pkce_pair()
    state = make_state_token(user_id, agent_id, provider="twitter", pkce_verifier=verifier)
    scopes = await _get_enabled_scopes("twitter_oauth_config", TWITTER_SCOPES)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
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


def get_linkedin_redirect_uri(request: Optional[Request] = None) -> str:
    return f"{_get_base_url(request).rstrip('/')}/api/v1/oauth/callback/linkedin"


async def build_linkedin_authorize_url(user_id: str, agent_id: str = "") -> str:
    client_id, _ = await get_linkedin_creds()
    redirect_uri = get_linkedin_redirect_uri()
    state = make_state_token(user_id, agent_id, provider="linkedin")
    scopes = await _get_enabled_scopes("linkedin_oauth_config", LINKEDIN_SCOPES)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
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


def get_tiktok_redirect_uri(request: Optional[Request] = None) -> str:
    return f"{_get_base_url(request).rstrip('/')}/api/v1/oauth/callback/tiktok"


async def build_tiktok_authorize_url(user_id: str, agent_id: str = "") -> str:
    client_id, _ = await get_tiktok_creds()
    redirect_uri = get_tiktok_redirect_uri()
    verifier, challenge = _pkce_pair()
    state = make_state_token(user_id, agent_id, provider="tiktok", pkce_verifier=verifier)
    scopes = await _get_enabled_scopes("tiktok_oauth_config", TIKTOK_SCOPES)
    params = {
        "client_key": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": ",".join(scopes),
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


def get_pinterest_redirect_uri(request: Optional[Request] = None) -> str:
    return f"{_get_base_url(request).rstrip('/')}/api/v1/oauth/callback/pinterest"


async def build_pinterest_authorize_url(user_id: str, agent_id: str = "") -> str:
    client_id, _ = await get_pinterest_creds()
    redirect_uri = get_pinterest_redirect_uri()
    state = make_state_token(user_id, agent_id, provider="pinterest")
    scopes = await _get_enabled_scopes("pinterest_oauth_config", PINTEREST_SCOPES)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": ",".join(scopes),
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


def get_reddit_redirect_uri(request: Optional[Request] = None) -> str:
    return f"{_get_base_url(request).rstrip('/')}/api/v1/oauth/callback/reddit"


async def build_reddit_authorize_url(user_id: str, agent_id: str = "") -> str:
    client_id, _ = await get_reddit_creds()
    redirect_uri = get_reddit_redirect_uri()
    state = make_state_token(user_id, agent_id, provider="reddit")
    scopes = await _get_enabled_scopes("reddit_oauth_config", REDDIT_SCOPES)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
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


def get_snapchat_redirect_uri(request: Optional[Request] = None) -> str:
    return f"{_get_base_url(request).rstrip('/')}/api/v1/oauth/callback/snapchat"


async def build_snapchat_authorize_url(user_id: str, agent_id: str = "") -> str:
    client_id, _ = await get_snapchat_creds()
    redirect_uri = get_snapchat_redirect_uri()
    state = make_state_token(user_id, agent_id, provider="snapchat")
    scopes = await _get_enabled_scopes("snapchat_oauth_config", SNAPCHAT_SCOPES)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
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


def get_twitch_redirect_uri(request: Optional[Request] = None) -> str:
    return f"{_get_base_url(request).rstrip('/')}/api/v1/oauth/callback/twitch"


async def build_twitch_authorize_url(user_id: str, agent_id: str = "") -> str:
    client_id, _ = await get_twitch_creds()
    redirect_uri = get_twitch_redirect_uri()
    state = make_state_token(user_id, agent_id, provider="twitch")
    scopes = await _get_enabled_scopes("twitch_oauth_config", TWITCH_SCOPES)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
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


# ── eBay ──────────────────────────────────────────────────────────────────

async def get_ebay_creds() -> tuple[str, str]:
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, "ebay_oauth_config", "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            cid = config.get("client_id", "")
            csec = elem.get("secret_ref", "") or config.get("client_secret", "")
            if cid and csec:
                return (cid, csec)
    except Exception as e:
        logger.debug("Failed to read eBay creds from DB: %s", e)
    return (os.environ.get("EBAY_CLIENT_ID", ""), os.environ.get("EBAY_CLIENT_SECRET", ""))


def get_ebay_redirect_uri(request: Optional[Request] = None) -> str:
    return f"{_get_base_url(request).rstrip('/')}/api/v1/oauth/callback/ebay"


async def build_ebay_authorize_url(user_id: str, agent_id: str = "", request: Optional[Request] = None) -> str:
    client_id, _ = await get_ebay_creds()
    redirect_uri = get_ebay_redirect_uri(request)
    state = make_state_token(user_id, agent_id, provider="ebay")
    scopes = await _get_enabled_scopes("ebay_oauth_config", EBAY_SCOPES)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,  # eBay calls this the "RuName" but accepts the URI here for web flows
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
    }
    return f"https://auth.ebay.com/oauth2/authorize?{urlencode(params)}"


async def revoke_and_delete_ebay(user_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    return await db.auth_element_delete(user_id, "ebay", "oauth")


# ── Etsy ──────────────────────────────────────────────────────────────────

async def get_etsy_creds() -> tuple[str, str]:
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, "etsy_oauth_config", "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            cid = config.get("client_id", "")
            csec = elem.get("secret_ref", "") or config.get("client_secret", "")
            if cid and csec:
                return (cid, csec)
    except Exception as e:
        logger.debug("Failed to read Etsy creds from DB: %s", e)
    return (os.environ.get("ETSY_CLIENT_ID", ""), os.environ.get("ETSY_CLIENT_SECRET", ""))


def get_etsy_redirect_uri(request: Optional[Request] = None) -> str:
    return f"{_get_base_url(request).rstrip('/')}/api/v1/oauth/callback/etsy"


async def build_etsy_authorize_url(user_id: str, agent_id: str = "", request: Optional[Request] = None) -> str:
    """Etsy uses OAuth 2.0 + PKCE. Stash the verifier in the state JWT."""
    client_id, _ = await get_etsy_creds()
    redirect_uri = get_etsy_redirect_uri(request)
    verifier, challenge = _pkce_pair()
    state = make_state_token(user_id, agent_id, provider="etsy", pkce_verifier=verifier)
    scopes = await _get_enabled_scopes("etsy_oauth_config", ETSY_SCOPES)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"https://www.etsy.com/oauth/connect?{urlencode(params)}"


async def revoke_and_delete_etsy(user_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    return await db.auth_element_delete(user_id, "etsy", "oauth")


# ── Shopify (per-shop) ────────────────────────────────────────────────────

async def get_shopify_creds() -> tuple[str, str]:
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, "shopify_oauth_config", "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            cid = config.get("client_id", "")
            csec = elem.get("secret_ref", "") or config.get("client_secret", "")
            if cid and csec:
                return (cid, csec)
    except Exception as e:
        logger.debug("Failed to read Shopify creds from DB: %s", e)
    return (os.environ.get("SHOPIFY_API_KEY", ""), os.environ.get("SHOPIFY_API_SECRET", ""))


def get_shopify_redirect_uri(request: Optional[Request] = None) -> str:
    return f"{_get_base_url(request).rstrip('/')}/api/v1/oauth/callback/shopify"


def _shopify_normalize_shop(shop: str) -> str:
    """Normalize 'my-store' or 'my-store.myshopify.com' → 'my-store.myshopify.com'."""
    s = (shop or "").strip().lower().replace("https://", "").replace("http://", "").rstrip("/")
    if "." not in s:
        s = f"{s}.myshopify.com"
    return s


async def build_shopify_authorize_url(
    user_id: str,
    shop: str,
    agent_id: str = "",
    request: Optional[Request] = None,
) -> str:
    """Build the per-shop Shopify install URL. Caller must supply the shop domain."""
    if not shop:
        raise ValueError("shop domain required for Shopify authorize")
    shop_norm = _shopify_normalize_shop(shop)
    client_id, _ = await get_shopify_creds()
    redirect_uri = get_shopify_redirect_uri(request)
    state = make_state_token(user_id, agent_id, provider="shopify", shop=shop_norm)
    scopes = await _get_enabled_scopes("shopify_oauth_config", SHOPIFY_SCOPES)
    params = {
        "client_id": client_id,
        "scope": ",".join(scopes),
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"https://{shop_norm}/admin/oauth/authorize?{urlencode(params)}"


async def revoke_and_delete_shopify(user_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    return await db.auth_element_delete(user_id, "shopify", "oauth")


# ── Amazon SP-API (LWA) ───────────────────────────────────────────────────

async def get_amazon_creds() -> tuple[str, str]:
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, "amazon_oauth_config", "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            cid = config.get("client_id", "")
            csec = elem.get("secret_ref", "") or config.get("client_secret", "")
            if cid and csec:
                return (cid, csec)
    except Exception as e:
        logger.debug("Failed to read Amazon creds from DB: %s", e)
    return (os.environ.get("AMAZON_LWA_CLIENT_ID", ""), os.environ.get("AMAZON_LWA_CLIENT_SECRET", ""))


def get_amazon_redirect_uri(request: Optional[Request] = None) -> str:
    return f"{_get_base_url(request).rstrip('/')}/api/v1/oauth/callback/amazon"


async def _get_amazon_app_id() -> str:
    """The Seller Central application id (separate from the LWA client_id).

    Stored in admin config alongside the LWA creds.
    """
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, "amazon_oauth_config", "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            return config.get("app_id", "") or os.environ.get("AMAZON_SP_APP_ID", "")
    except Exception:
        pass
    return os.environ.get("AMAZON_SP_APP_ID", "")


async def build_amazon_authorize_url(
    user_id: str,
    region: str = "NA",
    agent_id: str = "",
    request: Optional[Request] = None,
) -> str:
    """Build the Amazon Seller Central authorization URL.

    Sellers complete the OAuth handshake on Seller Central (region-specific),
    then Amazon redirects back to our LWA callback with a spapi_oauth_code.
    """
    app_id = await _get_amazon_app_id()
    redirect_uri = get_amazon_redirect_uri(request)
    state = make_state_token(user_id, agent_id, provider="amazon", region=region.upper())
    # Seller Central hosts the consent screen, not LWA directly.
    sc_hosts = {
        "NA": "https://sellercentral.amazon.com",
        "EU": "https://sellercentral.amazon.co.uk",
        "FE": "https://sellercentral.amazon.co.jp",
    }
    host = sc_hosts.get(region.upper(), sc_hosts["NA"])
    params = {
        "application_id": app_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "version": "beta",
    }
    return f"{host}/apps/authorize/consent?{urlencode(params)}"


async def revoke_and_delete_amazon(user_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    return await db.auth_element_delete(user_id, "amazon", "oauth")


# ── Generic Web Scraper (admin-global) ───────────────────────────────────

async def get_scraper_creds() -> Optional[dict]:
    """Return the admin-configured scraper provider config, or None."""
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, "scraper_config", "default")
        if not elem:
            return None
        config = elem.get("config", {})
        if isinstance(config, str):
            config = json.loads(config)
        api_key = elem.get("secret_ref", "") or config.get("api_key", "")
        if not api_key:
            return None
        return {
            "provider": (config.get("provider") or "apify").lower(),
            "api_key": api_key,
            "endpoint": config.get("endpoint", ""),
            "extra": {
                "actor_id": config.get("actor_id", ""),
                "host": config.get("host", ""),
            },
        }
    except Exception as e:
        logger.debug("Failed to read scraper creds from DB: %s", e)
        return None


# ── Browser Session (per-user) ───────────────────────────────────────────

async def get_browser_session_creds(user_id: str) -> Optional[dict]:
    """Return the per-user browser-session blob, or None."""
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(user_id, "browser_session", "default")
        if not elem:
            return None
        config = elem.get("config", {})
        if isinstance(config, str):
            config = json.loads(config)
        cookies = elem.get("secret_ref", "")
        if not cookies:
            return None
        return {
            "domain": config.get("domain", ""),
            "user_agent": config.get("user_agent", ""),
            "saved_at": config.get("saved_at", ""),
            "cookies": cookies,
        }
    except Exception as e:
        logger.debug("Failed to read browser session for %s: %s", user_id, e)
        return None


# ── Connection → admin-config mapping ─────────────────────────────────────
#
# Each agent-page connection_type maps to the auth_elements service key the
# admin uses to store OAuth credentials. Providers in `_NO_ADMIN_CONFIG_NEEDED`
# require no admin OAuth setup — their configuration lives per-agent (telegram
# bot token) or per-user (browser_session cookies) and they are always treated
# as "configured" so the agent page can surface them.
_CONNECTION_ADMIN_CONFIG_KEY: dict[str, str] = {
    "google":    "google_oauth_config",
    "microsoft": "microsoft_oauth_config",
    "yahoo":     "yahoo_oauth_config",
    "dropbox":   "dropbox_oauth_config",
    "facebook":  "meta_oauth_config",
    "instagram": "meta_oauth_config",
    "twitter":   "twitter_oauth_config",
    "linkedin":  "linkedin_oauth_config",
    "tiktok":    "tiktok_oauth_config",
    "pinterest": "pinterest_oauth_config",
    "reddit":    "reddit_oauth_config",
    "snapchat":  "snapchat_oauth_config",
    "twitch":    "twitch_oauth_config",
    "ebay":      "ebay_oauth_config",
    "etsy":      "etsy_oauth_config",
    "shopify":   "shopify_oauth_config",
    "amazon":    "amazon_oauth_config",
    "scraper":   "scraper_config",
}

_NO_ADMIN_CONFIG_NEEDED: set[str] = {"telegram", "browser_session"}


async def get_admin_configured_providers() -> set[str]:
    """Return the set of connection_types the admin has configured (or that need no admin config).

    A provider counts as configured when:
    - It is in `_NO_ADMIN_CONFIG_NEEDED` (per-agent or per-user setup), OR
    - Its `auth_elements` row exists with a non-empty `secret_ref` (the
      client secret / API key).

    This is the source of truth for which integrations the agent page may
    show as toggleable and which provider tools may be exposed to the LLM.
    """
    configured: set[str] = set(_NO_ADMIN_CONFIG_NEEDED)
    try:
        from app.db import get_db
        db = get_db()
    except Exception as e:
        logger.debug("Failed to import db while checking configured providers: %s", e)
        return configured

    # Cache per service_key — facebook & instagram share `meta_oauth_config`.
    seen: dict[str, bool] = {}
    for ct, service_key in _CONNECTION_ADMIN_CONFIG_KEY.items():
        if service_key in seen:
            if seen[service_key]:
                configured.add(ct)
            continue
        ok = False
        try:
            elem = await db.auth_element_get(_ADMIN_USER, service_key, "default")
            if elem and elem.get("secret_ref"):
                ok = True
        except Exception as e:
            logger.debug("auth_element_get failed for %s: %s", service_key, e)
        seen[service_key] = ok
        if ok:
            configured.add(ct)
    return configured


# ── Admin endpoints ──────────────────────────────────────────────────────

class OAuthConfigRequest(BaseModel):
    client_id: str
    client_secret: str
    scopes: Optional[list[str]] = None


class ScraperConfigRequest(BaseModel):
    provider: str = "apify"   # apify | rapidapi | custom_http
    api_key: str
    endpoint: Optional[str] = ""
    actor_id: Optional[str] = ""
    host: Optional[str] = ""


class BrowserSessionRequest(BaseModel):
    domain: Optional[str] = ""
    user_agent: Optional[str] = ""
    cookies: str

# Keep old name for backward compat
GoogleOAuthConfigRequest = OAuthConfigRequest


@router.get("")
async def get_integration_config(
    request: Request,
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
    ebay_cid, ebay_csec       = await get_ebay_creds()
    etsy_cid, etsy_csec       = await get_etsy_creds()
    shop_cid, shop_csec       = await get_shopify_creds()
    amz_cid, amz_csec         = await get_amazon_creds()
    scraper_cfg               = await get_scraper_creds()

    # Fetch enabled scopes for all providers (returns defaults when unconfigured)
    g_scopes    = await _get_enabled_scopes("google_oauth_config",    GOOGLE_SCOPES)
    ms_scopes   = await _get_enabled_scopes("microsoft_oauth_config", MICROSOFT_SCOPES)
    yh_scopes   = await _get_enabled_scopes("yahoo_oauth_config",     YAHOO_SCOPES)
    dbx_scopes  = await _get_enabled_scopes("dropbox_oauth_config",   DROPBOX_SCOPES)
    meta_scopes = await _get_enabled_scopes("meta_oauth_config",      META_SCOPES)
    tw_scopes   = await _get_enabled_scopes("twitter_oauth_config",   TWITTER_SCOPES)
    li_scopes   = await _get_enabled_scopes("linkedin_oauth_config",  LINKEDIN_SCOPES)
    tt_scopes   = await _get_enabled_scopes("tiktok_oauth_config",    TIKTOK_SCOPES)
    pin_scopes  = await _get_enabled_scopes("pinterest_oauth_config", PINTEREST_SCOPES)
    red_scopes  = await _get_enabled_scopes("reddit_oauth_config",    REDDIT_SCOPES)
    snap_scopes = await _get_enabled_scopes("snapchat_oauth_config",  SNAPCHAT_SCOPES)
    twit_scopes = await _get_enabled_scopes("twitch_oauth_config",    TWITCH_SCOPES)
    ebay_scopes = await _get_enabled_scopes("ebay_oauth_config",      EBAY_SCOPES)
    etsy_scopes = await _get_enabled_scopes("etsy_oauth_config",      ETSY_SCOPES)
    shop_scopes = await _get_enabled_scopes("shopify_oauth_config",   SHOPIFY_SCOPES)
    amz_scopes  = await _get_enabled_scopes("amazon_oauth_config",    AMAZON_SCOPES)

    return {
        # Productivity
        "google_configured":    bool(google_cid and google_csec),
        "google_client_id":     _mask(google_cid),
        "google_scopes":        g_scopes,
        "redirect_uri":         get_redirect_uri(request),
        "microsoft_configured": bool(ms_cid and ms_csec),
        "microsoft_client_id":  _mask(ms_cid),
        "microsoft_scopes":     ms_scopes,
        "microsoft_redirect_uri": get_microsoft_redirect_uri(request),
        "yahoo_configured":     bool(yahoo_cid and yahoo_csec),
        "yahoo_client_id":      _mask(yahoo_cid),
        "yahoo_scopes":         yh_scopes,
        "yahoo_redirect_uri":   get_yahoo_redirect_uri(request),
        "dropbox_configured":   bool(dbx_cid and dbx_csec),
        "dropbox_client_id":    _mask(dbx_cid),
        "dropbox_scopes":       dbx_scopes,
        "dropbox_redirect_uri": get_dropbox_redirect_uri(request),
        # Social media
        "meta_configured":      bool(meta_cid and meta_csec),
        "meta_client_id":       _mask(meta_cid),
        "meta_scopes":          meta_scopes,
        "meta_redirect_uri":    get_meta_redirect_uri(request),
        "twitter_configured":   bool(tw_cid and tw_csec),
        "twitter_client_id":    _mask(tw_cid),
        "twitter_scopes":       tw_scopes,
        "twitter_redirect_uri": get_twitter_redirect_uri(request),
        "linkedin_configured":  bool(li_cid and li_csec),
        "linkedin_client_id":   _mask(li_cid),
        "linkedin_scopes":      li_scopes,
        "linkedin_redirect_uri": get_linkedin_redirect_uri(request),
        "tiktok_configured":    bool(tt_cid and tt_csec),
        "tiktok_client_id":     _mask(tt_cid),
        "tiktok_scopes":        tt_scopes,
        "tiktok_redirect_uri":  get_tiktok_redirect_uri(request),
        "pinterest_configured": bool(pin_cid and pin_csec),
        "pinterest_client_id":  _mask(pin_cid),
        "pinterest_scopes":     pin_scopes,
        "pinterest_redirect_uri": get_pinterest_redirect_uri(request),
        "reddit_configured":    bool(red_cid and red_csec),
        "reddit_client_id":     _mask(red_cid),
        "reddit_scopes":        red_scopes,
        "reddit_redirect_uri":  get_reddit_redirect_uri(request),
        "snapchat_configured":  bool(snap_cid and snap_csec),
        "snapchat_client_id":   _mask(snap_cid),
        "snapchat_scopes":      snap_scopes,
        "snapchat_redirect_uri": get_snapchat_redirect_uri(request),
        "twitch_configured":    bool(twitch_cid and twitch_csec),
        "twitch_client_id":     _mask(twitch_cid),
        "twitch_scopes":        twit_scopes,
        "twitch_redirect_uri":  get_twitch_redirect_uri(request),
        # Marketplaces
        "ebay_configured":      bool(ebay_cid and ebay_csec),
        "ebay_client_id":       _mask(ebay_cid),
        "ebay_scopes":          ebay_scopes,
        "ebay_redirect_uri":    get_ebay_redirect_uri(request),
        "etsy_configured":      bool(etsy_cid and etsy_csec),
        "etsy_client_id":       _mask(etsy_cid),
        "etsy_scopes":          etsy_scopes,
        "etsy_redirect_uri":    get_etsy_redirect_uri(request),
        "shopify_configured":   bool(shop_cid and shop_csec),
        "shopify_client_id":    _mask(shop_cid),
        "shopify_scopes":       shop_scopes,
        "shopify_redirect_uri": get_shopify_redirect_uri(request),
        "amazon_configured":    bool(amz_cid and amz_csec),
        "amazon_client_id":     _mask(amz_cid),
        "amazon_scopes":        amz_scopes,
        "amazon_redirect_uri":  get_amazon_redirect_uri(request),
        # Generic, non-OAuth providers (no client_id/secret pair)
        "scraper_configured":   bool(scraper_cfg),
        "scraper_provider":     (scraper_cfg or {}).get("provider", ""),
        "scraper_endpoint":     (scraper_cfg or {}).get("endpoint", ""),
        "scraper_actor_id":     (scraper_cfg or {}).get("extra", {}).get("actor_id", ""),
        "scraper_host":         (scraper_cfg or {}).get("extra", {}).get("host", ""),
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
    cfg: dict = {"client_id": req.client_id.strip()}
    if req.scopes is not None:
        cfg["enabled_scopes"] = req.scopes
    await db.auth_element_set(
        user_id=_ADMIN_USER,
        service="google_oauth_config",
        config=cfg,
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
    cfg: dict = {"client_id": req.client_id.strip()}
    if req.scopes is not None:
        cfg["enabled_scopes"] = req.scopes
    await db.auth_element_set(
        user_id=_ADMIN_USER,
        service="microsoft_oauth_config",
        config=cfg,
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
    cfg: dict = {"client_id": req.client_id.strip()}
    if req.scopes is not None:
        cfg["enabled_scopes"] = req.scopes
    await db.auth_element_set(
        user_id=_ADMIN_USER,
        service="yahoo_oauth_config",
        config=cfg,
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
    cfg: dict = {"client_id": req.client_id.strip()}
    if req.scopes is not None:
        cfg["enabled_scopes"] = req.scopes
    await db.auth_element_set(
        user_id=_ADMIN_USER,
        service="dropbox_oauth_config",
        config=cfg,
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
        cfg: dict = {"client_id": req.client_id.strip()}
        if req.scopes is not None:
            cfg["enabled_scopes"] = req.scopes
        await db.auth_element_set(
            user_id=_ADMIN_USER,
            service=config_key,
            config=cfg,
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
_ebay_save,   _ebay_delete       = _make_social_endpoints("ebay",       "ebay_oauth_config",      "eBay")
_etsy_save,   _etsy_delete       = _make_social_endpoints("etsy",       "etsy_oauth_config",      "Etsy")
_shop_save,   _shop_delete       = _make_social_endpoints("shopify",    "shopify_oauth_config",   "Shopify")
_amz_save,    _amz_delete        = _make_social_endpoints("amazon",     "amazon_oauth_config",    "Amazon SP-API")

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
router.post("/ebay",      response_model=None)(_ebay_save)
router.delete("/ebay",    response_model=None)(_ebay_delete)
router.post("/etsy",      response_model=None)(_etsy_save)
router.delete("/etsy",    response_model=None)(_etsy_delete)
router.post("/shopify",   response_model=None)(_shop_save)
router.delete("/shopify", response_model=None)(_shop_delete)
router.post("/amazon",    response_model=None)(_amz_save)
router.delete("/amazon",  response_model=None)(_amz_delete)


# ── Generic Web Scraper config (admin) ─────────────────────────────────────

@router.post("/scraper")
async def save_scraper_config(
    req: ScraperConfigRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Save the global third-party scraper credentials (admin)."""
    from app.db import get_db
    db = get_db()
    provider = (req.provider or "apify").lower()
    if provider not in {"apify", "rapidapi", "custom_http"}:
        return {"status": "error", "message": "provider must be apify, rapidapi, or custom_http"}
    api_key = (req.api_key or "").strip()
    if not api_key:
        return {"status": "error", "message": "api_key is required"}
    cfg: dict = {
        "provider": provider,
        "endpoint": (req.endpoint or "").strip(),
        "actor_id": (req.actor_id or "").strip(),
        "host": (req.host or "").strip(),
    }
    await db.auth_element_set(
        user_id=_ADMIN_USER,
        service="scraper_config",
        config=cfg,
        secret_ref=api_key,
        label="default",
    )
    logger.info("Scraper config saved by admin (provider=%s)", provider)
    return {"status": "ok", "message": "Scraper configured."}


@router.delete("/scraper")
async def delete_scraper_config(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Remove the scraper credentials (admin)."""
    from app.db import get_db
    db = get_db()
    deleted = await db.auth_element_delete(_ADMIN_USER, "scraper_config", "default")
    logger.info("Scraper config removed by admin (deleted=%s)", deleted)
    return {"status": "ok", "deleted": deleted}


# ── Browser Session config (per-user) ──────────────────────────────────────

@router.get("/browser-session")
async def get_browser_session_status(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Report the caller's stored browser session (no cookies returned)."""
    user_id = resolve_user_id(authorization or "", token or "")
    if not user_id or user_id == ANONYMOUS_KEY:
        return {"status": "error", "message": "auth required"}
    sess = await get_browser_session_creds(user_id)
    if not sess:
        return {"configured": False}
    # Never echo cookies back — only summary metadata.
    cookie_count = 0
    try:
        from app.integrations.web_tools import _parse_cookies
        cookie_count = len(_parse_cookies(sess.get("cookies", "")))
    except Exception:
        pass
    return {
        "configured": True,
        "domain": sess.get("domain", ""),
        "user_agent": sess.get("user_agent", ""),
        "saved_at": sess.get("saved_at", ""),
        "cookie_count": cookie_count,
    }


@router.post("/browser-session")
async def save_browser_session(
    req: BrowserSessionRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Save the caller's browser cookies for authenticated scraping."""
    user_id = resolve_user_id(authorization or "", token or "")
    if not user_id or user_id == ANONYMOUS_KEY:
        return {"status": "error", "message": "auth required"}
    cookies = (req.cookies or "").strip()
    if not cookies:
        return {"status": "error", "message": "cookies are required"}
    from app.db import get_db
    db = get_db()
    cfg = {
        "domain": (req.domain or "").strip(),
        "user_agent": (req.user_agent or "").strip(),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.auth_element_set(
        user_id=user_id,
        service="browser_session",
        config=cfg,
        secret_ref=cookies,
        label="default",
    )
    logger.info("Browser session saved for user %s (domain=%s)", user_id, cfg["domain"])
    return {"status": "ok", "message": "Browser session saved."}


@router.delete("/browser-session")
async def delete_browser_session(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Remove the caller's stored browser session."""
    user_id = resolve_user_id(authorization or "", token or "")
    if not user_id or user_id == ANONYMOUS_KEY:
        return {"status": "error", "message": "auth required"}
    from app.db import get_db
    db = get_db()
    deleted = await db.auth_element_delete(user_id, "browser_session", "default")
    logger.info("Browser session removed for user %s (deleted=%s)", user_id, deleted)
    return {"status": "ok", "deleted": deleted}

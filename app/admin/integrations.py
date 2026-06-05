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
from urllib.parse import urlencode, urlparse, urlunparse

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request
from jose import jwt as jose_jwt
from pydantic import BaseModel

from app.auth.jwt import decode_token
from app.integrations.oauth_helper import oauth_label

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


# ── Ability-aware scope resolution (3-tier OAuth) ─────────────────────────
#
# Key under auth_elements for per-ability admin policy: one row per ability.
#   user_id  = "__admin__"
#   service  = f"oauth_ability:{ability_id}"        # e.g. "oauth_ability:google.gmail_read"
#   label    = "default"
#   config   = {"mode": "platform_only"|"byo_only"|"both"|"disabled",
#               "platform_scopes": [scope, ...],    # platform-OAuth scope subset
#               "max_byo_scopes":  [scope, ...]}    # cap on what BYO can request
# This namespace is distinct from `_ABILITY_CONFIG_KEY` (the legacy
# host-side abilities — codebase_admin, create_tools, automation) so the two
# concepts never collide.
OAUTH_ABILITY_SERVICE_PREFIX = "oauth_ability:"

_OAUTH_ABILITY_MODES = ("disabled", "platform_only", "byo_only", "both")


def _oauth_ability_service(ability_id: str) -> str:
    return f"{OAUTH_ABILITY_SERVICE_PREFIX}{ability_id}"


async def get_oauth_ability_config(ability_id: str) -> dict:
    """Return the admin policy for an ability, with sensible defaults.

    Defaults preserve current behaviour: every registered ability is available
    under platform OAuth with its full registry scope set. Admin can tighten
    later without touching agent rows."""
    from app.integrations.ability_registry import get_ability
    ab = get_ability(ability_id)
    default_scopes = list(ab.scopes) if ab else []
    cfg = {
        "mode": "platform_only",
        "platform_scopes": default_scopes,
        "max_byo_scopes": default_scopes,
    }
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, _oauth_ability_service(ability_id), "default")
        if elem and elem.get("config"):
            stored = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            if isinstance(stored, dict):
                cfg.update({k: v for k, v in stored.items() if k in cfg})
    except Exception:
        pass
    if cfg["mode"] not in _OAUTH_ABILITY_MODES:
        cfg["mode"] = "platform_only"
    return cfg


async def get_all_oauth_ability_configs() -> dict[str, dict]:
    """Fetch admin policies for every registered ability in a single DB query.

    Returns {ability_id: {mode, platform_scopes, max_byo_scopes}} — same shape
    as `get_oauth_ability_config` output, but batched so the agents UI doesn't
    run 58+ sequential queries when rendering the abilities tab.
    """
    from app.integrations.ability_registry import ABILITIES
    from app.db import get_db
    db = get_db()
    prefix = OAUTH_ABILITY_SERVICE_PREFIX

    # Pre-populate defaults for every ability in the registry
    configs: dict[str, dict] = {
        aid: {
            "mode": "platform_only",
            "platform_scopes": list(ab.scopes),
            "max_byo_scopes": list(ab.scopes),
        }
        for aid, ab in ABILITIES.items()
    }

    try:
        # One query — fetch all auth_elements for the admin user, then filter
        # by prefix so we only pay for the rows we care about.
        elements = await db.auth_element_list(_ADMIN_USER)
        for elem in elements:
            service = elem.get("service", "")
            if not service.startswith(prefix):
                continue
            ability_id = service[len(prefix):]
            cfg = configs.get(ability_id)
            if cfg is None:
                continue
            if elem.get("config"):
                stored = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
                if isinstance(stored, dict):
                    cfg.update({k: v for k, v in stored.items() if k in cfg})
            if cfg["mode"] not in _OAUTH_ABILITY_MODES:
                cfg["mode"] = "platform_only"
    except Exception:
        pass

    return configs


async def compute_required_scopes(
    agent_id: str,
    provider: str,
    *,
    source: str = "platform",
    legacy_fallback: Optional[list] = None,
) -> list[str]:
    """Resolve the OAuth scopes to request for an agent's authorize URL.

    Algorithm:
      1. Find the abilities this agent has enabled for `provider`.
      2. Union the scopes of each ability after intersecting with the
         app-admin policy for that ability:
           - `platform_only` mode: use `platform_scopes`
           - `byo_only` / `both`: use `max_byo_scopes` when source="byo",
             else `platform_scopes`
           - `disabled`: skipped entirely (treated as not enabled)
      3. Always include the provider's implicit abilities (profile / sign-in).

    When the agent has no enabled abilities (typical for legacy agents
    pre-migration), fall back to the legacy admin-configured scope list so
    every existing connection keeps working unchanged.
    """
    from app.integrations.ability_registry import (
        abilities_for_provider,
        implicit_abilities,
        get_ability,
    )
    p = (provider or "").lower()

    enabled_ability_ids: list[str] = []
    if agent_id:
        try:
            from app.db import get_db
            db = get_db()
            if hasattr(db, "get_agent_abilities"):
                rows = await db.get_agent_abilities(agent_id)
                enabled_ability_ids = [
                    r["ability_id"]
                    for r in rows
                    if r.get("enabled") and (r.get("source") or "platform") == source
                ]
        except Exception as e:
            logger.debug("get_agent_abilities(%s) failed: %s", agent_id, e)

    enabled_ability_ids = [
        aid for aid in enabled_ability_ids
        if (ab := get_ability(aid)) is not None and ab.provider == p
    ]

    if not enabled_ability_ids:
        # No per-agent ability rows yet — preserve the pre-existing behaviour
        # by handing back the admin's legacy scope list. Any new code path
        # populates ability rows so this branch only fires for unmigrated agents.
        if legacy_fallback is not None:
            return legacy_fallback
        return list({s for aid in abilities_for_provider(p) for s in (get_ability(aid).scopes if get_ability(aid) else ())})

    # Always include implicit abilities (sign-in) so we can identify the user.
    union_ids = list(dict.fromkeys(implicit_abilities(p) + enabled_ability_ids))
    scopes: list[str] = []
    for aid in union_ids:
        ab = get_ability(aid)
        if not ab:
            continue
        policy = await get_oauth_ability_config(aid)
        if policy["mode"] == "disabled" and not ab.implicit:
            continue
        cap = policy["max_byo_scopes"] if source == "byo" else policy["platform_scopes"]
        # Intersect ability's full scope set with the admin's cap.
        allowed = set(cap or [])
        for s in ab.scopes:
            if s in allowed and s not in scopes:
                scopes.append(s)
    return scopes


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


def _get_configured_base_url() -> Optional[str]:
    """Return the deployment-configured canonical base URL, or None if unset.

    Checked sources (in order): WEBHOOK_BASE_URL env var, then
    webhook_base_url.txt at the repo root. Returns None when neither is set,
    so callers can distinguish "operator picked a canonical host" from
    "fall back to request-derived host"."""
    from pathlib import Path
    base_url = os.environ.get("WEBHOOK_BASE_URL", "").strip()
    if not base_url:
        try:
            wh_file = Path(__file__).resolve().parent.parent.parent / "webhook_base_url.txt"
            if wh_file.exists():
                base_url = wh_file.read_text().strip()
        except Exception:
            pass
    return base_url or None


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
    base_url = _get_configured_base_url() or ""
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


# ── Per-provider stored redirect URI ──────────────────────────────────────

def per_agent_redirect_uri(provider: str, agent_id: str, request: Optional[Request] = None) -> str:
    """The stable per-agent OAuth landing URL.

    Agent admins running BYO OAuth register this URI in their own Google /
    Microsoft / etc. project so every per-agent flow lands here. The handler
    at this path does the full token exchange against the agent's BYO creds
    (Phase 3) — under platform OAuth (Phase 2) it 302s to the standard
    `/api/v1/oauth/callback/{provider}` endpoint.
    """
    base = _get_base_url(request).rstrip("/")
    return f"{base}/agents/{agent_id}/oauth/callback/{provider}"


async def _provider_redirect_uri(
    provider: str,
    service_key: str,
    request: Optional[Request],
    agent_id: str,
    source: str,
) -> str:
    """Shared resolver: per-agent URI for BYO, admin-stored or derived URI otherwise."""
    if source == "byo" and agent_id:
        return per_agent_redirect_uri(provider, agent_id, request)
    stored = await get_stored_oauth_redirect_uri(service_key)
    if stored:
        return stored
    return f"{_get_base_url(request).rstrip('/')}/api/v1/oauth/callback/{provider}"


# ── Cred resolver: platform vs BYO ─────────────────────────────────────────

_PROVIDER_PLATFORM_CRED_FN = {
    # Provider name → name of the get_*_creds function on this module.
    "google":    "get_google_creds",
    "microsoft": "get_microsoft_creds",
    "yahoo":     "get_yahoo_creds",
    "dropbox":   "get_dropbox_creds",
    "meta":      "get_meta_creds",
    "twitter":   "get_twitter_creds",
    "linkedin":  "get_linkedin_creds",
    "tiktok":    "get_tiktok_creds",
    "pinterest": "get_pinterest_creds",
    "reddit":    "get_reddit_creds",
    "snapchat":  "get_snapchat_creds",
    "twitch":    "get_twitch_creds",
    "ebay":      "get_ebay_creds",
    "etsy":      "get_etsy_creds",
    "shopify":   "get_shopify_creds",
    "amazon":    "get_amazon_creds",
}


async def resolve_oauth_creds(
    provider: str, agent_id: str = "", *, source: str = "platform",
) -> tuple[str, str]:
    """Return (client_id, client_secret) for an authorize / callback flow.

    For `source="platform"`, returns the app-admin's platform creds.
    For `source="byo"`, returns the agent admin's BYO creds (configured per
    agent in the abilities tab). Returns ("","") when BYO is selected but
    no creds are configured — callers should surface a setup error.
    """
    p = (provider or "").lower()
    if source == "byo" and agent_id:
        try:
            from app.db import get_db
            db = get_db()
            if hasattr(db, "get_agent_byo_creds"):
                cid, csec = await db.get_agent_byo_creds(agent_id, p)
                if cid and csec:
                    return (cid, csec)
        except Exception as e:
            logger.warning("BYO cred lookup failed for %s/%s: %s", p, agent_id, e)
        return ("", "")
    fn_name = _PROVIDER_PLATFORM_CRED_FN.get(p)
    if not fn_name:
        return ("", "")
    fn = globals().get(fn_name)
    if not fn:
        return ("", "")
    return await fn()


async def get_stored_oauth_redirect_uri(service_key: str) -> Optional[str]:
    """Return the admin-saved redirect URI for a provider, or None if unset.

    Each provider's stored OAuth config can carry an explicit `redirect_uri`
    chosen by the admin at setup time. That value is sent verbatim on the
    OAuth flow so the registered URI at the provider matches no matter which
    host the user originally arrived on. Returns None when the field isn't
    present so callers can fall back to a request-derived default."""
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(_ADMIN_USER, service_key, "default")
        if elem and elem.get("config"):
            config = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            uri = (config or {}).get("redirect_uri") or ""
            return uri.strip() or None
    except Exception as e:
        logger.debug("get_stored_oauth_redirect_uri(%s) failed: %s", service_key, e)
    return None


def _validate_oauth_redirect_uri(uri: str, expected_path: str) -> str:
    """Validate an admin-entered redirect URI and return the normalized form.

    Path is fixed (the FastAPI route the OAuth provider must hit); scheme is
    locked to https (with an http exemption for localhost dev). Host is the
    legitimate variable — only a presence check here; the UI shows a soft
    warning when it differs from the admin's current host. Raises
    HTTPException(400) on any failure so the admin sees the rejection in the
    setup form."""
    raw = (uri or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="redirect_uri must not be empty")
    try:
        parsed = urlparse(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="redirect_uri is not a valid URL")
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(status_code=400, detail="redirect_uri must include a host")
    is_loopback = host in ("localhost", "127.0.0.1", "::1")
    if scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="redirect_uri must use https")
    if scheme == "http" and not is_loopback:
        raise HTTPException(status_code=400, detail="redirect_uri must use https")
    path = parsed.path or ""
    if path.endswith("/") and path != "/":
        path = path.rstrip("/")
    if path != expected_path:
        raise HTTPException(
            status_code=400,
            detail=f"redirect_uri path must be {expected_path}",
        )
    if parsed.query:
        raise HTTPException(status_code=400, detail="redirect_uri must not include a query string")
    if parsed.fragment:
        raise HTTPException(status_code=400, detail="redirect_uri must not include a fragment")
    # Rebuild from validated components — lowercased scheme + host, original
    # port + userinfo if any, normalized path, no query/fragment.
    netloc = parsed.netloc
    # Lowercase only the host portion of netloc, preserving any port.
    if "@" in netloc or ":" in netloc.split("@")[-1]:
        # Has userinfo or explicit port — recompose carefully.
        userinfo = ""
        if "@" in netloc:
            userinfo, netloc = netloc.rsplit("@", 1)
            userinfo += "@"
        if ":" in netloc:
            h, port = netloc.rsplit(":", 1)
            netloc = f"{h.lower()}:{port}"
        else:
            netloc = netloc.lower()
        netloc = userinfo + netloc
    else:
        netloc = netloc.lower()
    return urlunparse((scheme, netloc, path, "", "", ""))


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


async def get_redirect_uri(
    request: Optional[Request] = None, *,
    agent_id: str = "", source: str = "platform",
) -> str:
    return await _provider_redirect_uri("google", "google_oauth_config", request, agent_id, source)


async def build_google_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    """Build the full Google OAuth authorization URL."""
    client_id, _ = await get_google_creds()
    redirect_uri = await get_redirect_uri(request, agent_id=agent_id, source=source)
    state = make_state_token(user_id, agent_id, provider="google", source=source)
    legacy = await _get_enabled_scopes("google_oauth_config", GOOGLE_SCOPES)
    scopes = await compute_required_scopes(
        agent_id, "google", source=source, legacy_fallback=legacy,
    )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        # `prompt=consent` forces Google to redisplay every sensitive-scope
        # checkbox so a user reconnecting can tick the ones they previously
        # skipped (e.g. `gmail.modify` required for Gmail push subscriptions).
        # `include_granted_scopes=true` makes the new grant *additive* over
        # any prior grant so we never accidentally lose a scope on re-consent.
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"https://accounts.google.com/o/oauth2/auth?{urlencode(params)}"


async def revoke_and_delete_google(user_id: str, agent_id: str) -> bool:
    """Revoke this (user, agent)'s Google token and delete from auth_elements."""
    from app.db import get_db
    db = get_db()
    label = oauth_label(agent_id)
    elem = await db.auth_element_get(user_id, "google", label)
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
    return await db.auth_element_delete(user_id, "google", label)


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


async def get_microsoft_redirect_uri(
    request: Optional[Request] = None, *,
    agent_id: str = "", source: str = "platform",
) -> str:
    return await _provider_redirect_uri("microsoft", "microsoft_oauth_config", request, agent_id, source)


async def build_microsoft_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    """Build the full Microsoft OAuth authorization URL (common / multi-tenant)."""
    client_id, _ = await get_microsoft_creds()
    redirect_uri = await get_microsoft_redirect_uri(request, agent_id=agent_id, source=source)
    state = make_state_token(user_id, agent_id, provider="microsoft", source=source)
    legacy = await _get_enabled_scopes("microsoft_oauth_config", MICROSOFT_SCOPES)
    scopes = await compute_required_scopes(
        agent_id, "microsoft", source=source, legacy_fallback=legacy,
    )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "response_mode": "query",
        # Force the consent screen so reconnects can pick up newly-added
        # scopes (e.g. Mail.ReadWrite for Outlook subscriptions). Microsoft
        # does NOT honour Google's `include_granted_scopes` flag — the v2
        # endpoint already merges scopes additively across sessions.
        "prompt": "consent",
        "state": state,
    }
    return f"https://login.microsoftonline.com/common/oauth2/v2.0/authorize?{urlencode(params)}"


async def revoke_and_delete_microsoft(user_id: str, agent_id: str) -> bool:
    """Delete this (user, agent)'s Microsoft tokens (no revoke endpoint)."""
    from app.db import get_db
    db = get_db()
    return await db.auth_element_delete(user_id, "microsoft", oauth_label(agent_id))


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


async def get_yahoo_redirect_uri(
    request: Optional[Request] = None, *,
    agent_id: str = "", source: str = "platform",
) -> str:
    return await _provider_redirect_uri("yahoo", "yahoo_oauth_config", request, agent_id, source)


async def build_yahoo_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    """Build the Yahoo OAuth authorization URL."""
    client_id, _ = await get_yahoo_creds()
    redirect_uri = await get_yahoo_redirect_uri(request, agent_id=agent_id, source=source)
    state = make_state_token(user_id, agent_id, provider="yahoo", source=source)
    legacy = await _get_enabled_scopes("yahoo_oauth_config", YAHOO_SCOPES)
    scopes = await compute_required_scopes(
        agent_id, "yahoo", source=source, legacy_fallback=legacy,
    )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
    }
    return f"https://api.login.yahoo.com/oauth2/request_auth?{urlencode(params)}"


async def revoke_and_delete_yahoo(user_id: str, agent_id: str) -> bool:
    """Delete this (user, agent)'s Yahoo tokens."""
    from app.db import get_db
    db = get_db()
    return await db.auth_element_delete(user_id, "yahoo", oauth_label(agent_id))


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


async def get_dropbox_redirect_uri(
    request: Optional[Request] = None, *,
    agent_id: str = "", source: str = "platform",
) -> str:
    return await _provider_redirect_uri("dropbox", "dropbox_oauth_config", request, agent_id, source)


async def build_dropbox_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    """Build the Dropbox OAuth authorization URL."""
    client_id, _ = await get_dropbox_creds()
    redirect_uri = await get_dropbox_redirect_uri(request, agent_id=agent_id, source=source)
    state = make_state_token(user_id, agent_id, provider="dropbox", source=source)
    legacy = await _get_enabled_scopes("dropbox_oauth_config", DROPBOX_SCOPES)
    scopes = await compute_required_scopes(
        agent_id, "dropbox", source=source, legacy_fallback=legacy,
    )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "token_access_type": "offline",
        "state": state,
    }
    return f"https://www.dropbox.com/oauth2/authorize?{urlencode(params)}"


async def revoke_and_delete_dropbox(user_id: str, agent_id: str) -> bool:
    """Revoke this (user, agent)'s Dropbox token and delete from auth_elements."""
    from app.db import get_db
    db = get_db()
    label = oauth_label(agent_id)
    elem = await db.auth_element_get(user_id, "dropbox", label)
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
    return await db.auth_element_delete(user_id, "dropbox", label)


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


async def get_meta_redirect_uri(
    request: Optional[Request] = None, *,
    agent_id: str = "", source: str = "platform",
) -> str:
    return await _provider_redirect_uri("meta", "meta_oauth_config", request, agent_id, source)


async def build_meta_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    client_id, _ = await get_meta_creds()
    redirect_uri = await get_meta_redirect_uri(request, agent_id=agent_id, source=source)
    state = make_state_token(user_id, agent_id, provider="meta", source=source)
    legacy = await _get_enabled_scopes("meta_oauth_config", META_SCOPES)
    scopes = await compute_required_scopes(
        agent_id, "meta", source=source, legacy_fallback=legacy,
    )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": ",".join(scopes),
        "state": state,
    }
    return f"https://www.facebook.com/v19.0/dialog/oauth?{urlencode(params)}"


async def revoke_and_delete_meta(user_id: str, agent_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    label = oauth_label(agent_id)
    # Meta: revoke via Graph API DELETE /{user_id}/permissions
    elem = await db.auth_element_get(user_id, "meta", label)
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
    await db.auth_element_delete(user_id, "meta", label)
    # Also clean up the per-platform aliases (same per-agent scope)
    await db.auth_element_delete(user_id, "facebook", label)
    await db.auth_element_delete(user_id, "instagram", label)
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


async def get_twitter_redirect_uri(
    request: Optional[Request] = None, *,
    agent_id: str = "", source: str = "platform",
) -> str:
    return await _provider_redirect_uri("twitter", "twitter_oauth_config", request, agent_id, source)


async def build_twitter_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> tuple[str, str]:
    """Returns (authorize_url, state_token). Twitter requires PKCE."""
    client_id, _ = await get_twitter_creds()
    redirect_uri = await get_twitter_redirect_uri(request, agent_id=agent_id, source=source)
    verifier, challenge = _pkce_pair()
    state = make_state_token(user_id, agent_id, provider="twitter", pkce_verifier=verifier, source=source)
    legacy = await _get_enabled_scopes("twitter_oauth_config", TWITTER_SCOPES)
    scopes = await compute_required_scopes(
        agent_id, "twitter", source=source, legacy_fallback=legacy,
    )
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


async def revoke_and_delete_twitter(user_id: str, agent_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    label = oauth_label(agent_id)
    elem = await db.auth_element_get(user_id, "twitter", label)
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
    return await db.auth_element_delete(user_id, "twitter", label)


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


async def get_linkedin_redirect_uri(
    request: Optional[Request] = None, *,
    agent_id: str = "", source: str = "platform",
) -> str:
    return await _provider_redirect_uri("linkedin", "linkedin_oauth_config", request, agent_id, source)


async def build_linkedin_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    client_id, _ = await get_linkedin_creds()
    redirect_uri = await get_linkedin_redirect_uri(request, agent_id=agent_id, source=source)
    state = make_state_token(user_id, agent_id, provider="linkedin", source=source)
    legacy = await _get_enabled_scopes("linkedin_oauth_config", LINKEDIN_SCOPES)
    scopes = await compute_required_scopes(
        agent_id, "linkedin", source=source, legacy_fallback=legacy,
    )
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
    }
    return f"https://www.linkedin.com/oauth/v2/authorization?{urlencode(params)}"


async def revoke_and_delete_linkedin(user_id: str, agent_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    return await db.auth_element_delete(user_id, "linkedin", oauth_label(agent_id))


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


async def get_tiktok_redirect_uri(
    request: Optional[Request] = None, *,
    agent_id: str = "", source: str = "platform",
) -> str:
    return await _provider_redirect_uri("tiktok", "tiktok_oauth_config", request, agent_id, source)


async def build_tiktok_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    client_id, _ = await get_tiktok_creds()
    redirect_uri = await get_tiktok_redirect_uri(request, agent_id=agent_id, source=source)
    verifier, challenge = _pkce_pair()
    state = make_state_token(user_id, agent_id, provider="tiktok", pkce_verifier=verifier, source=source)
    legacy = await _get_enabled_scopes("tiktok_oauth_config", TIKTOK_SCOPES)
    scopes = await compute_required_scopes(
        agent_id, "tiktok", source=source, legacy_fallback=legacy,
    )
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


async def revoke_and_delete_tiktok(user_id: str, agent_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    label = oauth_label(agent_id)
    elem = await db.auth_element_get(user_id, "tiktok", label)
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
    return await db.auth_element_delete(user_id, "tiktok", label)


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


async def get_pinterest_redirect_uri(
    request: Optional[Request] = None, *,
    agent_id: str = "", source: str = "platform",
) -> str:
    return await _provider_redirect_uri("pinterest", "pinterest_oauth_config", request, agent_id, source)


async def build_pinterest_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    client_id, _ = await get_pinterest_creds()
    redirect_uri = await get_pinterest_redirect_uri(request, agent_id=agent_id, source=source)
    state = make_state_token(user_id, agent_id, provider="pinterest", source=source)
    legacy = await _get_enabled_scopes("pinterest_oauth_config", PINTEREST_SCOPES)
    scopes = await compute_required_scopes(
        agent_id, "pinterest", source=source, legacy_fallback=legacy,
    )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": ",".join(scopes),
        "state": state,
    }
    return f"https://www.pinterest.com/oauth/?{urlencode(params)}"


async def revoke_and_delete_pinterest(user_id: str, agent_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    return await db.auth_element_delete(user_id, "pinterest", oauth_label(agent_id))


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


async def get_reddit_redirect_uri(
    request: Optional[Request] = None, *,
    agent_id: str = "", source: str = "platform",
) -> str:
    return await _provider_redirect_uri("reddit", "reddit_oauth_config", request, agent_id, source)


async def build_reddit_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    client_id, _ = await get_reddit_creds()
    redirect_uri = await get_reddit_redirect_uri(request, agent_id=agent_id, source=source)
    state = make_state_token(user_id, agent_id, provider="reddit", source=source)
    legacy = await _get_enabled_scopes("reddit_oauth_config", REDDIT_SCOPES)
    scopes = await compute_required_scopes(
        agent_id, "reddit", source=source, legacy_fallback=legacy,
    )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "duration": "permanent",
    }
    return f"https://www.reddit.com/api/v1/authorize?{urlencode(params)}"


async def revoke_and_delete_reddit(user_id: str, agent_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    label = oauth_label(agent_id)
    elem = await db.auth_element_get(user_id, "reddit", label)
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
    return await db.auth_element_delete(user_id, "reddit", label)


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


async def get_snapchat_redirect_uri(
    request: Optional[Request] = None, *,
    agent_id: str = "", source: str = "platform",
) -> str:
    return await _provider_redirect_uri("snapchat", "snapchat_oauth_config", request, agent_id, source)


async def build_snapchat_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    client_id, _ = await get_snapchat_creds()
    redirect_uri = await get_snapchat_redirect_uri(request, agent_id=agent_id, source=source)
    state = make_state_token(user_id, agent_id, provider="snapchat", source=source)
    legacy = await _get_enabled_scopes("snapchat_oauth_config", SNAPCHAT_SCOPES)
    scopes = await compute_required_scopes(
        agent_id, "snapchat", source=source, legacy_fallback=legacy,
    )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
    }
    return f"https://accounts.snapchat.com/login/oauth2/authorize?{urlencode(params)}"


async def revoke_and_delete_snapchat(user_id: str, agent_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    label = oauth_label(agent_id)
    elem = await db.auth_element_get(user_id, "snapchat", label)
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
    return await db.auth_element_delete(user_id, "snapchat", label)


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


async def get_twitch_redirect_uri(
    request: Optional[Request] = None, *,
    agent_id: str = "", source: str = "platform",
) -> str:
    return await _provider_redirect_uri("twitch", "twitch_oauth_config", request, agent_id, source)


async def build_twitch_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    client_id, _ = await get_twitch_creds()
    redirect_uri = await get_twitch_redirect_uri(request, agent_id=agent_id, source=source)
    state = make_state_token(user_id, agent_id, provider="twitch", source=source)
    legacy = await _get_enabled_scopes("twitch_oauth_config", TWITCH_SCOPES)
    scopes = await compute_required_scopes(
        agent_id, "twitch", source=source, legacy_fallback=legacy,
    )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
    }
    return f"https://id.twitch.tv/oauth2/authorize?{urlencode(params)}"


async def revoke_and_delete_twitch(user_id: str, agent_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    label = oauth_label(agent_id)
    elem = await db.auth_element_get(user_id, "twitch", label)
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
    return await db.auth_element_delete(user_id, "twitch", label)


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


async def get_ebay_redirect_uri(
    request: Optional[Request] = None, *,
    agent_id: str = "", source: str = "platform",
) -> str:
    return await _provider_redirect_uri("ebay", "ebay_oauth_config", request, agent_id, source)


async def build_ebay_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    client_id, _ = await get_ebay_creds()
    redirect_uri = await get_ebay_redirect_uri(request, agent_id=agent_id, source=source)
    state = make_state_token(user_id, agent_id, provider="ebay", source=source)
    legacy = await _get_enabled_scopes("ebay_oauth_config", EBAY_SCOPES)
    scopes = await compute_required_scopes(
        agent_id, "ebay", source=source, legacy_fallback=legacy,
    )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,  # eBay calls this the "RuName" but accepts the URI here for web flows
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
    }
    return f"https://auth.ebay.com/oauth2/authorize?{urlencode(params)}"


async def revoke_and_delete_ebay(user_id: str, agent_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    return await db.auth_element_delete(user_id, "ebay", oauth_label(agent_id))


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


async def get_etsy_redirect_uri(
    request: Optional[Request] = None, *,
    agent_id: str = "", source: str = "platform",
) -> str:
    return await _provider_redirect_uri("etsy", "etsy_oauth_config", request, agent_id, source)


async def build_etsy_authorize_url(
    user_id: str, agent_id: str = "", request: Optional[Request] = None,
    *, source: str = "platform",
) -> str:
    """Etsy uses OAuth 2.0 + PKCE. Stash the verifier in the state JWT."""
    client_id, _ = await get_etsy_creds()
    redirect_uri = await get_etsy_redirect_uri(request, agent_id=agent_id, source=source)
    verifier, challenge = _pkce_pair()
    state = make_state_token(user_id, agent_id, provider="etsy", pkce_verifier=verifier, source=source)
    legacy = await _get_enabled_scopes("etsy_oauth_config", ETSY_SCOPES)
    scopes = await compute_required_scopes(
        agent_id, "etsy", source=source, legacy_fallback=legacy,
    )
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


async def revoke_and_delete_etsy(user_id: str, agent_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    return await db.auth_element_delete(user_id, "etsy", oauth_label(agent_id))


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


async def get_shopify_redirect_uri(
    request: Optional[Request] = None, *,
    agent_id: str = "", source: str = "platform",
) -> str:
    return await _provider_redirect_uri("shopify", "shopify_oauth_config", request, agent_id, source)


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
    *, source: str = "platform",
) -> str:
    """Build the per-shop Shopify install URL. Caller must supply the shop domain."""
    if not shop:
        raise ValueError("shop domain required for Shopify authorize")
    shop_norm = _shopify_normalize_shop(shop)
    client_id, _ = await get_shopify_creds()
    redirect_uri = await get_shopify_redirect_uri(request, agent_id=agent_id, source=source)
    state = make_state_token(user_id, agent_id, provider="shopify", shop=shop_norm, source=source)
    legacy = await _get_enabled_scopes("shopify_oauth_config", SHOPIFY_SCOPES)
    scopes = await compute_required_scopes(
        agent_id, "shopify", source=source, legacy_fallback=legacy,
    )
    params = {
        "client_id": client_id,
        "scope": ",".join(scopes),
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"https://{shop_norm}/admin/oauth/authorize?{urlencode(params)}"


async def revoke_and_delete_shopify(user_id: str, agent_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    return await db.auth_element_delete(user_id, "shopify", oauth_label(agent_id))


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


async def get_amazon_redirect_uri(
    request: Optional[Request] = None, *,
    agent_id: str = "", source: str = "platform",
) -> str:
    return await _provider_redirect_uri("amazon", "amazon_oauth_config", request, agent_id, source)


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
    *, source: str = "platform",
) -> str:
    """Build the Amazon Seller Central authorization URL.

    Sellers complete the OAuth handshake on Seller Central (region-specific),
    then Amazon redirects back to our LWA callback with a spapi_oauth_code.
    """
    app_id = await _get_amazon_app_id()
    redirect_uri = await get_amazon_redirect_uri(request, agent_id=agent_id, source=source)
    state = make_state_token(user_id, agent_id, provider="amazon", region=region.upper(), source=source)
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


async def revoke_and_delete_amazon(user_id: str, agent_id: str) -> bool:
    from app.db import get_db
    db = get_db()
    return await db.auth_element_delete(user_id, "amazon", oauth_label(agent_id))


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
# admin uses to store OAuth credentials. `_ALWAYS_AVAILABLE` providers need
# no upfront setup — their configuration happens inside the agent's own
# Connections tab (e.g. telegram bot token) — so they are always surfaced.
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

# Communication channels — gated by an admin enable flag stored in
# auth_elements under `channel_{name}`. They have no upfront credentials at
# the platform level (the actual bot token / API key is supplied per-agent),
# so "configured" simply means the admin has enabled the channel.
_CHANNEL_CONFIG_KEY: dict[str, str] = {
    "telegram": "channel_telegram",
}

# Agent Tools — privileged host-side capabilities (codebase admin, tool
# creation). Same enable-flag mechanism as channels: an `auth_elements` row
# with `secret_ref="enabled"` under `ability_{name}` means the app admin has
# turned the ability on, after which agent admins can enable it per-agent.
_ABILITY_CONFIG_KEY: dict[str, str] = {
    "codebase_admin":   "ability_codebase_admin",
    # Git Control — structured version control (status/diff/commit/push/branch)
    # without raw shell access. Pure behavioral toggle (no platform credential —
    # uses the repo's git + the optional GitHub token), so it lives in
    # _ALWAYS_ON_ABILITIES (on by default; the per-agent switch is the real gate).
    "git_control":      "ability_git_control",
    # UI Admin — edit only front-end files under ui/ (.css/.html); never backend
    # or shell. Reuses app/admin/ui_guardrails.py. Also a pure behavioral toggle.
    "ui_admin":         "ability_ui_admin",
    "create_tools":     "ability_create_tools",
    "automation":       "ability_automation",
    "web_access":       "ability_web_access",
    "browser_control":  "ability_browser_control",
    "image_generation": "ability_image_generation",
    "visualizer":       "ability_visualizer",
    # Terminal control — lets an agent open + drive interactive terminal
    # programs (Claude Code, REPLs, any CLI). Effectively shell access, so it's
    # OFF by default (not in _ALWAYS_ON_ABILITIES) and must be turned on at the
    # app level then per-agent, exactly like codebase_admin.
    "terminal_control": "ability_terminal_control",
    # Pure behavioral toggles — no platform credential. They differ from the
    # rows above only in their *default*: they are available unless an admin
    # explicitly turns them off (see `_ALWAYS_ON_ABILITIES`), whereas the rows
    # above are off until enabled. Listed here so the admin Agent Tools page
    # can show + toggle them and the enable/disable endpoints accept them.
    "agent_orchestration": "ability_agent_orchestration",
    "diagnostics":         "ability_diagnostics",
    "agent_management":    "ability_agent_management",
    # App control — lets an agent rearrange the user's own screen (switch the
    # main view, show/hide & resize the chat panel). Pure behavioral toggle,
    # no credential, non-destructive → on by default at the app level.
    "app_control":         "ability_app_control",
    # Wiki control — lets an agent read/search the company-wide Wiki and
    # create/edit/delete its articles. Writes/deletes change shared data
    # everyone sees, so it's OFF by default (not in _ALWAYS_ON_ABILITIES) and
    # must be turned on at the app level then per-agent, like terminal_control.
    "wiki_control":        "ability_wiki_control",
}

# Abilities that are ON by default at the app level: available to the per-agent
# toggle unless an admin explicitly disables them. For these the persisted
# `auth_elements` row is inverted vs. the credentialed abilities — *absence of a
# row means enabled*, and an explicit `secret_ref="disabled"` row means the
# admin turned it off. This preserves the long-standing "always configured"
# behavior (no regression for agents already using them) while still letting an
# admin switch them off platform-wide from App Config → Agent Tools.
_ALWAYS_ON_ABILITIES: set[str] = {
    "agent_orchestration",
    "diagnostics",
    "agent_management",
    "git_control",
    "ui_admin",
    "app_control",
}


async def _ability_app_enabled(db, ability: str) -> bool:
    """True iff `ability` is enabled at the app (platform) level.

    - Credentialed abilities (codebase_admin, web_access, …): enabled only when
      an `auth_elements` row exists with a truthy `secret_ref`.
    - Always-on abilities (agent_orchestration, diagnostics, agent_management):
      enabled unless an explicit `secret_ref="disabled"` row exists.
    """
    service_key = _ABILITY_CONFIG_KEY.get(ability)
    if not service_key:
        return False
    try:
        elem = await db.auth_element_get(_ADMIN_USER, service_key, "default")
    except Exception as e:
        logger.debug("auth_element_get failed for ability %s: %s", service_key, e)
        elem = None
    if ability in _ALWAYS_ON_ABILITIES:
        return not (elem and elem.get("secret_ref") == "disabled")
    return bool(elem and elem.get("secret_ref"))


async def get_admin_configured_providers(user_id: Optional[str] = None) -> set[str]:
    """Return the set of connection_types that are set up and toggleable.

    A provider counts as configured when:
    - For channels (`telegram`, ...): the admin has enabled it in App Config
      → Agent Abilities (auth_elements row exists with secret_ref="enabled"), OR
    - Its admin `auth_elements` row exists with a non-empty `secret_ref`
      (the client secret / API key), OR
    - For `browser_session`: the given `user_id` has uploaded cookies. Without
      a `user_id` we cannot answer this — `browser_session` is omitted.
    """
    configured: set[str] = set()
    try:
        from app.db import get_db
        db = get_db()
    except Exception as e:
        logger.debug("Failed to import db while checking configured providers: %s", e)
        return configured

    # Channels: admin-enabled if their channel auth_element exists.
    for ct, service_key in _CHANNEL_CONFIG_KEY.items():
        try:
            elem = await db.auth_element_get(_ADMIN_USER, service_key, "default")
            if elem and elem.get("secret_ref"):
                configured.add(ct)
        except Exception as e:
            logger.debug("auth_element_get failed for channel %s: %s", service_key, e)

    # Agent Tools — credentialed abilities (codebase_admin, create_tools, …) use
    # the enable-flag pattern; always-on abilities (agent_orchestration,
    # diagnostics, agent_management) are configured unless explicitly disabled.
    # `_ability_app_enabled` encapsulates both cases.
    for ct in _ABILITY_CONFIG_KEY:
        if await _ability_app_enabled(db, ct):
            configured.add(ct)

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

    # browser_session is per-user — only surface when the active user has cookies.
    if user_id:
        try:
            elem = await db.auth_element_get(user_id, "browser_session", "default")
            if elem and elem.get("secret_ref"):
                configured.add("browser_session")
        except Exception as e:
            logger.debug("auth_element_get failed for browser_session (%s): %s", user_id, e)

    return configured


# ── Admin endpoints ──────────────────────────────────────────────────────

class OAuthConfigRequest(BaseModel):
    client_id: str
    client_secret: str
    scopes: Optional[list[str]] = None
    redirect_uri: Optional[str] = None


# Each provider's callback path under /api/v1/oauth/callback/... The save
# handlers validate that the admin-entered redirect_uri matches this path
# exactly (locking down everything but the host) before storing.
_PROVIDER_CALLBACK_PATH = {
    "google":    "/api/v1/oauth/callback/google",
    "microsoft": "/api/v1/oauth/callback/microsoft",
    "yahoo":     "/api/v1/oauth/callback/yahoo",
    "dropbox":   "/api/v1/oauth/callback/dropbox",
    "meta":      "/api/v1/oauth/callback/meta",
    "twitter":   "/api/v1/oauth/callback/twitter",
    "linkedin":  "/api/v1/oauth/callback/linkedin",
    "tiktok":    "/api/v1/oauth/callback/tiktok",
    "pinterest": "/api/v1/oauth/callback/pinterest",
    "reddit":    "/api/v1/oauth/callback/reddit",
    "snapchat":  "/api/v1/oauth/callback/snapchat",
    "twitch":    "/api/v1/oauth/callback/twitch",
    "ebay":      "/api/v1/oauth/callback/ebay",
    "etsy":      "/api/v1/oauth/callback/etsy",
    "shopify":   "/api/v1/oauth/callback/shopify",
    "amazon":    "/api/v1/oauth/callback/amazon",
}


def _apply_redirect_uri(cfg: dict, provider: str, req_redirect_uri: Optional[str]) -> None:
    """If the admin supplied a redirect_uri, validate & write it into cfg."""
    raw = (req_redirect_uri or "").strip()
    if not raw:
        return
    expected_path = _PROVIDER_CALLBACK_PATH.get(provider)
    if not expected_path:
        return
    cfg["redirect_uri"] = _validate_oauth_redirect_uri(raw, expected_path)


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
    channel_enabled           = await get_channel_enabled_map()
    ability_enabled           = await get_ability_enabled_map()

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

    # The configured-view URI (per provider) is whatever the admin saved at
    # setup time; if nothing is saved yet, the helper falls back to a
    # request-derived URI so the setup form has a sensible placeholder.
    suggested_base = _get_base_url(request).rstrip("/")
    def _suggest(provider: str) -> str:
        return f"{suggested_base}/api/v1/oauth/callback/{provider}"

    return {
        # Productivity
        "google_configured":    bool(google_cid and google_csec),
        "google_client_id":     _mask(google_cid),
        "google_scopes":        g_scopes,
        "redirect_uri":         await get_redirect_uri(request),
        "redirect_uri_suggested": _suggest("google"),
        "microsoft_configured": bool(ms_cid and ms_csec),
        "microsoft_client_id":  _mask(ms_cid),
        "microsoft_scopes":     ms_scopes,
        "microsoft_redirect_uri": await get_microsoft_redirect_uri(request),
        "microsoft_redirect_uri_suggested": _suggest("microsoft"),
        "yahoo_configured":     bool(yahoo_cid and yahoo_csec),
        "yahoo_client_id":      _mask(yahoo_cid),
        "yahoo_scopes":         yh_scopes,
        "yahoo_redirect_uri":   await get_yahoo_redirect_uri(request),
        "yahoo_redirect_uri_suggested": _suggest("yahoo"),
        "dropbox_configured":   bool(dbx_cid and dbx_csec),
        "dropbox_client_id":    _mask(dbx_cid),
        "dropbox_scopes":       dbx_scopes,
        "dropbox_redirect_uri": await get_dropbox_redirect_uri(request),
        "dropbox_redirect_uri_suggested": _suggest("dropbox"),
        # Social media
        "meta_configured":      bool(meta_cid and meta_csec),
        "meta_client_id":       _mask(meta_cid),
        "meta_scopes":          meta_scopes,
        "meta_redirect_uri":    await get_meta_redirect_uri(request),
        "meta_redirect_uri_suggested": _suggest("meta"),
        "twitter_configured":   bool(tw_cid and tw_csec),
        "twitter_client_id":    _mask(tw_cid),
        "twitter_scopes":       tw_scopes,
        "twitter_redirect_uri": await get_twitter_redirect_uri(request),
        "twitter_redirect_uri_suggested": _suggest("twitter"),
        "linkedin_configured":  bool(li_cid and li_csec),
        "linkedin_client_id":   _mask(li_cid),
        "linkedin_scopes":      li_scopes,
        "linkedin_redirect_uri": await get_linkedin_redirect_uri(request),
        "linkedin_redirect_uri_suggested": _suggest("linkedin"),
        "tiktok_configured":    bool(tt_cid and tt_csec),
        "tiktok_client_id":     _mask(tt_cid),
        "tiktok_scopes":        tt_scopes,
        "tiktok_redirect_uri":  await get_tiktok_redirect_uri(request),
        "tiktok_redirect_uri_suggested": _suggest("tiktok"),
        "pinterest_configured": bool(pin_cid and pin_csec),
        "pinterest_client_id":  _mask(pin_cid),
        "pinterest_scopes":     pin_scopes,
        "pinterest_redirect_uri": await get_pinterest_redirect_uri(request),
        "pinterest_redirect_uri_suggested": _suggest("pinterest"),
        "reddit_configured":    bool(red_cid and red_csec),
        "reddit_client_id":     _mask(red_cid),
        "reddit_scopes":        red_scopes,
        "reddit_redirect_uri":  await get_reddit_redirect_uri(request),
        "reddit_redirect_uri_suggested": _suggest("reddit"),
        "snapchat_configured":  bool(snap_cid and snap_csec),
        "snapchat_client_id":   _mask(snap_cid),
        "snapchat_scopes":      snap_scopes,
        "snapchat_redirect_uri": await get_snapchat_redirect_uri(request),
        "snapchat_redirect_uri_suggested": _suggest("snapchat"),
        "twitch_configured":    bool(twitch_cid and twitch_csec),
        "twitch_client_id":     _mask(twitch_cid),
        "twitch_scopes":        twit_scopes,
        "twitch_redirect_uri":  await get_twitch_redirect_uri(request),
        "twitch_redirect_uri_suggested": _suggest("twitch"),
        # Marketplaces
        "ebay_configured":      bool(ebay_cid and ebay_csec),
        "ebay_client_id":       _mask(ebay_cid),
        "ebay_scopes":          ebay_scopes,
        "ebay_redirect_uri":    await get_ebay_redirect_uri(request),
        "ebay_redirect_uri_suggested": _suggest("ebay"),
        "etsy_configured":      bool(etsy_cid and etsy_csec),
        "etsy_client_id":       _mask(etsy_cid),
        "etsy_scopes":          etsy_scopes,
        "etsy_redirect_uri":    await get_etsy_redirect_uri(request),
        "etsy_redirect_uri_suggested": _suggest("etsy"),
        "shopify_configured":   bool(shop_cid and shop_csec),
        "shopify_client_id":    _mask(shop_cid),
        "shopify_scopes":       shop_scopes,
        "shopify_redirect_uri": await get_shopify_redirect_uri(request),
        "shopify_redirect_uri_suggested": _suggest("shopify"),
        "amazon_configured":    bool(amz_cid and amz_csec),
        "amazon_client_id":     _mask(amz_cid),
        "amazon_scopes":        amz_scopes,
        "amazon_redirect_uri":  await get_amazon_redirect_uri(request),
        "amazon_redirect_uri_suggested": _suggest("amazon"),
        # Generic, non-OAuth providers (no client_id/secret pair)
        "scraper_configured":   bool(scraper_cfg),
        "scraper_provider":     (scraper_cfg or {}).get("provider", ""),
        "scraper_endpoint":     (scraper_cfg or {}).get("endpoint", ""),
        "scraper_actor_id":     (scraper_cfg or {}).get("extra", {}).get("actor_id", ""),
        "scraper_host":         (scraper_cfg or {}).get("extra", {}).get("host", ""),
        # Channels (admin enable/disable; per-agent creds live in Connections)
        "telegram_configured":       channel_enabled.get("telegram", False),
        # Agent Tools (host-side privileged capabilities)
        "codebase_admin_configured":   ability_enabled.get("codebase_admin", False),
        "git_control_configured":      ability_enabled.get("git_control", False),
        "ui_admin_configured":         ability_enabled.get("ui_admin", False),
        "terminal_control_configured": ability_enabled.get("terminal_control", False),
        "create_tools_configured":     ability_enabled.get("create_tools", False),
        "automation_configured":       ability_enabled.get("automation", False),
        "web_access_configured":       ability_enabled.get("web_access", False),
        "browser_control_configured":  ability_enabled.get("browser_control", False),
        "image_generation_configured": ability_enabled.get("image_generation", False),
        "visualizer_configured":       ability_enabled.get("visualizer", False),
        "agent_orchestration_configured": ability_enabled.get("agent_orchestration", False),
        "diagnostics_configured":         ability_enabled.get("diagnostics", False),
        "agent_management_configured":    ability_enabled.get("agent_management", False),
        "app_control_configured":         ability_enabled.get("app_control", False),
        "wiki_control_configured":         ability_enabled.get("wiki_control", False),
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
    _apply_redirect_uri(cfg, "google", req.redirect_uri)
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
    _apply_redirect_uri(cfg, "microsoft", req.redirect_uri)
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
    _apply_redirect_uri(cfg, "yahoo", req.redirect_uri)
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
    _apply_redirect_uri(cfg, "dropbox", req.redirect_uri)
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
        _apply_redirect_uri(cfg, provider, req.redirect_uri)
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


# ── Channels (admin enable/disable) ───────────────────────────────────────

async def get_channel_enabled_map() -> dict[str, bool]:
    """Return {channel_name: enabled} for every channel in `_CHANNEL_CONFIG_KEY`."""
    try:
        from app.db import get_db
        db = get_db()
    except Exception as e:
        logger.debug("Failed to import db while reading channel states: %s", e)
        return {ct: False for ct in _CHANNEL_CONFIG_KEY}
    out: dict[str, bool] = {}
    for ct, service_key in _CHANNEL_CONFIG_KEY.items():
        try:
            elem = await db.auth_element_get(_ADMIN_USER, service_key, "default")
            out[ct] = bool(elem and elem.get("secret_ref"))
        except Exception as e:
            logger.debug("auth_element_get failed for channel %s: %s", service_key, e)
            out[ct] = False
    return out


# One-time first-boot seed: turn every admin Agent Tool ON so the app ships
# ready to "do everything" out of the box. A marker `auth_elements` row records
# that the seed ran, so an admin who later DISABLES an ability is never silently
# re-enabled on the next restart.
_ABILITY_SEED_MARKER_KEY = "ability_defaults_seeded"
_ABILITY_SEED_VERSION = "v1"


async def seed_default_abilities() -> dict:
    """Enable all admin Agent Tools on first boot (idempotent, runs once).

    Mirrors `enable_ability` for each key in `_ABILITY_CONFIG_KEY`, then writes
    a version marker. If the marker already exists the seed is skipped, so admin
    disables persist across restarts.
    """
    try:
        from app.db import get_db
        db = get_db()
    except Exception as e:
        logger.debug("seed_default_abilities: db unavailable: %s", e)
        return {"seeded": False, "reason": "no-db"}

    try:
        marker = await db.auth_element_get(_ADMIN_USER, _ABILITY_SEED_MARKER_KEY, "default")
        if marker and marker.get("secret_ref"):
            return {"seeded": False, "reason": "already-seeded"}
    except Exception as e:
        logger.debug("seed_default_abilities: marker read failed (continuing): %s", e)

    enabled: list[str] = []
    for ability, service_key in _ABILITY_CONFIG_KEY.items():
        try:
            await db.auth_element_set(
                user_id=_ADMIN_USER,
                service=service_key,
                config={"enabled": True},
                secret_ref="enabled",
                label="default",
            )
            enabled.append(ability)
        except Exception as e:
            logger.warning("seed_default_abilities: failed to enable %s: %s", ability, e)

    try:
        await db.auth_element_set(
            user_id=_ADMIN_USER,
            service=_ABILITY_SEED_MARKER_KEY,
            config={"version": _ABILITY_SEED_VERSION},
            secret_ref=_ABILITY_SEED_VERSION,
            label="default",
        )
    except Exception as e:
        logger.warning("seed_default_abilities: failed to write seed marker: %s", e)

    logger.info("Seeded default admin abilities ON (enabled=%s)", enabled)
    return {"seeded": True, "enabled": enabled}


async def get_ability_enabled_map() -> dict[str, bool]:
    """Return {ability_name: enabled} for every entry in `_ABILITY_CONFIG_KEY`."""
    try:
        from app.db import get_db
        db = get_db()
    except Exception as e:
        logger.debug("Failed to import db while reading ability states: %s", e)
        return {ab: False for ab in _ABILITY_CONFIG_KEY}
    out: dict[str, bool] = {}
    for ab in _ABILITY_CONFIG_KEY:
        out[ab] = await _ability_app_enabled(db, ab)
    return out


async def is_ability_enabled_for_agent(agent_id: str, ability: str) -> bool:
    """True iff the ability is enabled both at app level AND for this specific agent."""
    service_key = _ABILITY_CONFIG_KEY.get(ability)
    if not service_key:
        return False
    try:
        from app.db import get_db
        db = get_db()
        if not await _ability_app_enabled(db, ability):
            return False
        rows = await db.get_agent_connections(agent_id)
        match = next(
            (r for r in rows
             if r.get("connection_type") == ability and r.get("section") == "ability"),
            None,
        )
        return bool(match and match.get("enabled"))
    except Exception as e:
        logger.debug("is_ability_enabled_for_agent(%s, %s) failed: %s", agent_id, ability, e)
        return False


# ── OAuth Ability admin endpoints ─────────────────────────────────────────

class OAuthAbilityPolicyRequest(BaseModel):
    mode: str                             # disabled | platform_only | byo_only | both
    platform_scopes: Optional[list[str]] = None
    max_byo_scopes:  Optional[list[str]] = None


@router.get("/oauth-abilities")
async def list_oauth_abilities(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Return every registered OAuth ability with its admin policy.

    Response shape mirrors the registry: one entry per ability with the
    current `mode`, configured `platform_scopes`, and `max_byo_scopes`.
    Agent admin UI uses this to know which abilities are toggleable per agent.
    """
    from app.integrations.ability_registry import ABILITIES
    out = []
    for ab in ABILITIES.values():
        policy = await get_oauth_ability_config(ab.id)
        out.append({
            "id": ab.id,
            "provider": ab.provider,
            "display_name": ab.display_name,
            "description": ab.description,
            "scopes": list(ab.scopes),
            "implicit": ab.implicit,
            "mode": policy["mode"],
            "platform_scopes": policy["platform_scopes"],
            "max_byo_scopes": policy["max_byo_scopes"],
        })
    return {"abilities": out}


@router.post("/oauth-abilities/{ability_id}")
async def set_oauth_ability_policy(
    ability_id: str,
    req: OAuthAbilityPolicyRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Set the admin policy for a single OAuth ability."""
    from app.integrations.ability_registry import get_ability
    ab = get_ability(ability_id)
    if not ab:
        raise HTTPException(status_code=404, detail=f"Unknown OAuth ability: {ability_id}")
    if req.mode not in _OAUTH_ABILITY_MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {_OAUTH_ABILITY_MODES}")
    # Caps must be a subset of the ability's registered scopes — admin can't
    # invent new scopes by editing this endpoint.
    allowed = set(ab.scopes)
    platform = [s for s in (req.platform_scopes or list(ab.scopes)) if s in allowed]
    max_byo = [s for s in (req.max_byo_scopes or list(ab.scopes)) if s in allowed]
    from app.db import get_db
    db = get_db()
    await db.auth_element_set(
        user_id=_ADMIN_USER,
        service=_oauth_ability_service(ability_id),
        config={"mode": req.mode, "platform_scopes": platform, "max_byo_scopes": max_byo},
        secret_ref="set",
        label="default",
    )
    logger.info("OAuth ability %s set to mode=%s", ability_id, req.mode)
    return {"status": "ok", "ability_id": ability_id, "mode": req.mode,
            "platform_scopes": platform, "max_byo_scopes": max_byo}


@router.delete("/oauth-abilities/{ability_id}")
async def reset_oauth_ability_policy(
    ability_id: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Delete the admin policy — reverts the ability to its registry defaults."""
    from app.db import get_db
    db = get_db()
    deleted = await db.auth_element_delete(_ADMIN_USER, _oauth_ability_service(ability_id), "default")
    return {"status": "ok", "ability_id": ability_id, "deleted": deleted}


@router.post("/abilities/{ability}")
async def enable_ability(
    ability: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Enable an agent ability (admin). Once enabled here, agent admins can
    turn it on per-agent in the Abilities tab."""
    if ability not in _ABILITY_CONFIG_KEY:
        return {"status": "error", "message": f"Unknown ability: {ability}"}
    from app.db import get_db
    db = get_db()
    if ability in _ALWAYS_ON_ABILITIES:
        # Inverted persistence: enabling means clearing any "disabled" marker so
        # the ability falls back to its always-available default.
        await db.auth_element_delete(_ADMIN_USER, _ABILITY_CONFIG_KEY[ability], "default")
    else:
        await db.auth_element_set(
            user_id=_ADMIN_USER,
            service=_ABILITY_CONFIG_KEY[ability],
            config={"enabled": True},
            secret_ref="enabled",
            label="default",
        )
    logger.info("Ability %s enabled by admin", ability)
    return {"status": "ok", "ability": ability, "enabled": True}


@router.delete("/abilities/{ability}")
async def disable_ability(
    ability: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Disable an agent ability (admin). Hides it from the agent Abilities
    tab. Existing per-agent `agent_connections` rows stay so they reactivate
    if the ability is re-enabled — except for `automation`, which purges all
    per-agent automation tasks, event subscriptions, and ability toggles."""
    if ability not in _ABILITY_CONFIG_KEY:
        return {"status": "error", "message": f"Unknown ability: {ability}"}
    from app.db import get_db
    db = get_db()
    if ability in _ALWAYS_ON_ABILITIES:
        # Inverted persistence: disabling writes an explicit "disabled" marker
        # (absence of a row would otherwise read as the always-on default).
        await db.auth_element_set(
            user_id=_ADMIN_USER,
            service=_ABILITY_CONFIG_KEY[ability],
            config={"enabled": False},
            secret_ref="disabled",
            label="default",
        )
        deleted = True
    else:
        deleted = await db.auth_element_delete(_ADMIN_USER, _ABILITY_CONFIG_KEY[ability], "default")
    if ability == "automation":
        # Wipe every agent's automation data so the feature truly resets.
        tasks_removed = await db.delete_all_automations()
        subs_removed = await db.delete_all_event_subscriptions()
        toggles_removed = await db.delete_all_ability_connections("automation")
        logger.info(
            "Automation ability disabled: purged %s tasks, %s subscriptions, %s per-agent toggles",
            tasks_removed, subs_removed, toggles_removed,
        )
    logger.info("Ability %s disabled by admin (deleted=%s)", ability, deleted)
    return {"status": "ok", "ability": ability, "enabled": False}


@router.post("/channels/{channel}")
async def enable_channel(
    channel: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Enable a communication channel (admin). Per-agent credentials still
    live in the agent's Connections tab — this just makes the channel
    available to agent admins and starts pollers for existing connections."""
    if channel not in _CHANNEL_CONFIG_KEY:
        return {"status": "error", "message": f"Unknown channel: {channel}"}
    from app.db import get_db
    db = get_db()
    await db.auth_element_set(
        user_id=_ADMIN_USER,
        service=_CHANNEL_CONFIG_KEY[channel],
        config={"enabled": True},
        secret_ref="enabled",
        label="default",
    )
    logger.info("Channel %s enabled by admin", channel)
    # Spin up pollers for any existing per-agent connections so the channel
    # works immediately without waiting for a restart.
    try:
        from app.communications.manager import get_plugin_manager
        pm = get_plugin_manager()
        if hasattr(pm, "reload_agent_connections"):
            import asyncio
            asyncio.create_task(pm.reload_agent_connections())
    except Exception as e:
        logger.debug("reload_agent_connections after enable failed: %s", e)
    return {"status": "ok", "channel": channel, "enabled": True}


@router.delete("/channels/{channel}")
async def disable_channel(
    channel: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Disable a communication channel (admin). Stops all active pollers for
    the channel and hides it from the agent page. Existing per-agent rows
    stay in the DB so they reactivate if the channel is re-enabled."""
    if channel not in _CHANNEL_CONFIG_KEY:
        return {"status": "error", "message": f"Unknown channel: {channel}"}
    from app.db import get_db
    db = get_db()
    deleted = await db.auth_element_delete(_ADMIN_USER, _CHANNEL_CONFIG_KEY[channel], "default")
    logger.info("Channel %s disabled by admin (deleted=%s)", channel, deleted)
    # Tear down any running pollers for this channel so messages stop arriving.
    stopped = 0
    try:
        from app.communications.manager import get_plugin_manager
        pm = get_plugin_manager()
        if hasattr(pm, "stop_channel_pollers"):
            stopped = await pm.stop_channel_pollers(channel)
    except Exception as e:
        logger.warning("stop_channel_pollers(%s) failed: %s", channel, e)
    return {"status": "ok", "channel": channel, "enabled": False, "pollers_stopped": stopped}

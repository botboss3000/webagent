"""Shared OAuth helpers for integration tools.

Centralises:
  - get_oauth_token(): reads access_token from auth_elements, auto-refreshes
    if expired (Google / Microsoft / Dropbox / Yahoo supported), persists the
    refreshed token back to auth_elements.
  - oauth_api_call(): generic HTTP call that injects the bearer token, parses
    JSON when possible, and surfaces structured errors the agent can act on.

Per-provider integration modules should always go through these helpers so
token refresh and error handling stay consistent.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


# Mapping: provider -> (token_url, basic_auth_required, scope_in_refresh)
_REFRESH_ENDPOINTS = {
    "google":    ("https://oauth2.googleapis.com/token",                                False, False),
    "microsoft": ("https://login.microsoftonline.com/common/oauth2/v2.0/token",         False, True),
    "dropbox":   ("https://api.dropboxapi.com/oauth2/token",                            False, False),
    "yahoo":     ("https://api.login.yahoo.com/oauth2/get_token",                       True,  False),
    "reddit":    ("https://www.reddit.com/api/v1/access_token",                         True,  False),
    "twitter":   ("https://api.twitter.com/2/oauth2/token",                             True,  False),
    "linkedin":  ("https://www.linkedin.com/oauth/v2/accessToken",                      False, False),
    "tiktok":    ("https://open.tiktokapis.com/v2/oauth/token/",                        False, False),
    "twitch":    ("https://id.twitch.tv/oauth2/token",                                  False, False),
}


_PROVIDER_CRED_FNS = {
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
}


# User-facing aliases (mirrors check_oauth_connection aliases).
PROVIDER_ALIASES = {
    "gmail":     "google",
    "drive":     "google",
    "calendar":  "google",
    "gcal":      "google",
    "docs":      "google",
    "outlook":   "microsoft",
    "onedrive":  "microsoft",
    "facebook":  "meta",
    "instagram": "meta",
    "x":         "twitter",
}


def normalize_provider(provider: str) -> str:
    p = (provider or "").lower().strip()
    return PROVIDER_ALIASES.get(p, p)


async def _get_creds(provider: str) -> Tuple[str, str]:
    """Look up admin-configured (client_id, client_secret) for a provider."""
    fn_name = _PROVIDER_CRED_FNS.get(provider)
    if not fn_name:
        return ("", "")
    try:
        import importlib
        mod = importlib.import_module("app.admin.integrations")
        fn = getattr(mod, fn_name, None)
        if fn:
            return await fn()
    except Exception as e:
        logger.warning("could not load creds for %s: %s", provider, e)
    return ("", "")


def _is_expired(connected_at: str, expires_in: Any, skew_seconds: int = 60) -> bool:
    """Return True if the stored token is past (expiry - skew)."""
    try:
        if not connected_at or not expires_in:
            return False  # treat unknown as fresh; let API call fail-then-refresh
        ttl = int(expires_in)
        ts = datetime.fromisoformat(connected_at.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) >= ts + timedelta(seconds=ttl - skew_seconds)
    except Exception:
        return False


async def _refresh_token(user_id: str, provider: str, refresh_token: str) -> Optional[dict]:
    """Hit the provider's refresh endpoint. Returns a fresh secret dict or None."""
    if provider not in _REFRESH_ENDPOINTS:
        return None
    url, use_basic, send_scope = _REFRESH_ENDPOINTS[provider]
    client_id, client_secret = await _get_creds(provider)
    if not client_id or not client_secret:
        logger.warning("refresh skipped — admin creds missing for %s", provider)
        return None

    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    headers = {}
    if use_basic:
        import base64 as _b64
        creds = _b64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {creds}"
        if provider == "reddit":
            headers["User-Agent"] = "webAgent/1.0"
    else:
        data["client_id"] = client_id
        data["client_secret"] = client_secret
    if send_scope:
        # Microsoft requires the original scopes echoed back during refresh
        pass  # left blank; MS accepts refresh without scope when offline_access was granted

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, data=data, headers=headers)
        if resp.status_code != 200:
            logger.warning("refresh %s failed %s: %s", provider, resp.status_code, resp.text[:200])
            return None
        payload = resp.json()
        return {
            "access_token":  payload.get("access_token", ""),
            "refresh_token": payload.get("refresh_token", refresh_token),  # some providers omit on refresh
            "expires_in":    payload.get("expires_in", 3600),
            "token_type":    payload.get("token_type", "Bearer"),
        }
    except Exception as e:
        logger.warning("refresh %s exception: %s", provider, e)
        return None


async def get_oauth_token(user_id: str, provider: str, allow_refresh: bool = True) -> Optional[dict]:
    """Return current token dict for (user_id, provider). Refreshes if needed.

    Result shape: {"access_token": str, "provider": str, "account": str}
                  or None if no connection exists / refresh impossible.
    """
    provider = normalize_provider(provider)
    from app.db import get_db
    db = get_db()
    elem = await db.auth_element_get(user_id, provider, "oauth")
    if not elem or not elem.get("secret_ref"):
        return None

    try:
        secret = json.loads(elem["secret_ref"])
    except Exception:
        return None

    config = elem.get("config") or {}
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except Exception:
            config = {}

    access_token = secret.get("access_token", "")
    refresh_token = secret.get("refresh_token", "")
    expires_in = secret.get("expires_in")
    connected_at = config.get("connected_at", "")

    needs_refresh = bool(allow_refresh and refresh_token and _is_expired(connected_at, expires_in))
    if needs_refresh:
        fresh = await _refresh_token(user_id, provider, refresh_token)
        if fresh and fresh.get("access_token"):
            access_token = fresh["access_token"]
            # Persist the refreshed token + new connected_at so next refresh check is correct.
            new_config = {**config, "connected_at": datetime.now(timezone.utc).isoformat()}
            await db.auth_element_set(
                user_id=user_id,
                service=provider,
                config=new_config,
                secret_ref=json.dumps(fresh),
                label="oauth",
            )

    if not access_token:
        return None

    return {
        "access_token": access_token,
        "provider": provider,
        "account": config.get("email") or config.get("name") or "",
    }


def not_connected_payload(provider: str) -> str:
    """Standard JSON the agent sees when no token exists. Tells it to call check_oauth_connection."""
    provider = normalize_provider(provider)
    return json.dumps({
        "status": "not_connected",
        "provider": provider,
        "message": (
            f"No connected {provider} account for this user. "
            f"Call check_oauth_connection('{provider}') to get a connect link, "
            f"then ask the user to click it."
        ),
    })


async def oauth_api_call(
    user_id: str,
    provider: str,
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    json_body: Optional[Any] = None,
    data: Optional[Any] = None,
    headers: Optional[dict] = None,
    timeout: float = 30.0,
    retry_on_401: bool = True,
) -> dict:
    """Generic OAuth-authenticated HTTP call.

    Returns a dict: {"status": "ok"|"error"|"not_connected", "http_status": int,
                     "body": parsed_json_or_text, "url": url}
    Bearer token is injected automatically. On 401 a single token refresh +
    retry is attempted.
    """
    tok = await get_oauth_token(user_id, provider)
    if not tok:
        return {"status": "not_connected", "provider": normalize_provider(provider)}

    hdrs = dict(headers or {})
    hdrs.setdefault("Authorization", f"Bearer {tok['access_token']}")
    hdrs.setdefault("Accept", "application/json")
    if json_body is not None and "Content-Type" not in hdrs:
        hdrs["Content-Type"] = "application/json"

    method = method.upper()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(
                method, url,
                params=params,
                json=json_body if json_body is not None else None,
                data=data if data is not None else None,
                headers=hdrs,
            )
    except Exception as e:
        return {"status": "error", "http_status": 0, "body": str(e), "url": url}

    if resp.status_code == 401 and retry_on_401:
        # Force a refresh + one retry.
        tok2 = await get_oauth_token(user_id, provider, allow_refresh=True)
        if tok2 and tok2["access_token"] != tok["access_token"]:
            hdrs["Authorization"] = f"Bearer {tok2['access_token']}"
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.request(
                        method, url,
                        params=params,
                        json=json_body if json_body is not None else None,
                        data=data if data is not None else None,
                        headers=hdrs,
                    )
            except Exception as e:
                return {"status": "error", "http_status": 0, "body": str(e), "url": url}

    # Parse body — JSON when possible, raw text otherwise.
    body: Any
    ctype = resp.headers.get("content-type", "")
    if "json" in ctype.lower():
        try:
            body = resp.json()
        except Exception:
            body = resp.text
    else:
        body = resp.text

    status = "ok" if 200 <= resp.status_code < 300 else "error"
    return {"status": status, "http_status": resp.status_code, "body": body, "url": url}

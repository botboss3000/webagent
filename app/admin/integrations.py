"""Integrations admin endpoints — OAuth credential config (admin) + shared helpers.

Supported providers:
  - Google  (Gmail, Drive, Docs, Calendar)
  - Microsoft  (Outlook, OneDrive, SharePoint)
  - Yahoo  (Yahoo Mail)
  - Dropbox  (file storage)
"""

import json
import logging
import os
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

def make_state_token(user_id: str, agent_id: str = "", provider: str = "google") -> str:
    """Create a signed JWT state token for OAuth CSRF protection."""
    payload = {
        "user_id": user_id,
        "agent_id": agent_id,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
        "purpose": f"{provider}_oauth",
    }
    return jose_jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)


def decode_state_token(state: str, provider: str = "google") -> Optional[dict]:
    """Decode state token. Returns {"user_id": ..., "agent_id": ...} or None."""
    try:
        payload = jose_jwt.decode(state, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
        if payload.get("purpose") != f"{provider}_oauth":
            return None
        return {"user_id": payload.get("user_id", ""), "agent_id": payload.get("agent_id", "")}
    except Exception:
        return None


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
    google_cid, google_csec = await get_google_creds()
    ms_cid, ms_csec = await get_microsoft_creds()
    yahoo_cid, yahoo_csec = await get_yahoo_creds()
    dbx_cid, dbx_csec = await get_dropbox_creds()

    def _mask(s: str) -> str:
        return s[:20] + "..." if len(s) > 20 else s

    return {
        # Google
        "google_configured": bool(google_cid and google_csec),
        "google_client_id": _mask(google_cid),
        "redirect_uri": get_redirect_uri(),
        # Microsoft
        "microsoft_configured": bool(ms_cid and ms_csec),
        "microsoft_client_id": _mask(ms_cid),
        "microsoft_redirect_uri": get_microsoft_redirect_uri(),
        # Yahoo
        "yahoo_configured": bool(yahoo_cid and yahoo_csec),
        "yahoo_client_id": _mask(yahoo_cid),
        "yahoo_redirect_uri": get_yahoo_redirect_uri(),
        # Dropbox
        "dropbox_configured": bool(dbx_cid and dbx_csec),
        "dropbox_client_id": _mask(dbx_cid),
        "dropbox_redirect_uri": get_dropbox_redirect_uri(),
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

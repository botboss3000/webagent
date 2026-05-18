"""Integrations admin endpoints — OAuth credential config (admin) + shared helpers."""

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

GOOGLE_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/calendar.readonly",
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
    from pathlib import Path
    base_url = os.environ.get("WEBHOOK_BASE_URL", "")
    if not base_url:
        try:
            wh_file = Path(__file__).resolve().parent.parent.parent / "webhook_base_url.txt"
            if wh_file.exists():
                base_url = wh_file.read_text().strip()
        except Exception:
            pass
    if not base_url:
        base_url = "http://localhost:8000"
    return f"{base_url.rstrip('/')}/api/v1/oauth/callback/google"


def make_state_token(user_id: str, agent_id: str = "") -> str:
    """Create a signed JWT state token for Google OAuth CSRF protection."""
    payload = {
        "user_id": user_id,
        "agent_id": agent_id,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
        "purpose": "google_oauth",
    }
    return jose_jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)


def decode_state_token(state: str) -> Optional[dict]:
    """Decode state token. Returns {"user_id": ..., "agent_id": ...} or None."""
    try:
        payload = jose_jwt.decode(state, _JWT_SECRET, algorithms=[_JWT_ALGORITHM])
        if payload.get("purpose") != "google_oauth":
            return None
        return {"user_id": payload.get("user_id", ""), "agent_id": payload.get("agent_id", "")}
    except Exception:
        return None


async def build_google_authorize_url(user_id: str, agent_id: str = "") -> str:
    """Build the full Google OAuth authorization URL."""
    client_id, _ = await get_google_creds()
    redirect_uri = get_redirect_uri()
    state = make_state_token(user_id, agent_id)
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


# ── Admin endpoints ──────────────────────────────────────────────────────

class GoogleOAuthConfigRequest(BaseModel):
    client_id: str
    client_secret: str


@router.get("")
async def get_integration_config(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Admin check: is Google OAuth configured? Returns config status + redirect URI."""
    client_id, client_secret = await get_google_creds()
    redirect_uri = get_redirect_uri()
    return {
        "google_configured": bool(client_id and client_secret),
        "google_client_id": client_id[:20] + "..." if len(client_id) > 20 else client_id,
        "redirect_uri": redirect_uri,
    }


@router.post("/google")
async def save_google_config(
    req: GoogleOAuthConfigRequest,
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

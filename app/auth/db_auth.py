"""FastAPI dependency for protecting DB viewer endpoints."""

import logging
from fastapi import Header, HTTPException, Query
from typing import Optional

from app.auth.jwt import decode_token

logger = logging.getLogger(__name__)


async def require_db_auth(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """FastAPI dependency: checks JWT for DB viewer endpoints.

    Accepts token via Authorization header (Bearer) or ?token= query param.
    Returns 401 if no valid token.
    """
    raw = ""
    if authorization and authorization.startswith("Bearer "):
        raw = authorization[7:]
    if not raw and token:
        raw = token

    if raw:
        payload = decode_token(raw)
        if payload is not None:
            return payload

    # Open access mode fallback: a tunnel client holds no mintable JWT
    # (open-login is local/LAN-only, never a public tunnel exit), so resolve a
    # tokenless OR stale-token caller to the bootstrap admin. This is the same
    # chokepoint the rest of the app uses (app.auth.identity.open_mode_admin_id)
    # so the DB viewer and every panel that depends on this gate — recordings,
    # diagnostics, feature catalog — work over a Cloudflare Tunnel. Admin status
    # is still confirmed downstream (db_viewer._get_caller looks up the
    # user_profiles table). Non-open modes keep strict enforcement.
    from app.auth.identity import open_mode_admin_id
    open_admin = open_mode_admin_id()
    if open_admin:
        return {"user_id": open_admin}

    raise HTTPException(status_code=401, detail="Authentication required for DB viewer")

"""Caller-identity verification helpers.

The auth middleware (`app/auth/middleware.py`) decodes the JWT on every
non-public request and stashes the subject's user_id on
`request.state.user_id`. It does NOT, however, enforce that the user_id
passed in the request payload (query param, body field, etc.) actually
matches that authenticated user — so without an extra check, an
authenticated user could simply submit a request claiming
`user_id=somebody_else` and the route would happily operate on that other
user's data.

This module is the single chokepoint for that check. Every API route that
reads or writes per-user state should call `assert_caller_is(request,
claimed_user_id)` before touching the DB. Global admins are allowed to
spoof user_ids (used by the admin tooling); everyone else must match.

WebSocket endpoints can't use this directly (no Request), but can call
`verify_token_matches_user(token, claimed_user_id)` on their handshake
payload to get the same guarantee.
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request

from app.auth.jwt import decode_token


async def _is_admin(user_id: str) -> bool:
    try:
        from app.db import get_db
        return await get_db().is_user_admin(user_id)
    except Exception:
        return False


async def assert_caller_is(request: Request, claimed_user_id: Optional[str]) -> str:
    """Confirm the JWT-authenticated caller matches `claimed_user_id`.

    Returns the verified user_id (the JWT subject) so callers can use it
    directly instead of the client-supplied value. Raises 401 if no token,
    403 if mismatch and caller is not a global admin.
    """
    state_uid = getattr(request.state, "user_id", None)
    if not state_uid:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not claimed_user_id:
        return state_uid
    if state_uid == claimed_user_id:
        return state_uid
    # Admins may operate on behalf of other users.
    if await _is_admin(state_uid):
        return claimed_user_id
    raise HTTPException(
        status_code=403,
        detail="user_id in request does not match authenticated caller",
    )


def verify_token_matches_user(token: str, claimed_user_id: str) -> Optional[str]:
    """For WebSocket handshakes: decode `token` and verify its subject
    equals `claimed_user_id`. Returns the verified user_id on success,
    `None` on failure (caller should close the socket)."""
    if not token or not claimed_user_id:
        return None
    payload = decode_token(token)
    if payload is None:
        return None
    sub = payload.get("user_id") or payload.get("sub")
    if sub == claimed_user_id:
        return sub
    return None

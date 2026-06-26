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

_BOOTSTRAP_ADMIN_ID = "admin"


def open_mode_admin_id() -> Optional[str]:
    """Return the bootstrap admin id when the app is in 'open' access mode,
    else None.

    The single chokepoint for the open-mode "trust the local single user as
    admin" shortcut. 'open' mode is the single-user / local-convenience mode,
    so there is no cross-tenant risk: a caller who presents no (mintable) JWT
    is resolved to the bootstrap admin. This is what lets the token-strict
    gates — this module's HTTP check plus the terminal / browser WebSocket
    handshakes — work over a Cloudflare Tunnel, where the frontend cannot mint
    a server-side JWT (open-login only issues to local + LAN devices, never a
    public tunnel exit). Admin *actions* are still gated on
    db.is_user_admin('admin') downstream, mirroring app/api/files.py and
    db_viewer. Returns None in every other access mode, so those modes keep
    their strict token enforcement unchanged.
    """
    try:
        from app.admin.settings import get_access_mode
        if get_access_mode() == "open":
            return _BOOTSTRAP_ADMIN_ID
    except Exception:
        pass
    return None


async def _is_admin(user_id: str) -> bool:
    try:
        from app.db import get_db
        return await get_db().is_user_admin(user_id)
    except Exception:
        return False


def request_user_id(request: Request) -> str:
    """Resolve the caller's user_id for an HTTP gate, the standard way.

    The single request-based chokepoint for admin/identity gates that read the
    token off the request themselves (AuthMiddleware is not registered globally,
    so request.state carries no identity here). Resolution order:

    1. JWT from the ``Authorization: Bearer`` header, then the ``?token=`` query
       param.
    2. Open-mode fallback: a tokenless caller in 'open' access mode resolves to
       the bootstrap admin (single-user / local convenience, no cross-tenant
       risk) so admin surfaces work over a Cloudflare Tunnel, where the browser
       cannot mint a server-side JWT. See open_mode_admin_id.

    Returns "" when neither yields an identity. Per-router helpers should call
    this instead of re-implementing the decode + open-mode dance (mirrors
    app/api/github.py and app/api/files.py).
    """
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else request.query_params.get("token", "")
    if token:
        payload = decode_token(token)
        if payload:
            uid = payload.get("user_id") or payload.get("sub")
            if uid:
                return uid
    return open_mode_admin_id() or ""


def _is_anonymous(user_id: Optional[str]) -> bool:
    """True for a caller with no identity or an ``anon_*`` guest id — i.e. NOT a
    registered, signed-in account. Mirrors the same check in app/wiki/store.py."""
    return (not user_id) or str(user_id).startswith("anon_")


async def user_may_access_page(user_id: Optional[str], kind: str, page_id: str) -> bool:
    """Server-side enforcement of a main/admin page's visibility for one caller.

    The single source of truth a page's sensitive endpoints call so the
    visibility the admin set is enforced on the SERVER, not merely hidden in the
    UI (the client can be bypassed). Rules, mirroring the 3-state visibility:
      • "all"  → everyone, including anonymous guests
      • "auth" → any signed-in registered user (or an admin)
      • "off"  → admins only
    Admins always pass. 'open' (single-user / local) access mode is full local
    trust, so everything passes there too — preserving the local convenience the
    old canvas gate had. Fails closed (treats as 'auth') if visibility can't be
    read."""
    # Open mode = single-user / local box = full trust; permit everything.
    if open_mode_admin_id() is not None:
        return True
    try:
        from app.ui_pages import effective_visibility
        vis = effective_visibility(kind, page_id)
    except Exception:
        vis = "auth"  # fail closed to registration-required
    if vis == "all":
        return True
    if await _is_admin(user_id or ""):
        return True
    if vis == "auth":
        return not _is_anonymous(user_id)
    return False  # "off" → admins only (already returned above)


async def caller_may_access_page(request: Request, kind: str, page_id: str) -> bool:
    """Request-based wrapper around user_may_access_page: resolves the caller from
    the request's token (open-mode tokenless → bootstrap admin) and applies the
    page-visibility gate. Use this in HTTP routes that must refuse a caller the
    admin's visibility setting excludes (e.g. the Canvas endpoints, which run
    agent-authored code with the caller's own app trust)."""
    return await user_may_access_page(request_user_id(request), kind, page_id)


async def resolve_admin_uid(claimed_uid: Optional[str]) -> Optional[str]:
    """Return an admin user_id the caller may act as, or None if not admin.

    The single chokepoint for the body/query-param admin gates (where the page
    passes its own ``requesting_user_id`` and the gate only checks it against the
    DB — no JWT). A claimed id that is a real DB admin is honored; otherwise the
    open-mode bootstrap admin is granted (tokenless tunnel convenience, same
    trust model as request_user_id). Returns None only when the caller is
    neither a DB admin nor in open mode, so non-open modes keep strict
    enforcement unchanged.
    """
    if claimed_uid and await _is_admin(claimed_uid):
        return claimed_uid
    return open_mode_admin_id()


async def assert_caller_is(request: Request, claimed_user_id: Optional[str]) -> str:
    """Confirm the JWT-authenticated caller matches `claimed_user_id`.

    Returns the verified user_id (the JWT subject) so callers can use it
    directly instead of the client-supplied value. Raises 401 if no token,
    403 if mismatch and caller is not a global admin.
    """
    state_uid = getattr(request.state, "user_id", None)
    if not state_uid:
        # AuthMiddleware is not registered globally (see app/auth/__init__.py
        # comment), so request.state.user_id is unset. Decode the JWT from
        # the Authorization header (or ?token=) ourselves as a fallback.
        auth_header = request.headers.get("Authorization", "")
        token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
        if not token:
            token = request.query_params.get("token", "")
        payload = decode_token(token) if token else None
        if payload:
            state_uid = payload.get("user_id") or payload.get("sub")
    if not state_uid:
        # Open access mode: resolve a tokenless caller to the bootstrap admin
        # (single-user / local convenience) so admin surfaces — e.g. the web
        # terminal's sidebar lists — work over a tunnel. See open_mode_admin_id.
        state_uid = open_mode_admin_id()
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
    `None` on failure (caller should close the socket).

    Exception: in 'open' access mode the claimed id is trusted even without a
    (valid) token — single-user / local convenience — so handshakes work over
    a tunnel where no JWT can be minted. See open_mode_admin_id."""
    if not claimed_user_id:
        return None
    payload = decode_token(token) if token else None
    if payload is not None:
        sub = payload.get("user_id") or payload.get("sub")
        if sub == claimed_user_id:
            return sub
    if open_mode_admin_id() is not None:
        return claimed_user_id
    return None

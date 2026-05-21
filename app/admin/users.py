"""
Admin user management endpoints.

POST   /admin/users/{user_id}/set-admin   — grant or revoke admin access
POST   /admin/users/{user_id}/approve     — approve a pending user
POST   /admin/users/{user_id}/revoke      — revoke approval (lock account)
DELETE /admin/users/{user_id}             — delete a user account
GET    /admin/users/{user_id}/profile     — fetch user profile (admin only)
GET    /admin/users                       — list all user profiles (admin only)
GET    /admin/users/stats                 — list users with full stats (admin only)
"""

import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.auth.users import (
    list_users,
    set_user_approval,
    delete_user as auth_delete_user,
    get_user_by_id,
)
from app.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


class SetAdminRequest(BaseModel):
    requesting_user_id: str   # must be an existing admin
    is_admin: bool


async def _require_admin(db, user_id: str) -> None:
    if not await db.is_user_admin(user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")


@router.post("/{user_id}/set-admin")
async def set_user_admin(user_id: str, req: SetAdminRequest):
    """
    Grant or revoke admin access for a user.
    The requesting_user_id must already be an admin.
    The very first admin can be created by setting BOOTSTRAP_ADMIN_ID in the
    environment, which bypasses the check for an empty admin set.
    """
    import os
    db = get_db()

    # Allow a bootstrap admin from env (first-time setup)
    bootstrap_id = os.environ.get("BOOTSTRAP_ADMIN_ID", "")
    if req.requesting_user_id != bootstrap_id:
        await _require_admin(db, req.requesting_user_id)

    profile = await db.set_user_admin(user_id, req.is_admin)
    logger.info(
        "Admin flag for user %s set to %s by %s",
        user_id, req.is_admin, req.requesting_user_id,
    )
    return {
        "user_id": user_id,
        "is_admin": bool(profile.get("is_admin")),
    }


@router.get("/{user_id}/profile")
async def get_user_profile(user_id: str, requesting_user_id: str = Query(...)):
    """Get any user's profile. Requires admin access."""
    db = get_db()
    await _require_admin(db, requesting_user_id)
    profile = await db.get_user_profile(user_id)
    if not profile:
        return {"user_id": user_id, "is_admin": False, "default_agent_id": None}
    return {
        "user_id": profile["user_id"],
        "is_admin": bool(profile.get("is_admin")),
        "default_agent_id": profile.get("default_agent_id"),
    }


@router.get("")
async def list_user_profiles(requesting_user_id: str = Query(...)):
    """List all user profiles. Requires admin access."""
    db = get_db()
    await _require_admin(db, requesting_user_id)
    conn = db._get_conn()
    try:
        rows = conn.execute(
            "SELECT user_id, is_admin, default_agent_id, created_at, updated_at FROM user_profiles ORDER BY created_at DESC"
        ).fetchall()
        return {
            "profiles": [
                {
                    "user_id": r["user_id"],
                    "is_admin": bool(r["is_admin"]),
                    "default_agent_id": r["default_agent_id"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        }
    finally:
        conn.close()


@router.get("/stats")
async def list_users_with_stats(requesting_user_id: str = Query(...)):
    """List registered users joined with profile + activity stats.
    Returns username, display_name, user_id, is_admin, is_approved,
    created_at, last_login_at, session_count, interaction_count.
    Admin only.
    """
    db = get_db()
    await _require_admin(db, requesting_user_id)

    # Profiles keyed by user_id
    conn = db._get_conn()
    profile_map: dict[str, dict] = {}
    session_counts: dict[str, int] = {}
    interaction_counts: dict[str, int] = {}
    try:
        for r in conn.execute(
            "SELECT user_id, is_admin, default_agent_id, created_at, updated_at, last_login_at FROM user_profiles"
        ).fetchall():
            profile_map[r["user_id"]] = dict(r)
        for r in conn.execute(
            "SELECT user_id, COUNT(*) AS n FROM sessions GROUP BY user_id"
        ).fetchall():
            session_counts[r["user_id"]] = r["n"]
        for r in conn.execute(
            "SELECT s.user_id AS user_id, COUNT(i.id) AS n "
            "FROM interactions i JOIN sessions s ON i.session_id = s.id "
            "GROUP BY s.user_id"
        ).fetchall():
            interaction_counts[r["user_id"]] = r["n"]
    finally:
        conn.close()

    # Registered users from users.json
    out = []
    for u in list_users():
        prof = profile_map.get(u.user_id, {})
        out.append({
            "username": u.username,
            "display_name": u.display_name,
            "user_id": u.user_id,
            "is_admin": bool(prof.get("is_admin", 0)) or (u.user_id == "admin_default"),
            "is_approved": bool(u.is_approved),
            "created_at": prof.get("created_at"),
            "last_login_at": prof.get("last_login_at"),
            "session_count": session_counts.get(u.user_id, 0),
            "interaction_count": interaction_counts.get(u.user_id, 0),
        })

    # Sort: pending approval first, then by last_login_at desc
    out.sort(key=lambda r: (
        1 if r["is_approved"] else 0,
        -(0 if r["last_login_at"] is None else 1),
        r["last_login_at"] or "",
    ), reverse=True)

    return {"users": out}


class _ApprovalRequest(BaseModel):
    requesting_user_id: str


@router.post("/{user_id}/approve")
async def approve_user(user_id: str, req: _ApprovalRequest):
    """Approve a pending account. Admin only."""
    db = get_db()
    await _require_admin(db, req.requesting_user_id)
    u = get_user_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="User not found")
    set_user_approval(u.username, True)
    return {"user_id": user_id, "is_approved": True}


@router.post("/{user_id}/revoke")
async def revoke_user(user_id: str, req: _ApprovalRequest):
    """Revoke approval (lock the account). Admin only."""
    db = get_db()
    await _require_admin(db, req.requesting_user_id)
    u = get_user_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="User not found")
    if u.username == "admin":
        raise HTTPException(status_code=400, detail="Cannot revoke the built-in admin account")
    set_user_approval(u.username, False)
    return {"user_id": user_id, "is_approved": False}


@router.delete("/{user_id}")
async def delete_user_endpoint(user_id: str, requesting_user_id: str = Query(...)):
    """Delete a user account. Admin only. Built-in admin cannot be deleted."""
    db = get_db()
    await _require_admin(db, requesting_user_id)
    u = get_user_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="User not found")
    if u.username == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete the built-in admin account")
    ok = auth_delete_user(u.username)
    if not ok:
        raise HTTPException(status_code=400, detail="Could not delete user")
    return {"user_id": user_id, "deleted": True}

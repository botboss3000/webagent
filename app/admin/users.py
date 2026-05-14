"""
Admin user management endpoints.

POST /admin/users/{user_id}/set-admin  — grant or revoke admin access
GET  /admin/users/{user_id}/profile    — fetch user profile (admin only)
GET  /admin/users                      — list all user profiles (admin only)
"""

import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

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

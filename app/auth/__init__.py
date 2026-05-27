"""Auth router — login with remember-me + recall endpoint."""

import logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.auth.jwt import create_access_token, decode_token
from app.auth.users import (
    authenticate,
    set_remember_token,
    resolve_remember_token,
    get_user,
    register_user,
    update_user,
    change_password,
    delete_user_self,
    UpdateConflict,
)
from app.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str
    remember_me: bool = False


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    user_id: str
    display_name: str
    remember_token: str = ""


class RegisterRequest(BaseModel):
    email: str
    password: str
    display_name: str = ""


class RegisterResponse(BaseModel):
    access_token: str = ""
    token_type: str = "bearer"
    username: str
    user_id: str
    display_name: str
    pending_approval: bool = False


class RecallRequest(BaseModel):
    remember_token: str


class RecallResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    user_id: str
    display_name: str


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    """Authenticate with username and password.

    If remember_me is True, a persistent remember token is generated
    (invalidates any previous one). Returns it alongside the JWT.
    """
    user = authenticate(req.email, req.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_approved:
        raise HTTPException(status_code=403, detail="Account pending admin approval")

    token = create_access_token(user.username, user.user_id)
    resp = LoginResponse(
        access_token=token,
        username=user.username,
        user_id=user.user_id,
        display_name=user.display_name,
    )

    if req.remember_me:
        remember = set_remember_token(req.email)
        if remember:
            resp.remember_token = remember

    # Track user login: create/update user_profiles row
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_db()
    await db.upsert_user_profile(user.user_id, last_login_at=now_iso)

    return resp


@router.post("/recall", response_model=RecallResponse)
async def recall(req: RecallRequest):
    """Exchange a valid remember token for a fresh JWT (auto-login).

    Called when user returns and the JWT has expired / doesn't exist yet.
    Returns a fresh access_token.
    """
    user = resolve_remember_token(req.remember_token)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired remember token")
    if not user.is_approved:
        raise HTTPException(status_code=403, detail="Account pending admin approval")

    token = create_access_token(user.username, user.user_id)
    resp = RecallResponse(
        access_token=token,
        username=user.username,
        user_id=user.user_id,
        display_name=user.display_name,
    )

    # Track auto-login from remember token
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_db()
    await db.upsert_user_profile(user.user_id, last_login_at=now_iso)

    return resp


@router.post("/register", response_model=RegisterResponse)
async def register(req: RegisterRequest):
    """Register a new user account.

    Username must be unique. On success, returns a JWT (auto-login).
    Honors the app's access_mode:
      - private:         registration blocked (403)
      - admin_approval:  account created but unapproved; login blocked until admin approves
      - public_*:        account created and auto-approved
    """
    from app.admin.settings import get_access_mode as _gam
    mode = _gam()
    if mode == "private":
        raise HTTPException(status_code=403, detail="Registration is disabled. This app is private.")

    auto_approve = (mode != "admin_approval")
    user = register_user(req.email, req.password, req.display_name, is_approved=auto_approve)
    if user is None:
        raise HTTPException(status_code=409, detail="Email already registered")

    if not auto_approve:
        # Created but unapproved — no token issued, client must wait for admin
        return RegisterResponse(
            access_token="",
            username=user.username,
            user_id=user.user_id,
            display_name=user.display_name,
            pending_approval=True,
        )

    token = create_access_token(user.username, user.user_id)
    resp = RegisterResponse(
        access_token=token,
        username=user.username,
        user_id=user.user_id,
        display_name=user.display_name,
        pending_approval=False,
    )

    # Track new user registration (first login)
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_db()
    await db.upsert_user_profile(user.user_id, last_login_at=now_iso)

    return resp


class AccessModeResponse(BaseModel):
    access_mode: str  # public_anonymous | public_registered | admin_approval | private
    # ── PRESENTATION-MODE START ── (delete this field to drop the demo flag from the API)
    presentation_mode: bool = False
    # ── PRESENTATION-MODE END ──


@router.get("/access-mode", response_model=AccessModeResponse)
async def access_mode():
    """Public endpoint: returns the current registration/access policy.
    Used by the sign-in modal and chat UI to gate registration and anonymous use.
    """
    from app.admin.settings import get_access_mode as _gam, _load_app_settings
    # ── PRESENTATION-MODE START ── (drop the kwarg below to remove from the API)
    return AccessModeResponse(
        access_mode=_gam(),
        presentation_mode=bool(_load_app_settings().get("presentation_mode", False)),
    )
    # ── PRESENTATION-MODE END ──


@router.post("/logout")
async def logout(req: LoginRequest | None = None):
    """Placeholder — actual token clearing is client-side (localStorage).
    The remember token stays valid until the user logs in again.
    No server-side action needed; the client clears its storage.
    """
    return {"status": "ok", "message": "Logged out. Clear client tokens."}


# ── Self-service profile management ─────────────────────────────────────────

class MeResponse(BaseModel):
    username: str
    user_id: str
    display_name: str
    is_approved: bool


class UpdateMeRequest(BaseModel):
    email: str | None = None
    display_name: str | None = None


class UpdateMeResponse(BaseModel):
    username: str
    user_id: str
    display_name: str
    is_approved: bool
    access_token: str  # fresh JWT (sub claim may have changed)
    token_type: str = "bearer"


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ChangePasswordResponse(BaseModel):
    status: str = "ok"
    remember_token: str = ""  # rotated; client should replace any cached copy


class DeleteMeRequest(BaseModel):
    password: str


def _require_auth(request: Request) -> tuple[str, str]:
    """Decode the Bearer JWT from the Authorization header.

    AuthMiddleware is not registered in main.py, so each route resolves the
    caller from the header directly. Returns (username, user_id).
    """
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if not token:
        token = request.query_params.get("token", "")
    payload = decode_token(token) if token else None
    if not payload:
        raise HTTPException(status_code=401, detail="Not authenticated")
    username = payload.get("sub") or ""
    user_id = payload.get("user_id") or ""
    if not username or not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return username, user_id


@router.get("/me", response_model=MeResponse)
async def get_me(request: Request):
    """Return the currently authenticated user's profile."""
    username, _user_id = _require_auth(request)
    user = get_user(username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return MeResponse(
        username=user.username,
        user_id=user.user_id,
        display_name=user.display_name,
        is_approved=user.is_approved,
    )


@router.patch("/me", response_model=UpdateMeResponse)
async def patch_me(request: Request, body: UpdateMeRequest):
    """Update the current user's email (username) and/or display name.

    Returns a fresh JWT because the `sub` claim may have changed.
    """
    current_username, _user_id = _require_auth(request)
    user = get_user(current_username)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    new_email = (body.email or "").strip() or None
    new_display = body.display_name if body.display_name is not None else None

    if new_email is not None and "@" not in new_email:
        raise HTTPException(status_code=400, detail="Invalid email address")

    try:
        updated = update_user(
            current_username,
            new_username=new_email,
            new_display_name=new_display,
        )
    except UpdateConflict:
        raise HTTPException(status_code=409, detail="Email already in use")

    if updated is None:
        raise HTTPException(status_code=404, detail="User not found")

    token = create_access_token(updated.username, updated.user_id)
    return UpdateMeResponse(
        username=updated.username,
        user_id=updated.user_id,
        display_name=updated.display_name,
        is_approved=updated.is_approved,
        access_token=token,
    )


@router.post("/change-password", response_model=ChangePasswordResponse)
async def post_change_password(request: Request, body: ChangePasswordRequest):
    """Change the authenticated user's password.

    Verifies the current password, writes a new bcrypt hash, and rotates the
    remember token so any previously-issued remember credential is invalidated.
    """
    username, _user_id = _require_auth(request)

    if not body.new_password or len(body.new_password) < 4:
        raise HTTPException(status_code=400, detail="New password must be at least 4 characters")

    verified = authenticate(username, body.current_password)
    if verified is None:
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    ok = change_password(username, body.new_password)
    if not ok:
        raise HTTPException(status_code=404, detail="User not found")

    # Rotate remember token so old persistent credentials can't auto-login.
    new_remember = set_remember_token(username) or ""
    return ChangePasswordResponse(status="ok", remember_token=new_remember)


@router.delete("/me")
async def delete_me(request: Request, body: DeleteMeRequest):
    """Delete the authenticated user's account.

    Requires password confirmation. The bootstrap `admin_default` user
    cannot be deleted (returns 403).
    """
    username, _user_id = _require_auth(request)

    verified = authenticate(username, body.password)
    if verified is None:
        raise HTTPException(status_code=401, detail="Password is incorrect")

    ok, reason = delete_user_self(username)
    if not ok:
        if reason == "protected":
            raise HTTPException(status_code=403, detail="The bootstrap admin account cannot be deleted")
        raise HTTPException(status_code=404, detail="User not found")

    return {"status": "ok"}

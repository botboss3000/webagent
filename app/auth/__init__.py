"""Auth router — login with remember-me + recall endpoint."""

import logging
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.auth.jwt import create_access_token
from app.auth.users import authenticate, set_remember_token, resolve_remember_token, get_user, register_user
from app.db import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
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
    username: str
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
    user = authenticate(req.username, req.password)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password")
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
        remember = set_remember_token(user.username)
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
    user = register_user(req.username, req.password, req.display_name, is_approved=auto_approve)
    if user is None:
        raise HTTPException(status_code=409, detail="Username already exists")

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


@router.get("/access-mode", response_model=AccessModeResponse)
async def access_mode():
    """Public endpoint: returns the current registration/access policy.
    Used by the sign-in modal and chat UI to gate registration and anonymous use.
    """
    from app.admin.settings import get_access_mode as _gam
    return AccessModeResponse(access_mode=_gam())


@router.post("/logout")
async def logout(req: LoginRequest | None = None):
    """Placeholder — actual token clearing is client-side (localStorage).
    The remember token stays valid until the user logs in again.
    No server-side action needed; the client clears its storage.
    """
    return {"status": "ok", "message": "Logged out. Clear client tokens."}

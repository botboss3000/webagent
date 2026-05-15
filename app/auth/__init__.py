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
    access_token: str
    token_type: str = "bearer"
    username: str
    user_id: str
    display_name: str


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
    """
    user = register_user(req.username, req.password, req.display_name)
    if user is None:
        raise HTTPException(status_code=409, detail="Username already exists")

    token = create_access_token(user.username, user.user_id)
    resp = RegisterResponse(
        access_token=token,
        username=user.username,
        user_id=user.user_id,
        display_name=user.display_name,
    )

    # Track new user registration (first login)
    now_iso = datetime.now(timezone.utc).isoformat()
    db = get_db()
    await db.upsert_user_profile(user.user_id, last_login_at=now_iso)

    return resp


@router.post("/logout")
async def logout(req: LoginRequest | None = None):
    """Placeholder — actual token clearing is client-side (localStorage).
    The remember token stays valid until the user logs in again.
    No server-side action needed; the client clears its storage.
    """
    return {"status": "ok", "message": "Logged out. Clear client tokens."}

"""Auth router — login with remember-me + recall endpoint."""

import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.auth.jwt import create_access_token
from app.auth.users import authenticate, set_remember_token, resolve_remember_token, get_user

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
    return RecallResponse(
        access_token=token,
        username=user.username,
        user_id=user.user_id,
        display_name=user.display_name,
    )


@router.post("/logout")
async def logout(req: LoginRequest | None = None):
    """Placeholder — actual token clearing is client-side (localStorage).
    The remember token stays valid until the user logs in again.
    No server-side action needed; the client clears its storage.
    """
    return {"status": "ok", "message": "Logged out. Clear client tokens."}

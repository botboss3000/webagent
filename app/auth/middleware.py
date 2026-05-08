"""Auth middleware — protects API routes and the main UI."""

import logging
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth.jwt import decode_token

logger = logging.getLogger(__name__)

# Paths that do NOT require authentication
PUBLIC_PATHS = {
    "/",
    "/login.html",
    "/ui/login.html",
    "/health",
    "/favicon.ico",
    "/favicon.svg",
    "/api/v1/auth/login",
    "/api/v1/auth/recall",
    "/api/v1/auth/logout",
    "/test",
}

# Static asset prefixes (CSS, JS, images — always public)
PUBLIC_PREFIXES = ("/ui/", "/uploads/", "/screenshots/")


class AuthMiddleware(BaseHTTPMiddleware):
    """Check JWT token on protected routes. Redirect to login.html if missing."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method

        # Allow OPTIONS preflight (CORS handles it below)
        if method == "OPTIONS":
            return await call_next(request)

        # Allow public paths
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)

        # Allow docs
        if path.startswith("/docs") or path.startswith("/openapi"):
            return await call_next(request)

        # Allow WebSocket endpoints (auth done via first message)
        if path.startswith("/api/v1/agent/ws") or path.startswith("/api/v1/terminal/ws"):
            return await call_next(request)

        # Check for Authorization header
        auth_header = request.headers.get("Authorization", "")
        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

        # Also check query param (for SSE / stream connections)
        if not token:
            token = request.query_params.get("token", "")

        if token:
            payload = decode_token(token)
            if payload is not None:
                # Attach user info to request state
                request.state.username = payload.get("sub")
                request.state.user_id = payload.get("user_id")
                return await call_next(request)

        # Not authenticated
        # API routes → 401 JSON
        if path.startswith("/api/"):
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Web pages → redirect to login
        return RedirectResponse(url="/login.html", status_code=307)

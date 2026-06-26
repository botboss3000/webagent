"""JWT token creation and validation."""

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt

logger = logging.getLogger(__name__)

# Use env var or fallback — in production set JWT_SECRET to a strong random key
_SECRET = os.environ.get("JWT_SECRET", "webagent-dev-secret-change-in-prod")
_ALGORITHM = "HS256"
_ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours


def create_access_token(username: str, user_id: str, expires_minutes: Optional[int] = None) -> str:
    """Create a signed JWT access token.

    ``expires_minutes`` overrides the default 24h lifetime when given — used by
    the browser-connector pairing flow, which mints a long-lived token the user
    pastes into the extension once (so it doesn't 401 a day later). The override
    only changes the expiry; the token is otherwise identical to a login token,
    scoped to the same user_id, so it carries no extra privilege.
    """
    minutes = _ACCESS_TOKEN_EXPIRE_MINUTES if expires_minutes is None else expires_minutes
    expire = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    payload = {
        "sub": username,
        "user_id": user_id,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, _SECRET, algorithm=_ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT token. Returns payload dict or None."""
    try:
        payload = jwt.decode(token, _SECRET, algorithms=[_ALGORITHM])
        return payload
    except JWTError as e:
        logger.debug("JWT decode failed: %s", e)
        return None

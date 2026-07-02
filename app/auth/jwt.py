"""JWT token creation and validation.

Signing secret (hardened 2026-06-28): the HS256 secret is NO LONGER a
hardcoded fallback. A guessable/committed secret lets anyone FORGE a token for
any user (including the bootstrap ``admin``), which defeats every downstream
auth gate. The secret is now resolved once at import in this order:

  1. ``JWT_SECRET`` env var, if set — explicit operator override (e.g. to share
     one secret across a multi-instance deployment).
  2. A persisted, git-ignored file ``data/config/jwt_secret.txt`` — created on
     first run and reused on every restart so existing tokens stay valid across
     restarts and across sibling processes (server, TUI, subprocess token mint).
  3. Freshly generated with ``secrets.token_urlsafe`` and written to that file.

If the file can't be written (read-only FS, perms), we fall back to an
in-memory random secret for this process — still unforgeable, but tokens won't
survive a restart (everyone re-logs in). That degraded mode is logged loudly.
There is intentionally NO hardcoded default to fall back to.
"""

import os
import logging
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from jose import JWTError, jwt

logger = logging.getLogger(__name__)

# Old committed default — kept ONLY to warn if an operator pins it via env.
_KNOWN_WEAK_SECRET = "webagent-dev-secret-change-in-prod"

# app/auth/jwt.py → app/auth → app → repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SECRET_FILE = _REPO_ROOT / "data" / "config" / "jwt_secret.txt"

_ALGORITHM = "HS256"
_ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 hours


def _read_secret_file() -> str:
    try:
        s = _SECRET_FILE.read_text(encoding="utf-8").strip()
        return s
    except (FileNotFoundError, NotADirectoryError):
        return ""
    except OSError as e:  # pragma: no cover - defensive
        logger.warning("Could not read JWT secret file %s: %s", _SECRET_FILE, e)
        return ""


def _resolve_secret() -> str:
    """Resolve the signing secret (see module docstring for the order)."""
    env = os.environ.get("JWT_SECRET")
    if env:
        if env == _KNOWN_WEAK_SECRET:
            logger.warning(
                "JWT_SECRET is set to the old hardcoded default — this is "
                "publicly known and forgeable. Set it to a strong random value "
                "or unset it to auto-generate one."
            )
        return env

    existing = _read_secret_file()
    if existing:
        return existing

    new = secrets.token_urlsafe(64)
    try:
        _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Atomic, exclusive create so two processes racing on first run don't
        # clobber each other — whoever loses reads the winner's value.
        fd = os.open(str(_SECRET_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(fd, new.encode("utf-8"))
        finally:
            os.close(fd)
        logger.info("Generated and persisted a new JWT signing secret.")
        return new
    except FileExistsError:
        # Another process created it between our read and our create — adopt theirs.
        adopted = _read_secret_file()
        return adopted or new
    except OSError as e:
        logger.warning(
            "Could not persist JWT secret to %s (%s). Using an in-memory secret "
            "for this process — tokens will NOT survive a restart (re-login "
            "required) and won't match sibling processes.",
            _SECRET_FILE, e,
        )
        return new


_SECRET = _resolve_secret()


def get_secret() -> str:
    """The active HS256 signing secret. Other modules that sign/verify their own
    tokens (e.g. OAuth state in app/admin/integrations.py) must use THIS so every
    token in the app is signed with the same key."""
    return _SECRET


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

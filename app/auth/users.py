"""Simple user store. Default admin user seeded on import.

Uses bcrypt directly (passlib has compat issues on Windows + Python 3.14).
Supports remember-me tokens for persistent sessions.
"""

import json
import logging
import secrets
from pathlib import Path
from typing import Optional

import bcrypt

logger = logging.getLogger(__name__)

_USER_FILE = Path(__file__).resolve().parent / "users.json"


# ── Data ────────────────────────────────────────────────────────────────────

class User:
    __slots__ = ("username", "password_hash", "user_id", "display_name", "remember_token", "is_approved")

    def __init__(self, username: str, password_hash: str, user_id: str,
                 display_name: str = "", remember_token: str = "",
                 is_approved: bool = True):
        self.username = username
        self.password_hash = password_hash
        self.user_id = user_id
        self.display_name = display_name or username
        self.remember_token = remember_token
        self.is_approved = is_approved

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "password_hash": self.password_hash,
            "user_id": self.user_id,
            "display_name": self.display_name,
            "remember_token": self.remember_token,
            "is_approved": self.is_approved,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "User":
        return cls(
            username=d["username"],
            password_hash=d["password_hash"],
            user_id=d["user_id"],
            display_name=d.get("display_name", d["username"]),
            remember_token=d.get("remember_token", ""),
            is_approved=d.get("is_approved", True),
        )


# ── Helpers ─────────────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    """Return bcrypt hash as string."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    """Check password against bcrypt hash."""
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"), password_hash.encode("utf-8")
        )
    except Exception:
        return False


def _generate_remember_token() -> str:
    """Generate a cryptographically secure 32-byte hex token."""
    return secrets.token_hex(32)


# ── Store ───────────────────────────────────────────────────────────────────

_users: dict[str, User] = {}  # username → User


def _ensure_default_admin():
    """Seed the default admin user if not already persisted."""
    if "admin" in _users:
        return
    admin = User(
        username="admin",
        password_hash=_hash_password("admin"),
        user_id="admin_default",
        display_name="Admin",
    )
    _users["admin"] = admin
    _persist()


def _load():
    if _USER_FILE.exists():
        try:
            data = json.loads(_USER_FILE.read_text(encoding="utf-8"))
            for d in data:
                u = User.from_dict(d)
                _users[u.username] = u
        except Exception as e:
            logger.warning("Failed to load users.json: %s", e)


def _persist():
    try:
        _USER_FILE.write_text(
            json.dumps([u.to_dict() for u in _users.values()], indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Failed to persist users.json: %s", e)


# Load existing users; seed admin if needed
_load()
_ensure_default_admin()


# ── Public API ──────────────────────────────────────────────────────────────

def authenticate(username: str, password: str) -> Optional[User]:
    """Verify credentials. Returns User on success, None on failure."""
    user = _users.get(username)
    if user is None:
        return None
    if not _verify_password(password, user.password_hash):
        return None
    return user


def get_user(username: str) -> Optional[User]:
    return _users.get(username)


def set_remember_token(username: str) -> Optional[str]:
    """Generate and store a new remember token for the user.
    Returns the token string, or None if user not found.
    Every call invalidates the previous token (single-session)."""
    user = _users.get(username)
    if user is None:
        return None
    user.remember_token = _generate_remember_token()
    _persist()
    return user.remember_token


def resolve_remember_token(token: str) -> Optional[User]:
    """Look up user by remember token. Returns User or None."""
    for user in _users.values():
        if user.remember_token and user.remember_token == token:
            return user
    return None


def register_user(username: str, password: str, display_name: str = "",
                  is_approved: bool = True) -> Optional[User]:
    """Register a new user. Returns User on success, None if username taken.
    Set is_approved=False when admin approval is required.
    """
    if username in _users:
        return None
    user = User(
        username=username,
        password_hash=_hash_password(password),
        user_id=username,
        display_name=display_name or username,
        is_approved=is_approved,
    )
    _users[username] = user
    _persist()
    return user


def list_users() -> list[User]:
    """Return all registered users."""
    return list(_users.values())


def set_user_approval(username: str, is_approved: bool) -> Optional[User]:
    """Set the is_approved flag on a user. Returns updated User or None."""
    user = _users.get(username)
    if user is None:
        return None
    user.is_approved = is_approved
    _persist()
    return user


def delete_user(username: str) -> bool:
    """Remove a user from the store. Returns True if deleted."""
    if username == "admin":
        return False
    if username not in _users:
        return False
    del _users[username]
    _persist()
    return True


def get_user_by_id(user_id: str) -> Optional[User]:
    """Look up a user by user_id (vs username)."""
    for u in _users.values():
        if u.user_id == user_id:
            return u
    return None


def clear_remember_token(username: str) -> bool:
    """Clear the remember token for a user (sign-out)."""
    user = _users.get(username)
    if user is None:
        return False
    user.remember_token = ""
    _persist()
    return True


# ── Self-service profile/password/delete ────────────────────────────────────

class UpdateConflict(Exception):
    """Raised when a username/email change collides with an existing user."""


def update_user(current_username: str,
                new_username: Optional[str] = None,
                new_display_name: Optional[str] = None) -> Optional[User]:
    """Update a user's username (email) and/or display name.

    Keeps user_id stable so existing DB references stay valid.
    Raises UpdateConflict if new_username collides with another user.
    Returns the updated User, or None if current_username not found.
    """
    user = _users.get(current_username)
    if user is None:
        return None

    if new_username is not None and new_username != current_username:
        if new_username in _users:
            raise UpdateConflict(new_username)
        # Rekey the dict entry
        del _users[current_username]
        user.username = new_username
        _users[new_username] = user

    if new_display_name is not None:
        user.display_name = new_display_name or user.username

    _persist()
    return user


def change_password(username: str, new_password: str) -> bool:
    """Set a new password hash for the user.

    Caller must verify the current password first (e.g. via authenticate()).
    Returns True on success, False if user not found.
    """
    user = _users.get(username)
    if user is None:
        return False
    user.password_hash = _hash_password(new_password)
    _persist()
    return True


def delete_user_self(username: str) -> tuple[bool, str]:
    """Delete the user identified by `username` (self-service).

    Blocks deletion of the bootstrap admin (user_id == "admin_default").
    Returns (ok, reason). reason is "" on success, otherwise a short code:
      - "not_found"  — username not in store
      - "protected"  — the admin_default seed cannot be deleted
    """
    user = _users.get(username)
    if user is None:
        return False, "not_found"
    if user.user_id == "admin_default":
        return False, "protected"
    del _users[username]
    _persist()
    return True, ""

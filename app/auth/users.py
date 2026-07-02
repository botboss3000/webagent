"""Simple user store. Admin account created via setup page or BOOTSTRAP_ADMIN_PASSWORD env var.

Uses bcrypt directly (passlib has compat issues on Windows + Python 3.14).
Supports remember-me tokens for persistent sessions."""

import json
import logging
import os as _os
import secrets
from pathlib import Path
from typing import Optional

import bcrypt

logger = logging.getLogger(__name__)

_USER_FILE = Path(__file__).resolve().parent / "users.json"

# How long a fresh login pass stays valid before it must renew, per user.
# This is the default for any account that hasn't picked its own value on the
# Manage Account page; each user can shorten or lengthen it (clamped to the
# bounds below). 30 days keeps people signed in across normal gaps; the silent
# auto-renew (when on) then keeps active sessions alive indefinitely.
DEFAULT_SESSION_LIFETIME_MINUTES = 60 * 24 * 30          # 30 days
MIN_SESSION_LIFETIME_MINUTES = 60                        # 1 hour floor
MAX_SESSION_LIFETIME_MINUTES = 60 * 24 * 90              # 90 day ceiling


def clamp_session_lifetime(minutes: int) -> int:
    """Pin a requested session lifetime into the allowed [1h, 90d] range."""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return DEFAULT_SESSION_LIFETIME_MINUTES
    return max(MIN_SESSION_LIFETIME_MINUTES, min(MAX_SESSION_LIFETIME_MINUTES, m))


# ── Data ────────────────────────────────────────────────────────────────────

class User:
    __slots__ = ("username", "password_hash", "user_id", "display_name",
                 "remember_token", "is_approved",
                 "session_lifetime_minutes", "auto_renew", "social_links")

    def __init__(self, username: str, password_hash: str, user_id: str,
                 display_name: str = "", remember_token: str = "",
                 is_approved: bool = True,
                 session_lifetime_minutes: int = DEFAULT_SESSION_LIFETIME_MINUTES,
                 auto_renew: bool = True,
                 social_links: Optional[dict] = None):
        self.username = username
        self.password_hash = password_hash
        self.user_id = user_id
        self.display_name = display_name or username
        self.remember_token = remember_token
        self.is_approved = is_approved
        # Linked social sign-in identities: {provider_id: external_id}. Lets the
        # same Google/GitHub/etc. account always resolve to this user even if the
        # provider's email later changes. See app/api/social_auth.py.
        self.social_links = dict(social_links or {})
        # Per-user session policy (set on the Manage Account page):
        #   session_lifetime_minutes — how long one login pass lasts
        #   auto_renew — whether the pass silently renews so the user effectively
        #                never gets signed out while they keep using the app
        self.session_lifetime_minutes = clamp_session_lifetime(session_lifetime_minutes)
        self.auto_renew = bool(auto_renew)

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "password_hash": self.password_hash,
            "user_id": self.user_id,
            "display_name": self.display_name,
            "remember_token": self.remember_token,
            "is_approved": self.is_approved,
            "session_lifetime_minutes": self.session_lifetime_minutes,
            "auto_renew": self.auto_renew,
            "social_links": self.social_links,
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
            # Older users.json files predate these keys — fall back to the
            # defaults so existing accounts load unchanged.
            session_lifetime_minutes=d.get("session_lifetime_minutes",
                                           DEFAULT_SESSION_LIFETIME_MINUTES),
            auto_renew=d.get("auto_renew", True),
            social_links=d.get("social_links", {}),
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


def _bootstrap_admin():
    """On first start, create admin from env var BOOTSTRAP_ADMIN_PASSWORD if set."""
    password = _os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    _have_admin = any(u.user_id == "admin" for u in _users.values())
    if password and not _have_admin:
        admin = User(
            username="admin",
            password_hash=_hash_password(password),
            user_id="admin",
            display_name="Admin",
        )
        _users["admin"] = admin
        _persist()
        logger.info("Bootstrap admin created from BOOTSTRAP_ADMIN_PASSWORD env var")
    elif not _have_admin:
        logger.info("No admin user found. App is in uninitialized state.")


def admin_exists() -> bool:
    """Return whether an admin user account exists.

    Keyed on the canonical bootstrap user_id ("admin"), NOT on the login name,
    so an admin who chose a custom login name is still recognised as set up."""
    return any(u.user_id == "admin" for u in _users.values())


def is_default_seed() -> bool:
    """Return whether the admin was seeded with a default password.
    Always False now — no default seed is used."""
    return False


# Load existing users; bootstrap admin from env var if set
_load()
_bootstrap_admin()


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
                  is_approved: bool = True,
                  user_id: Optional[str] = None) -> Optional[User]:
    """Register a new user. Returns User on success, None if username taken.
    Set is_approved=False when admin approval is required.

    `user_id` defaults to the username (the normal case — the login name *is*
    the stable id). The bootstrap admin passes user_id="admin" explicitly so the
    login name can be anything the operator chooses while the canonical admin id
    that the rest of the app keys privileges on stays fixed.
    """
    if username in _users:
        return None
    user = User(
        username=username,
        password_hash=_hash_password(password),
        user_id=user_id or username,
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
    user = _users.get(username)
    if user is None:
        return False
    # Protect the bootstrap admin regardless of its (possibly custom) login name.
    if user.user_id == "admin":
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


# ── Social sign-in links ────────────────────────────────────────────────────

def get_user_by_social(provider: str, external_id: str) -> Optional[User]:
    """Find the user who has linked this (provider, external_id), or None.

    Keyed on the provider's STABLE id (not email) so a user keeps their account
    even if they change their email at the provider.
    """
    if not provider or not external_id:
        return None
    ext = str(external_id)
    for u in _users.values():
        if str(u.social_links.get(provider, "")) == ext:
            return u
    return None


def link_social(username: str, provider: str, external_id: str) -> Optional[User]:
    """Record that `username`'s account is linked to (provider, external_id)."""
    user = _users.get(username)
    if user is None or not provider or not external_id:
        return None
    user.social_links[provider] = str(external_id)
    _persist()
    return user


def register_social_user(email: str, display_name: str, provider: str,
                         external_id: str, is_approved: bool = True) -> Optional[User]:
    """Create a new account for a first-time social sign-in and link it.

    The password is a random, unusable value — the person signs in through the
    provider, not with a password. Returns None if the email is already taken
    (the caller should link to the existing account instead).
    """
    if email in _users:
        return None
    user = User(
        username=email,
        password_hash=_hash_password(secrets.token_urlsafe(32)),
        user_id=email,
        display_name=display_name or email,
        is_approved=is_approved,
        social_links={provider: str(external_id)} if provider and external_id else None,
    )
    _users[email] = user
    _persist()
    return user


def clear_remember_token(username: str) -> bool:
    """Clear the remember token for a user (sign-out)."""
    user = _users.get(username)
    if user is None:
        return False
    user.remember_token = ""
    _persist()
    return True


def set_session_policy(username: str, lifetime_minutes: int,
                       auto_renew: bool) -> Optional[User]:
    """Set a user's session policy (login-pass lifetime + auto-renew).

    The lifetime is clamped into the allowed range. Returns the updated User,
    or None if the user is not found. Does NOT touch the remember token —
    callers decide whether to issue/clear it based on the new auto_renew value.
    """
    user = _users.get(username)
    if user is None:
        return None
    user.session_lifetime_minutes = clamp_session_lifetime(lifetime_minutes)
    user.auto_renew = bool(auto_renew)
    _persist()
    return user


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

    Blocks deletion of the bootstrap admin (user_id == "admin").
    Returns (ok, reason). reason is "" on success, otherwise a short code:
      - "not_found"  — username not in store
      - "protected"  — the admin seed cannot be deleted
    """
    user = _users.get(username)
    if user is None:
        return False, "not_found"
    if user.user_id == "admin":
        return False, "protected"
    del _users[username]
    _persist()
    return True, ""

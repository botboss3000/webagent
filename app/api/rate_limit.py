"""Tiny dependency-free, in-process rate limiter.

Purpose: put a floor under the abuse surface opened by anonymous/public chat
(the website embed widget). Without it, anyone can mint unlimited guest tokens
and spam `/chat/stream`, burning the agent owner's LLM budget (see the security
audit — this was the CRITICAL finding).

Design: fixed-window counters in a plain dict, keyed by an arbitrary string
(IP, anon user id, agent id, …). No external deps, no Redis. This is a
best-effort in-process guard — under multi-worker / multi-instance it limits
per process, not globally — so treat it as defence-in-depth ALONGSIDE an edge
limiter (Caddy/Cloudflare), not a replacement. Good enough to stop a trivial
single-origin flood, which is the common case.

Limits are env-overridable so an operator can tighten/loosen without a redeploy.
"""

from __future__ import annotations

import os
import time
from threading import Lock
from typing import Dict, Tuple

from fastapi import HTTPException, Request

# key -> (window_start_epoch, count)
_BUCKETS: Dict[str, Tuple[float, int]] = {}
_LOCK = Lock()
# Prune when the table grows past this many keys (windows are short, so stale
# entries are common under churn — keeps memory bounded without a background task).
_MAX_KEYS = 20000


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except Exception:
        return default


def client_ip(request: Request) -> str:
    """Best-effort client IP, honouring the edge proxy (Caddy/tunnel) that
    terminates TLS in front of the app. Falls back to the socket peer."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        # First hop is the original client; the rest are proxies.
        return xff.split(",")[0].strip()
    real = request.headers.get("x-real-ip", "")
    if real:
        return real.strip()
    return (request.client.host if request.client else "unknown")


def hit(key: str, limit: int, window_sec: int) -> bool:
    """Register one event for ``key``. Returns True if it is WITHIN the limit,
    False if the limit is now exceeded for the current window."""
    if limit <= 0:
        return True  # 0/negative disables the check
    now = time.monotonic()
    with _LOCK:
        if len(_BUCKETS) > _MAX_KEYS:
            cutoff = now - window_sec
            for k in [k for k, (ws, _) in _BUCKETS.items() if ws < cutoff]:
                _BUCKETS.pop(k, None)
        ws, count = _BUCKETS.get(key, (now, 0))
        if now - ws >= window_sec:
            ws, count = now, 0        # window elapsed → reset
        count += 1
        _BUCKETS[key] = (ws, count)
        return count <= limit


def enforce(key: str, limit: int, window_sec: int, detail: str = "Too many requests. Please slow down.") -> None:
    """Raise HTTP 429 when ``key`` exceeds ``limit`` events per ``window_sec``."""
    if not hit(key, limit, window_sec):
        raise HTTPException(status_code=429, detail=detail,
                            headers={"Retry-After": str(window_sec)})


# ── Public-chat abuse limits (env-overridable, app-settings.json-backed) ─────
def anon_session_limits() -> Tuple[int, int]:
    """(max new anonymous sessions, per window seconds) per client IP.
    Reads from app-settings.json via app.admin.settings, with env-var override."""
    try:
        from app.admin.settings import get_anon_session_max, get_anon_session_window
        return get_anon_session_max(), get_anon_session_window()
    except Exception:
        return _env_int("WEBAGENT_ANON_SESSION_MAX", 20), _env_int("WEBAGENT_ANON_SESSION_WINDOW", 60)


def anon_chat_limits() -> Tuple[int, int]:
    """(max messages, per window seconds) for one anonymous identity.
    Reads from app-settings.json via app.admin.settings, with env-var override."""
    try:
        from app.admin.settings import get_anon_chat_max, get_anon_chat_window
        return get_anon_chat_max(), get_anon_chat_window()
    except Exception:
        return _env_int("WEBAGENT_ANON_CHAT_MAX", 30), _env_int("WEBAGENT_ANON_CHAT_WINDOW", 300)


def anon_chat_ip_limits() -> Tuple[int, int]:
    """(max messages, per window seconds) across all anon chat from one IP —
    catches a single IP cycling through many minted tokens.
    Reads from app-settings.json via app.admin.settings, with env-var override."""
    try:
        from app.admin.settings import get_anon_chat_ip_max, get_anon_chat_ip_window
        return get_anon_chat_ip_max(), get_anon_chat_ip_window()
    except Exception:
        return _env_int("WEBAGENT_ANON_CHAT_IP_MAX", 90), _env_int("WEBAGENT_ANON_CHAT_IP_WINDOW", 300)


def enforce_anon_chat(user_id: str, request: Request) -> None:
    """Rate-limit ONE public (anonymous) chat turn — no-op for registered users.

    Anonymous identities are ``anon_*`` (app/communications/auth.py). Caps both
    per-identity and per-IP so a minted guest token can't be looped to burn the
    agent owner's LLM budget. Called from every send lane (/chat, /send, /stream)
    right after the caller id is verified. See the security audit CRITICAL find."""
    if not str(user_id or "").startswith("anon_"):
        return
    cmax, cwin = anon_chat_limits()
    enforce(f"anon-chat-uid:{user_id}", cmax, cwin,
            detail="You're sending messages too quickly. Please wait a moment.")
    ipmax, ipwin = anon_chat_ip_limits()
    enforce(f"anon-chat-ip:{client_ip(request)}", ipmax, ipwin,
            detail="Too many messages from your network. Please wait a moment.")

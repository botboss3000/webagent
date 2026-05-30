"""Persistence for Remote Access.

Two files, both runtime-only (gitignored):

* ``remote_access.json`` — this install's config: which method is active, each
  method's non-secret settings, the signpost settings, and this PC's own
  rendezvous key + push token.  No provider secrets live here — ngrok and
  cloudflared keep their own credentials in their own config (set up once by the
  user), so a leak of this file can't move money or impersonate the tunnel
  provider; it only reveals where this PC currently is.

* ``remote_access_pointers.json`` — the signpost directory when THIS install
  plays the "server" role (e.g. the always-on VM at webagent.live).  It is
  multi-tenant: one entry per rendezvous key, so it can host the "where is my
  PC right now" lookup for many users, not just the operator.  Each entry binds
  a secret push token on first write (trust-on-first-use), so only the PC that
  created the entry can later move it.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "remote_access.json"
POINTERS_FILE = PROJECT_ROOT / "remote_access_pointers.json"

_lock = threading.Lock()

DEFAULT_SIGNPOST_SERVER = "https://webagent.live"

# Methods the card offers. "managed" == the app can start/stop the tunnel.
METHODS = ("same_network", "ngrok", "cloudflare", "tailscale", "manual")

DEFAULT_CONFIG: Dict[str, Any] = {
    "active_method": "same_network",
    "auto_start": False,            # start the managed tunnel on server boot
    "rendezvous_key": "",           # public-ish; lives in the phone bookmark
    "push_token": "",               # secret; authorizes pointer updates
    "ngrok": {
        "bin_path": "",            # blank → look up "ngrok" on PATH
        "domain": "",              # reserved domain → stable address (paid)
        "region": "",
    },
    "cloudflare": {
        "bin_path": "",            # blank → "cloudflared" on PATH
        "tunnel": "",              # named tunnel (stable hostname)
        "hostname": "",            # the fixed https hostname you mapped
        "quick": False,            # True → ephemeral trycloudflare URL
    },
    "tailscale": {
        "bin_path": "",
    },
    "manual": {
        "public_url": "",          # the address your port-forward exposes
    },
    "signpost": {
        "enabled": True,
        "role": "client",          # client | server | both
        "server_url": DEFAULT_SIGNPOST_SERVER,
        "label": "",               # friendly name shown on the bookmark page
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> Dict[str, Any]:
    """Config with defaults filled in. Generates the key/token on first read."""
    data: Dict[str, Any] = {}
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
    except Exception:
        data = {}
    cfg = _deep_merge(DEFAULT_CONFIG, data)
    changed = False
    if not cfg.get("rendezvous_key"):
        cfg["rendezvous_key"] = secrets.token_urlsafe(12)
        changed = True
    if not cfg.get("push_token"):
        cfg["push_token"] = secrets.token_urlsafe(24)
        changed = True
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    with _lock:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
        except Exception:
            pass


def update_config(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-merge a patch into the saved config and return the result."""
    cfg = _deep_merge(load_config(), patch or {})
    save_config(cfg)
    return cfg


def regenerate_keys() -> Dict[str, str]:
    """Rotate this PC's rendezvous key + push token (invalidates old bookmark)."""
    cfg = load_config()
    cfg["rendezvous_key"] = secrets.token_urlsafe(12)
    cfg["push_token"] = secrets.token_urlsafe(24)
    save_config(cfg)
    return {"rendezvous_key": cfg["rendezvous_key"], "push_token": cfg["push_token"]}


# ── Signpost directory (server role) ─────────────────────────────────────────

def _load_pointers() -> Dict[str, Any]:
    try:
        if POINTERS_FILE.exists():
            with open(POINTERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}


def _save_pointers(data: Dict[str, Any]) -> None:
    try:
        with open(POINTERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def set_pointer(key: str, url: str, push_token: str, label: str = "") -> Dict[str, Any]:
    """Record/refresh where the PC behind ``key`` currently is.

    Trust-on-first-use: the first write for a key binds ``push_token``; later
    writes must present the same token or they're rejected. Returns a dict with
    ``ok`` (bool) and, on failure, ``error``.
    """
    if not key or not url:
        return {"ok": False, "error": "missing key or url"}
    with _lock:
        data = _load_pointers()
        existing = data.get(key)
        if existing and existing.get("push_token") and existing["push_token"] != push_token:
            return {"ok": False, "error": "push token mismatch"}
        data[key] = {
            "url": url,
            "label": label or (existing or {}).get("label", ""),
            "push_token": push_token or (existing or {}).get("push_token", ""),
            "updated_at": time.time(),
        }
        _save_pointers(data)
    return {"ok": True}


def get_pointer(key: str) -> Optional[Dict[str, Any]]:
    """Public lookup — returns url/label/updated_at only (never the token)."""
    if not key:
        return None
    entry = _load_pointers().get(key)
    if not entry:
        return None
    return {
        "url": entry.get("url", ""),
        "label": entry.get("label", ""),
        "updated_at": entry.get("updated_at", 0),
    }

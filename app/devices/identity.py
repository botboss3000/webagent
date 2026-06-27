"""Device identity — a STABLE, per-machine id for this webAgent instance.

When several instances share ONE database (the multi-device feature), each must
know which physical machine it is so jobs can be addressed to it. The existing
per-box id in app/db/logs_store.py lives *inside* the data/db/ folder — which is
exactly the folder a shared deployment would share — so every device would read
the same id and think it was the same box. This module deliberately stores the
device id OUTSIDE the shared data tree (the user's home dir, or an explicit env
override) so it stays unique per machine even when the database is shared.

Overrides (per machine): set WEBAGENT_DEVICE_ID / WEBAGENT_DEVICE_LABEL in the
environment to pin a device's id or friendly name.
"""
from __future__ import annotations

import os
import platform
import socket
import uuid
from pathlib import Path
from typing import Dict, Optional

_DEVICE_ID: Optional[str] = None
_DEVICE_LABEL: Optional[str] = None


def _id_file() -> Path:
    """Home dir, NOT under the project's data/ tree (which may be shared)."""
    return Path(os.path.expanduser("~")) / ".webagent" / "device_id"


def device_id() -> str:
    """A stable id unique to THIS machine. Resolution order: explicit env
    override → a persisted home-dir file (created once) → ephemeral fallback."""
    global _DEVICE_ID
    if _DEVICE_ID:
        return _DEVICE_ID
    env = (os.environ.get("WEBAGENT_DEVICE_ID") or "").strip()
    if env:
        _DEVICE_ID = env
        return env
    try:
        p = _id_file()
        if p.exists():
            val = p.read_text(encoding="utf-8").strip()
            if val:
                _DEVICE_ID = val
                return val
        p.parent.mkdir(parents=True, exist_ok=True)
        val = f"dev_{uuid.uuid4().hex[:16]}"
        p.write_text(val, encoding="utf-8")
        _DEVICE_ID = val
        return val
    except Exception:
        # Never crash over identity — fall back to an ephemeral id.
        _DEVICE_ID = _DEVICE_ID or f"dev_{uuid.uuid4().hex[:16]}"
        return _DEVICE_ID


def device_label() -> str:
    """A human-friendly name for this device (hostname, or an env override)."""
    global _DEVICE_LABEL
    if _DEVICE_LABEL:
        return _DEVICE_LABEL
    env = (os.environ.get("WEBAGENT_DEVICE_LABEL") or "").strip()
    if env:
        _DEVICE_LABEL = env
        return env
    try:
        _DEVICE_LABEL = socket.gethostname() or "device"
    except Exception:
        _DEVICE_LABEL = "device"
    return _DEVICE_LABEL


def capabilities() -> Dict[str, object]:
    """What this device is / where it runs. Kept small; enrich as the feature
    grows (e.g. has_browser, has_terminal, on_battery)."""
    try:
        sysname = platform.system()
    except Exception:
        sysname = ""
    return {"platform": sysname, "hostname": device_label()}

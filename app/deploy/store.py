"""Persistence for the Deploy panel (App Config → App Settings → Deploy).

ONE runtime-only file (gitignored): ``data/config/deploy.json``. It holds, per
deploy target (``google_vm`` / ``aws`` / …), the NON-secret settings the admin
filled in (project, zone, machine size, repo URL, domain, the "forget keys after
deploy" flag) plus a record of the LAST deployment that target produced (its
server name, public address, state, timestamps).

NO cloud secrets live here — the service-account JSON / cloud keys go into the
encrypted vault (``app/deploy/credentials.py``), exactly like every other secret
in the app. A leak of this file reveals where a server was created, never a key
that could spend money.

All writes route through ``app/util/config_io.py`` (atomic, self-creates
``data/config/``) per docs/claude/deployment.md.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

from app.util.config_io import read_json, safe_write_json

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "data" / "config" / "deploy.json"


def _load() -> Dict[str, Any]:
    data = read_json(CONFIG_FILE, {})
    return data if isinstance(data, dict) else {}


def _save(data: Dict[str, Any]) -> None:
    safe_write_json(CONFIG_FILE, data)


# ── Per-provider non-secret config ──────────────────────────────────────────

def get_config(provider_id: str) -> Dict[str, Any]:
    """The saved non-secret settings for one target (empty dict if none)."""
    block = _load().get("providers", {}).get(provider_id, {})
    cfg = block.get("config", {})
    return dict(cfg) if isinstance(cfg, dict) else {}


def save_config(provider_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Replace the saved settings for one target. Returns the stored config."""
    data = _load()
    providers = data.setdefault("providers", {})
    block = providers.setdefault(provider_id, {})
    block["config"] = dict(config or {})
    _save(data)
    return block["config"]


# ── Which target is currently selected in the panel ─────────────────────────

def get_active_provider() -> str:
    return str(_load().get("active_provider", "") or "")


def set_active_provider(provider_id: str) -> None:
    data = _load()
    data["active_provider"] = provider_id
    _save(data)


# ── Last-deployment record (what the status line + tear-down read) ───────────

def get_deployment(provider_id: str) -> Dict[str, Any]:
    block = _load().get("providers", {}).get(provider_id, {})
    rec = block.get("deployment", {})
    return dict(rec) if isinstance(rec, dict) else {}


def set_deployment(provider_id: str, record: Dict[str, Any]) -> None:
    """Stamp the result of a deploy (server name, IP/URL, state…)."""
    data = _load()
    providers = data.setdefault("providers", {})
    block = providers.setdefault(provider_id, {})
    rec = dict(record or {})
    rec.setdefault("updated_at", time.time())
    block["deployment"] = rec
    _save(data)


def clear_deployment(provider_id: str) -> None:
    data = _load()
    block = data.get("providers", {}).get(provider_id)
    if isinstance(block, dict) and "deployment" in block:
        block.pop("deployment", None)
        _save(data)

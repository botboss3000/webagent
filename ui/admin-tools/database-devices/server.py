"""Database & Devices — drop-in BACKEND for the admin page /admin/database-devices/*

This page is the app's shared backbone: the admin SIGNS IN to the database and
the secrets vault they want, and then sees every other DEVICE signed in to that
same database. Two concerns:

  • backend-status — the active database + secrets vault, each with a LIVE
    reachability probe, plus the "what's shared vs local to this device"
    classification. The CONNECT/TEST/ACTIVATE controls themselves reuse the
    existing /admin/storage/* endpoints (this page never re-implements them).
  • devices       — every computer running WebAgent against the shared database
    (the device-presence registry, app/devices/). Auto-discovered, online/
    offline, each self-reporting its platform + code repo. This is the "who else
    is on this database" view.

Discovered + mounted by the page catalog (app/ui_pages/__init__.py
discover_routers via this folder's page.json `router` field) — the API comes and
goes with the folder, NO edit to app/main.py. `.py` files under ui/ are never
served to the browser. All endpoints are admin-only via `resolve_admin_uid`
(honours open mode), mirroring app/api/deploy.py and the Server Manager page.
REMOVE-WHEN: the Database & Devices view is dropped from the admin page catalog.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/database-devices", tags=["admin-database-devices"])


async def _require_admin(uid: str) -> None:
    # Honours open mode via the shared chokepoint (mirrors app/api/deploy.py).
    from app.auth.identity import resolve_admin_uid
    if not await resolve_admin_uid(uid):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Admin required")


# ── Shared/local classification (mirrors the frontend + storage.js) ──────────
# Application-Data backends that put the data on a networked database more than
# one machine can point at (everything except the single-file local SQLite).
_REMOTE_DB = {"postgres", "supabase", "aws_rds", "gcp_cloud_sql", "azure_postgres", "neon"}
# How a vault choice travels across devices: cloud vaults are reachable from every
# machine (shared); the OS keyring is per-machine (local); the in-DB vault rides
# along inside the database (shared with it, but chicken-and-egg for the DB
# password); env is injected by the deployment.
_VAULT_REACH = {
    "hashicorp_vault": "shared", "azure_key_vault": "shared",
    "gcp_secret_manager": "shared", "aws_secrets_manager": "shared",
    "os_keyring": "local", "inline_db": "in_db", "env": "external",
}


def _db_target_summary(active: Dict[str, Any]) -> str:
    """A short human label for where the active database lives."""
    prov = (active.get("provider") or "sqlite").strip()
    if prov == "sqlite":
        return active.get("database") or "Local file (data/db/local.db)"
    if prov == "supabase":
        return active.get("supabase_url") or "Supabase project"
    host = active.get("host") or "?"
    port = active.get("port") or 5432
    dbname = active.get("database") or "?"
    user = active.get("username") or ""
    who = f"{user}@" if user else ""
    return f"{who}{host}:{port}/{dbname}"


def _vault_locator_summary(provider: str, configs: Dict[str, Any]) -> str:
    """A short human label for the active vault's address/project/region."""
    cfg = configs.get(provider) or {}
    if provider == "hashicorp_vault":
        return cfg.get("address") or "(address not set)"
    if provider == "azure_key_vault":
        return cfg.get("vault_url") or "(vault URL not set)"
    if provider == "gcp_secret_manager":
        return cfg.get("project") or "(project not set)"
    if provider == "aws_secrets_manager":
        return cfg.get("region") or "(region not set)"
    if provider == "os_keyring":
        return "This machine's credential store"
    if provider == "inline_db":
        return "Inside the app database"
    if provider == "env":
        return "Environment variables"
    return provider


async def _probe_db() -> Dict[str, Any]:
    """Cheap liveness probe of the ACTIVE database — one tiny row read. Read-only;
    never writes or switches anything. Mirrors get_db_stats()'s raw-client use so
    it works on every backend (SQLite / Postgres / Supabase)."""
    try:
        from app.db import get_db
        raw = get_db().get_raw_client()
        raw.table("sessions").select("id").limit(1).execute()
        return {"reachable": True, "detail": "Connected"}
    except Exception as e:  # unreachable / not bootstrapped / wrong creds
        return {"reachable": False, "detail": (str(e) or type(e).__name__)[:200]}


async def _probe_vault() -> Dict[str, Any]:
    """Liveness probe of the ACTIVE secrets vault via its test_connection()."""
    try:
        from app.secrets import get_secrets
        res = await get_secrets().test_connection()
        if isinstance(res, dict):
            return {"reachable": bool(res.get("ok", True)),
                    "detail": res.get("detail") or res.get("error") or "Reachable"}
        return {"reachable": bool(res), "detail": "Reachable" if res else "Unreachable"}
    except Exception as e:
        return {"reachable": False, "detail": (str(e) or type(e).__name__)[:200]}


@router.get("/backend-status")
async def backend_status(requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    # Reuse the storage page's own config builder so DB + vault facts come from a
    # single source of truth (provider, locators, restart flag) — we only add the
    # live probes + the shared/local classification on top.
    from app.admin.storage import get_storage_config
    cfg = await get_storage_config(requesting_user_id=requesting_user_id)
    db_active = cfg.get("db", {}).get("active", {}) or {}
    secrets = cfg.get("secrets", {}) or {}

    db_provider = (db_active.get("provider") or "sqlite").strip()
    db_shared = db_provider in _REMOTE_DB
    db_reach = await _probe_db()

    # The local SQLite copy ALWAYS probes green, so "reachable" alone hides a
    # silent fallback. Pull the DB layer's own health record: it knows whether the
    # backend that actually loaded is the shared DB the admin signed into, or this
    # device's local copy it dropped to. That mismatch is what we surface loudly.
    try:
        from app.db import get_connection_health
        health = get_connection_health()
    except Exception:
        health = {}
    degraded = bool(health.get("degraded")) and db_shared

    vault_provider = (secrets.get("provider") or "inline_db").strip()
    vault_reach = await _probe_vault()

    from app.devices import identity
    return {
        "device": {"id": identity.device_id(), "label": identity.device_label()},
        "env_locked": bool(cfg.get("env_locked")),
        "db": {
            "provider": db_provider,
            "target": _db_target_summary(db_active),
            "shared": db_shared,
            "reachable": db_reach["reachable"],
            "detail": db_reach["detail"],
            # Fallback-aware fields (see get_connection_health):
            "degraded": degraded,
            "active_backend": health.get("actual") or ("local" if not db_shared else None),
            "fallback_reason": health.get("reason") if degraded else None,
            "fallback_message": health.get("message") if degraded else None,
            "fallback_detail": health.get("detail") if degraded else None,
        },
        "vault": {
            "provider": vault_provider,
            "locator": _vault_locator_summary(vault_provider, secrets.get("configs", {}) or {}),
            "reach": _VAULT_REACH.get(vault_provider, "local"),
            "reachable": vault_reach["reachable"],
            "detail": vault_reach["detail"],
            "restart_recommended": bool(secrets.get("restart_recommended")),
            "boot_provider": secrets.get("boot_provider"),
        },
    }


# ── Linked devices — every machine signed in to the shared database ──────────
@router.get("/devices")
async def devices(requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    from app.devices import dispatch, identity

    me = identity.device_id()
    rows = await dispatch.list_devices(online_within_seconds=60)

    def _caps(raw):
        if isinstance(raw, str):
            try:
                return json.loads(raw or "{}")
            except Exception:
                return {}
        return raw or {}

    # Live facts for THIS machine, used to fill the self row even before its first
    # heartbeat lands (or if an older build wrote it without the repo).
    my_caps = identity.capabilities()

    out: List[Dict[str, Any]] = []
    seen_self = False
    for d in rows:
        iid = d.get("instance_id")
        is_self = iid == me
        if is_self:
            seen_self = True
        caps = _caps(d.get("capabilities"))
        out.append({
            "instance_id": iid,
            "label": d.get("label") or iid,
            "online": bool(d.get("online")),
            "last_seen": d.get("last_seen"),
            "is_self": is_self,
            "platform": caps.get("platform") or (my_caps.get("platform", "") if is_self else ""),
            "endpoint": caps.get("endpoint") or d.get("endpoint") or "",
            "repo": caps.get("repo") or (my_caps.get("repo", "") if is_self else ""),
            "branch": caps.get("branch") or (my_caps.get("branch", "") if is_self else ""),
        })
    # Surface THIS device even before its first heartbeat (mirrors app/api/devices.py).
    if not seen_self:
        caps = identity.capabilities()
        out.insert(0, {
            "instance_id": me,
            "label": identity.device_label(),
            "online": True,
            "last_seen": None,
            "is_self": True,
            "platform": caps.get("platform", ""),
            "endpoint": caps.get("endpoint", ""),
            "repo": caps.get("repo", ""),
            "branch": caps.get("branch", ""),
        })
    return {"devices": out, "self": me}

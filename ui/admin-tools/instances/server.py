"""Instances — drop-in BACKEND for the admin Instances page: /admin/instances/*

This ONE router powers the whole Instances page (ui/admin-tools/instances/), which
replaced two older pages (Database & Devices + Server Manager). It has three
concerns, all admin-only:

  • Connection health / config for THIS device — the active database + secrets
    vault, each with a LIVE reachability probe, plus the shared/local
    classification and the silent-fallback flag. The connect / test / activate
    CONTROLS themselves reuse the existing /admin/storage/* endpoints (this router
    never re-implements them); we only add the probes + classification on top.
  • The device-presence registry — every computer running WebAgent against the
    shared database (app/devices/): auto-discovered, online/offline, each self-
    reporting its platform + code repo. These render as the fleet tiles.
  • Cloud VMs — live API control of servers in a connected cloud account (list /
    start / stop / delete), reusing the deploy subsystem (app/deploy/), plus the
    cloud-ACCOUNT sign-in (connect / disconnect) and a reachability ping. Each VM
    carries the repo it runs, overlaid from an admin annotation.

Discovered + mounted by the page catalog (app/ui_pages/__init__.py
discover_routers, via this folder's page.json `router` field), so the API comes
and goes with the folder — NO edit to app/main.py. `.py` files under ui/ are never
served to the browser. Endpoints are admin-only via `resolve_admin_uid` (honours
open mode), mirroring app/api/deploy.py.

What lives where:
  • DB + vault config → the /admin/storage/* endpoints + app/db / app/secrets.
  • Cloud account keys + saved project/zone → the SAME encrypted vault + deploy.json
    the Deploy card uses (app/deploy/); never returned to the browser.
  • Per-VM repo annotations → ONE runtime-only file
    data/config/instance-annotations.json (gitignored), written atomically through
    app/util/config_io.py. No cloud secret ever lands here.

NOTE: the old Server Manager's manually-tracked "machines" + "sites" (hand-typed
boxes / URLs) were intentionally dropped — this page is auto-discovered instances +
cloud infrastructure you control, not a manual asset inventory.
REMOVE-WHEN: the Instances view is dropped from the admin page catalog.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.deploy import manager
from app.deploy import store as deploy_store
from app.deploy.registry import get_provider
from app.util.config_io import read_json, safe_write_json

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/instances", tags=["admin-instances"])

# Runtime-only store for per-VM repo annotations (gitignored). app/util/config_io.py
# self-creates the dir. Path depth: this file is ui/admin-tools/instances/server.py,
# so parents[3] is the repo root.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_STORE_FILE = _PROJECT_ROOT / "data" / "config" / "instance-annotations.json"


async def _require_admin(uid: str) -> None:
    # Honours open mode via the shared chokepoint (mirrors app/api/deploy.py).
    from app.auth.identity import resolve_admin_uid
    if not await resolve_admin_uid(uid):
        raise HTTPException(status_code=403, detail="Admin required")


# ══════════════════════════════════════════════════════════════════════════════
# Connection health — the active database + secrets vault (was Database & Devices)
# ══════════════════════════════════════════════════════════════════════════════
# Application-Data backends that put the data on a networked database more than
# one machine can point at (everything except the single-file local SQLite).
_REMOTE_DB = {"postgres", "supabase", "aws_rds", "gcp_cloud_sql", "azure_postgres", "neon"}
# How a vault choice travels across devices: cloud vaults are reachable from every
# machine (shared); the OS keyring is per-machine (local); the in-DB vault rides
# along inside the database; env is injected by the deployment.
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
    never writes or switches anything.

    The raw client's ``.execute()`` is a BLOCKING network round-trip. Run it on a
    worker thread (``asyncio.to_thread``) so a slow/remote database can't freeze the
    whole event loop for the duration — otherwise this admin probe stalls every
    other request (tiles, static assets, the New Deployment tab) behind it."""
    def _sync_probe() -> None:
        from app.db import get_db
        raw = get_db().get_raw_client()
        raw.table("sessions").select("id").limit(1).execute()
    try:
        await asyncio.to_thread(_sync_probe)
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
    # single source of truth; we only add live probes + the shared/local split.
    from app.admin.storage import get_storage_config
    cfg = await get_storage_config(requesting_user_id=requesting_user_id)
    db_active = cfg.get("db", {}).get("active", {}) or {}
    secrets = cfg.get("secrets", {}) or {}

    db_provider = (db_active.get("provider") or "sqlite").strip()
    db_shared = db_provider in _REMOTE_DB
    db_reach = await _probe_db()

    # The local SQLite copy ALWAYS probes green, so "reachable" alone hides a
    # silent fallback. Pull the DB layer's own health record to surface a drop to
    # this device's local copy loudly.
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


# ══════════════════════════════════════════════════════════════════════════════
# Device presence registry — every WebAgent instance on the shared database
# ══════════════════════════════════════════════════════════════════════════════
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
    # This device's tunnel is read LIVE (not from its possibly-stale heartbeat) so
    # the self tile reflects a start/stop instantly; other devices read the tunnel
    # state they published in their heartbeat's capabilities.
    try:
        from app.remote_access.manager import tunnel_snapshot
        my_tunnel = tunnel_snapshot()
    except Exception:
        my_tunnel = None

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
            # Admin display overrides (shared DB) — the UI prefers these over the
            # self-reported hostname / platform icon. Empty string = no override.
            "custom_label": d.get("custom_label") or "",
            "custom_icon": d.get("custom_icon") or "",
            "online": bool(d.get("online")),
            "last_seen": d.get("last_seen"),
            "is_self": is_self,
            "platform": caps.get("platform") or (my_caps.get("platform", "") if is_self else ""),
            "endpoint": caps.get("endpoint") or d.get("endpoint") or "",
            "repo": caps.get("repo") or (my_caps.get("repo", "") if is_self else ""),
            "branch": caps.get("branch") or (my_caps.get("branch", "") if is_self else ""),
            # Remote Access tunnel state this device published (self = live).
            "tunnel": (my_tunnel if is_self else caps.get("tunnel")) or None,
        })
    # Surface THIS device even before its first heartbeat (mirrors app/api/devices.py).
    if not seen_self:
        caps = identity.capabilities()
        out.insert(0, {
            "instance_id": me,
            "label": identity.device_label(),
            "custom_label": "",
            "custom_icon": "",
            "online": True,
            "last_seen": None,
            "is_self": True,
            "platform": caps.get("platform", ""),
            "endpoint": caps.get("endpoint") or _self_base_url(),
            "repo": caps.get("repo", ""),
            "branch": caps.get("branch", ""),
            "tunnel": my_tunnel or None,
        })
    return {"devices": out, "self": me}


def _self_base_url() -> str:
    """This app's own reachable base URL (configured / last-seen request host),
    used to fill the self row's endpoint before the first heartbeat lands. '' when
    only the bare fallback is known, so the UI falls back to localhost:<port>."""
    try:
        from app.admin.integrations import _get_base_url
        url = (_get_base_url() or "").strip().rstrip("/")
        return "" if url == "http://localhost:8000" else url
    except Exception:
        return ""


@router.post("/device/unlink")
async def device_unlink(body: UnlinkBody):
    """Remove a stale device from the presence registry — the admin "Unlink"
    action. Two guard rails, mirrored in the UI so neither side can bypass them:
      • never THIS device (it would just re-add itself immediately), and
      • only a device that is currently OFFLINE — a live one would re-appear on
        its next heartbeat, so unlinking it is misleading.
    This clears the record only; it never touches the actual machine."""
    await _require_admin(body.requesting_user_id)
    from app.devices import dispatch, identity

    iid = (body.instance_id or "").strip()
    if not iid:
        raise HTTPException(status_code=400, detail="An instance id is required")
    if iid == identity.device_id():
        raise HTTPException(status_code=400, detail="This device can't unlink itself.")

    rows = await dispatch.list_devices(online_within_seconds=60)
    match = next((d for d in rows if d.get("instance_id") == iid), None)
    if match is None:
        return {"ok": True, "already_gone": True}   # someone cleared it first
    if match.get("online"):
        raise HTTPException(
            status_code=409,
            detail="That device is online — it will re-link on its next check-in. "
                   "Unlink is only for offline devices.",
        )
    await dispatch.remove_device(iid)
    return {"ok": True}


@router.post("/device/rename")
async def device_rename(body: RenameBody):
    """Set a device's custom display name and/or icon — the admin "rename an
    instance" action, mirroring the Agents page. Both are stored on the device's
    shared-registry row (custom_label / custom_icon), so they persist across the
    device's heartbeats and show identically on every device pointed at this
    database. Sending an empty string for a field clears that override (the tile
    falls back to the machine's hostname / platform icon)."""
    await _require_admin(body.requesting_user_id)
    from app.devices import dispatch

    iid = (body.instance_id or "").strip()
    if not iid:
        raise HTTPException(status_code=400, detail="An instance id is required")
    # Normalise: strip provided fields; None stays None (leave that field alone).
    label = body.label.strip() if isinstance(body.label, str) else None
    icon = body.icon.strip() if isinstance(body.icon, str) else None
    if label is None and icon is None:
        raise HTTPException(status_code=400, detail="Nothing to update")
    ok = await dispatch.set_device_override(iid, label=label, icon=icon)
    if not ok:
        raise HTTPException(
            status_code=404,
            detail="That device isn't in the shared registry yet — it needs to "
                   "check in once before it can be renamed.",
        )
    return {"ok": True}


@router.post("/tunnel/control")
async def tunnel_control(body: TunnelBody):
    """Start / stop the Remote Access tunnel on ANOTHER instance (or this one).

    There is no direct network push to a remote device — instead we drop a small
    ACTION job into the shared cross-device queue (app/devices), addressed to the
    target's instance_id. The target's own worker claims it and runs its LOCAL
    tunnel controller (app/remote_access), so cloudflared starts on that machine,
    not here. The Instances page reflects the new state on the target's next
    heartbeat (its published tunnel snapshot flips to running + a public URL).

    Guard-railed like the other device actions: the target must be a known,
    ONLINE device — a job for an offline one would sit queued and fire whenever it
    next checks in, which is misleading for a click-to-start."""
    await _require_admin(body.requesting_user_id)
    from app.devices import dispatch

    iid = (body.instance_id or "").strip()
    if not iid:
        raise HTTPException(status_code=400, detail="An instance id is required")
    action = (body.action or "start").strip().lower()
    if action not in ("start", "stop"):
        raise HTTPException(status_code=400, detail="Unknown action")

    dev = await dispatch.resolve_target(iid, online_within_seconds=60)
    if dev is None:
        raise HTTPException(
            status_code=404,
            detail="That device isn't in the shared registry yet — it needs to "
                   "check in once before it can be controlled.",
        )
    if not dev.get("online"):
        raise HTTPException(
            status_code=409,
            detail="That device is offline — it can't start its tunnel until its "
                   "app is running and checking in again.",
        )

    payload: Dict[str, Any] = {"action": f"{action}_tunnel", "source": "instances_page"}
    if (body.provider or "").strip():
        payload["provider"] = body.provider.strip()
    job_id = await dispatch.enqueue(
        owner_user_id=body.requesting_user_id or "",
        prompt="",
        target_instance=iid,
        target_label=dev.get("label") or iid,
        payload=payload,
    )
    return {"ok": True, "job_id": job_id, "queued_for": iid}


# Fleet server/repo control — restart the server, or bring the target device's OWN
# app repo up to date / commit + push it, on ANY online instance. Same seam as the
# tunnel: drop a small ACTION job addressed to the target; its worker claims it and
# runs the action LOCALLY (app/devices/actions.py), reusing the very helpers the
# local Server Reset / Source-Control ⭐ button use. No direct network push.
_DEVICE_ACTIONS = {
    "restart": "restart_server",
    "git_pull": "git_pull",
    "git_commit_push": "git_commit_push",
}


@router.post("/device/control")
async def device_control(body: DeviceControlBody):
    """Enqueue a restart / git-pull / commit-and-push on a target instance.

    Guard-railed like tunnel_control: the target must be a known, ONLINE device —
    a job for an offline one would sit queued and fire whenever it next checks in,
    which is misleading for a click-to-run. Returns the job id so the caller can
    poll the outcome via ``/device/action-status`` (git actions leave no visible
    heartbeat state, unlike a tunnel flip)."""
    await _require_admin(body.requesting_user_id)
    from app.devices import dispatch

    iid = (body.instance_id or "").strip()
    if not iid:
        raise HTTPException(status_code=400, detail="An instance id is required")
    action = (body.action or "").strip().lower()
    if action not in _DEVICE_ACTIONS:
        raise HTTPException(status_code=400, detail="Unknown action")

    dev = await dispatch.resolve_target(iid, online_within_seconds=60)
    if dev is None:
        raise HTTPException(
            status_code=404,
            detail="That device isn't in the shared registry yet — it needs to "
                   "check in once before it can be controlled.",
        )
    if not dev.get("online"):
        raise HTTPException(
            status_code=409,
            detail="That device is offline — it can't run this until its app is "
                   "running and checking in again.",
        )

    payload: Dict[str, Any] = {"action": _DEVICE_ACTIONS[action], "source": "instances_page"}
    job_id = await dispatch.enqueue(
        owner_user_id=body.requesting_user_id or "",
        prompt="",
        target_instance=iid,
        target_label=dev.get("label") or iid,
        payload=payload,
    )
    return {"ok": True, "job_id": job_id, "queued_for": iid}


@router.get("/device/action-status")
async def device_action_status(job_id: str = Query(...), requesting_user_id: str = Query("")):
    """Poll a fleet ACTION job's outcome so the Instances page can show a plain
    result line (git pull / commit+push report no heartbeat state). Returns the
    job's status (pending | claimed | done | error | skipped) plus its short
    result excerpt / error message once the target has run it."""
    await _require_admin(requesting_user_id)
    from app.db import get_db

    job = await get_db().get_device_job((job_id or "").strip())
    if not job:
        raise HTTPException(status_code=404, detail="No such action job.")
    return {
        "status": job.get("status"),
        "result": job.get("result_excerpt") or "",
        "error": job.get("error") or "",
    }


# ══════════════════════════════════════════════════════════════════════════════
# Cloud VMs + cloud-account sign-in (was Server Manager)
# ══════════════════════════════════════════════════════════════════════════════
def _load_store() -> Dict[str, Any]:
    data = read_json(_STORE_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("annotations", {})
    return data


def _save_store(data: Dict[str, Any]) -> None:
    safe_write_json(_STORE_FILE, data)


def _annotation(data: Dict[str, Any], key: str) -> Dict[str, Any]:
    ann = data.get("annotations", {}).get(key)
    return dict(ann) if isinstance(ann, dict) else {}


class ActionBody(BaseModel):
    requesting_user_id: str = ""
    provider: str
    action: str            # "start" | "stop" | "delete"
    zone: str
    name: str


class ConnectBody(BaseModel):
    # The sign-in panel's submission: the cloud 'id' fields (e.g. project) AND the
    # secret 'key', mixed in one map keyed by field key. The manager routes each
    # value to deploy.json or the vault by which field list it belongs to.
    requesting_user_id: str = ""
    provider: str
    values: dict = {}


class ProviderBody(BaseModel):
    requesting_user_id: str = ""
    provider: str
    # "Sign out" forgets only the key; "Remove account" also clears the saved id.
    forget_config: bool = False


class AnnotateBody(BaseModel):
    # Set the repo (and optional friendly label) override for an auto-discovered
    # cloud VM. key = "cloud:<provider>|<zone>|<name>".
    requesting_user_id: str = ""
    key: str
    repo: str = ""
    label: str = ""


class PingBody(BaseModel):
    requesting_user_id: str = ""
    address: str           # host[:port] or http(s)://url


class UnlinkBody(BaseModel):
    # Drop a stale device's presence row from the shared registry. Offline-only +
    # never this device — both enforced server-side below, not just in the UI.
    requesting_user_id: str = ""
    instance_id: str


class RenameBody(BaseModel):
    # Set a device's admin display overrides in the shared registry. Each field is
    # optional: omitted/None = leave unchanged; "" = clear the override (fall back
    # to the self-reported hostname / platform icon); any other string = set it.
    requesting_user_id: str = ""
    instance_id: str
    label: Optional[str] = None
    icon: Optional[str] = None


class TunnelBody(BaseModel):
    # Start/stop the Remote Access tunnel on a target instance via the cross-device
    # job queue. provider is an optional hint; blank = the device's own configured
    # managed method (mirrors its boot auto-start).
    requesting_user_id: str = ""
    instance_id: str
    action: str = "start"       # "start" | "stop"
    provider: str = ""


class DeviceControlBody(BaseModel):
    # Run a server/repo fleet ACTION on a target instance via the cross-device job
    # queue (sibling of TunnelBody). action: restart | git_pull | git_commit_push.
    requesting_user_id: str = ""
    instance_id: str
    action: str = "restart"


# This module is imported dynamically (page-catalog drop-in) and uses
# `from __future__ import annotations`, so the request models carry STRING
# annotations FastAPI can't resolve lazily later. Resolve them now, while the
# module namespace is live (same fix as ui/admin-tools/update/server.py).
for _m in (ActionBody, ConnectBody, ProviderBody, AnnotateBody, PingBody, UnlinkBody, RenameBody, TunnelBody, DeviceControlBody):
    _m.model_rebuild()


@router.get("/providers")
async def providers(requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    return await manager.manageable_providers()


@router.get("/cloud-instances")
async def cloud_instances(provider: str = Query(...), requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    if not get_provider(provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    res = await manager.list_instances(provider)
    # Overlay the repo each VM runs: the admin's annotation wins; otherwise the
    # "this app" VM inherits the repo the Deploy card installed.
    store_data = _load_store()
    deploy_cfg = deploy_store.get_config(provider)
    default_repo = (deploy_cfg.get("repo_url") or "").strip()
    for inst in res.get("instances", []) or []:
        key = "cloud:%s|%s|%s" % (provider, inst.get("zone", ""), inst.get("name", ""))
        ann = _annotation(store_data, key)
        repo = ann.get("repo") or (default_repo if inst.get("is_this_app") else "")
        inst["repo"] = repo
        inst["annotation_key"] = key
    return res


@router.post("/annotate")
async def annotate(body: AnnotateBody):
    await _require_admin(body.requesting_user_id)
    key = (body.key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="A target key is required")
    data = _load_store()
    annotations = data.setdefault("annotations", {})
    repo = (body.repo or "").strip()
    label = (body.label or "").strip()
    if not repo and not label:
        annotations.pop(key, None)          # clearing both = forget the override
    else:
        ann = annotations.setdefault(key, {})
        ann["repo"] = repo
        if label:
            ann["label"] = label
        else:
            ann.pop("label", None)
    _save_store(data)
    return {"ok": True}


# ── Reachability ping (devices + cloud VMs) ──
# Server-side so it isn't blocked by the browser's cross-origin / mixed-content
# rules. http(s):// → an HTTP request (any response = reachable). Otherwise a
# plain TCP connect to host[:port] (default port 80).
@router.post("/ping")
async def ping(body: PingBody):
    await _require_admin(body.requesting_user_id)
    addr = (body.address or "").strip()
    if not addr:
        return {"reachable": False, "detail": "No address to check."}

    if addr.startswith("http://") or addr.startswith("https://"):
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
                r = await client.get(addr)
            return {"reachable": r.status_code < 500,
                    "detail": "HTTP %s" % r.status_code, "status": r.status_code}
        except Exception as e:
            return {"reachable": False, "detail": str(e) or "Could not reach the site."}

    host = addr
    port = 80
    if ":" in addr and not addr.count(":") > 1:   # host:port (skip IPv6 literals)
        host, _, p = addr.partition(":")
        try:
            port = int(p)
        except ValueError:
            port = 80
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=6.0)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return {"reachable": True, "detail": "Reachable on port %s" % port}
    except Exception as e:
        return {"reachable": False, "detail": "No response on port %s (%s)" % (port, type(e).__name__)}


@router.post("/connect")
async def connect(body: ConnectBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    res = await manager.save_connection(body.provider, body.values or {})
    if not res.get("ok"):
        raise HTTPException(status_code=500, detail=res.get("detail") or "Could not connect")
    return res


@router.post("/disconnect")
async def disconnect(body: ProviderBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    return await manager.disconnect(body.provider, body.forget_config)


# ── Start / Stop / Delete one cloud server (streaming NDJSON) ──
def _stream(agen):
    async def _gen():
        try:
            async for evt in agen:
                yield json.dumps(evt) + "\n"
        except Exception as e:  # never leave the client's reader hanging
            logger.exception("instances cloud-action stream crashed")
            yield json.dumps({"phase": "done", "result": {"ok": False, "message": str(e) or "failed"}}) + "\n"
    return StreamingResponse(
        _gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/instance/action")
async def instance_action(body: ActionBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    if body.action not in ("start", "stop", "delete"):
        raise HTTPException(status_code=400, detail="Unknown action")
    return _stream(manager.run_instance_action(body.provider, body.action, body.zone, body.name))

"""Server Manager — drop-in BACKEND for the Server Manager admin page:
/admin/server-manager/*

A single hub for the cloud servers + manually-tracked boxes the admin runs:

  • Cloud VMs    — live API control of servers in a cloud account (start / stop /
                   delete), reusing the deploy subsystem. Today: Google Compute.
  • Machines     — manually-tracked boxes that do NOT run WebAgent (a remote
                   Linux/Termux/Windows/Mac host): name, address, repo, notes,
                   plus a reachability ping.
  • Sites        — manually-tracked hosted sites / URLs: name, url, repo, notes,
                   plus an HTTP health ping.

Linked WebAgent DEVICES (the presence registry) moved to their own page —
ui/admin-tools/database-devices/ — framed as "who else is signed in to the shared
database". This is the evolution of the old "Cloud VMs" page.

Discovered + mounted by the page catalog (app/ui_pages/__init__.py
discover_routers, via this folder's page.json `router` field), so the API comes
and goes with the folder — NO edit to app/main.py. `.py` files under ui/ are
never served to the browser.

What lives where:
  • Cloud account keys + saved project/zone → the SAME encrypted vault + deploy.json
    the Deploy card uses (app/deploy/); this page never re-implements them and
    never returns a key to the browser.
  • Manually-added machines/sites + per-entity repo annotations → ONE runtime-only
    file data/config/server-manager.json (gitignored), written atomically through
    app/util/config_io.py. No cloud secret ever lands here.

ALL endpoints are admin-only, gated through `resolve_admin_uid` (honours open
mode) exactly like app/api/deploy.py. Sister backend: ui/admin-tools/update/.
REMOVE-WHEN: the Server Manager view is dropped from the admin page catalog.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.deploy import manager
from app.deploy import store as deploy_store
from app.deploy.registry import get_provider
from app.util.config_io import read_json, safe_write_json

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/server-manager", tags=["admin-server-manager"])

# Runtime-only store for manual entries + repo annotations (gitignored). Mirrors
# app/deploy/store.py's deploy.json. app/util/config_io.py self-creates the dir.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_STORE_FILE = _PROJECT_ROOT / "data" / "config" / "server-manager.json"

# The kinds a manually-tracked entry may take. "site" is a hosted URL; the rest
# are physical/remote machines that don't run WebAgent (so can't auto-discover).
_MACHINE_KINDS = {"linux", "termux", "windows", "mac", "other"}
_ENTRY_KINDS = _MACHINE_KINDS | {"site"}


async def _require_admin(uid: str) -> None:
    # Honours open mode via the shared chokepoint (mirrors app/api/deploy.py).
    from app.auth.identity import resolve_admin_uid
    if not await resolve_admin_uid(uid):
        raise HTTPException(status_code=403, detail="Admin required")


# ── Local store helpers (manual entries + annotations) ──────────────────────
def _load_store() -> Dict[str, Any]:
    data = read_json(_STORE_FILE, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("entries", [])
    data.setdefault("annotations", {})
    return data


def _save_store(data: Dict[str, Any]) -> None:
    safe_write_json(_STORE_FILE, data)


def _annotation(data: Dict[str, Any], key: str) -> Dict[str, Any]:
    ann = data.get("annotations", {}).get(key)
    return dict(ann) if isinstance(ann, dict) else {}


# ── Models ──
class ActionBody(BaseModel):
    requesting_user_id: str = ""
    provider: str
    action: str            # "start" | "stop" | "delete"
    zone: str
    name: str


class ConnectBody(BaseModel):
    # The sign-in panel's submission: the cloud 'id' fields (e.g. project) AND
    # the secret 'key', mixed in one map keyed by field key. The manager routes
    # each value to deploy.json or the vault by which field list it belongs to.
    requesting_user_id: str = ""
    provider: str
    values: dict = {}


class ProviderBody(BaseModel):
    requesting_user_id: str = ""
    provider: str
    # "Sign out" forgets only the key; "Remove account" also clears the saved id
    # (the connect-config keys) so the account drops out of the page's list.
    forget_config: bool = False


class EntryBody(BaseModel):
    # Add or update one manually-tracked machine / site. An id present = update.
    requesting_user_id: str = ""
    id: str = ""
    kind: str = "linux"
    name: str = ""
    address: str = ""      # host / ip (machines) or full URL (sites)
    repo: str = ""
    notes: str = ""


class RemoveBody(BaseModel):
    requesting_user_id: str = ""
    id: str


class AnnotateBody(BaseModel):
    # Set the repo (and optional friendly label) override for an auto-discovered
    # cloud VM or device. key = "cloud:<provider>|<zone>|<name>" | "device:<id>".
    requesting_user_id: str = ""
    key: str
    repo: str = ""
    label: str = ""


class PingBody(BaseModel):
    requesting_user_id: str = ""
    address: str           # host[:port] or http(s)://url


# This module is imported dynamically (page-catalog drop-in) and uses
# `from __future__ import annotations`, so the request models carry STRING
# annotations FastAPI can't resolve lazily later. Resolve them now, while the
# module namespace is live (same fix as ui/admin-tools/update/server.py).
for _m in (ActionBody, ConnectBody, ProviderBody, EntryBody, RemoveBody,
           AnnotateBody, PingBody):
    _m.model_rebuild()


# ── Cloud providers the page can manage ──
@router.get("/providers")
async def providers(requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    return await manager.manageable_providers()


# ── List every server one cloud target can see (with repo overlay) ──
@router.get("/instances")
async def instances(provider: str = Query(...), requesting_user_id: str = ""):
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


# NOTE: the WebAgent DEVICES list (presence registry) moved to its own page —
# ui/admin-tools/database-devices/ (GET /admin/database-devices/devices) — which
# frames devices as "who else is signed in to the shared database". Server Manager
# now covers cloud VMs + manually-tracked machines/sites only.


# ── Manually-tracked machines + sites ──
@router.get("/entries")
async def entries(requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    return {"entries": _load_store().get("entries", [])}


@router.post("/entries")
async def upsert_entry(body: EntryBody):
    await _require_admin(body.requesting_user_id)
    kind = (body.kind or "").strip().lower()
    if kind not in _ENTRY_KINDS:
        raise HTTPException(status_code=400, detail="Unknown kind")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="A name is required")

    data = _load_store()
    entries_list = data.setdefault("entries", [])
    fields = {
        "kind": kind,
        "name": name,
        "address": (body.address or "").strip(),
        "repo": (body.repo or "").strip(),
        "notes": (body.notes or "").strip(),
    }
    if body.id:
        for e in entries_list:
            if e.get("id") == body.id:
                e.update(fields)
                _save_store(data)
                return {"ok": True, "entry": e}
        raise HTTPException(status_code=404, detail="Entry not found")
    entry = {"id": uuid.uuid4().hex[:12], "created_at": time.time(), **fields}
    entries_list.append(entry)
    _save_store(data)
    return {"ok": True, "entry": entry}


@router.post("/entries/remove")
async def remove_entry(body: RemoveBody):
    await _require_admin(body.requesting_user_id)
    data = _load_store()
    before = data.get("entries", [])
    after = [e for e in before if e.get("id") != body.id]
    if len(after) == len(before):
        raise HTTPException(status_code=404, detail="Entry not found")
    data["entries"] = after
    _save_store(data)
    return {"ok": True}


# ── Repo / label override for an auto-discovered cloud VM or device ──
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


# ── Reachability ping (machines + sites) ──
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


# ── Connect a cloud account from this page (the sign-in panel) ──
@router.post("/connect")
async def connect(body: ConnectBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    res = await manager.save_connection(body.provider, body.values or {})
    if not res.get("ok"):
        raise HTTPException(status_code=500, detail=res.get("detail") or "Could not connect")
    return res


# ── Sign out / remove a cloud account ──
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
            logger.exception("server-manager stream crashed")
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

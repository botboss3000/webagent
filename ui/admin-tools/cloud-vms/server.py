"""Cloud VMs — drop-in BACKEND for the Cloud VMs admin page: /admin/cloud-vms/*

A full manager for the virtual machines living in the admin's cloud account —
list every server, then Open / Start / Stop / Delete each one. This is the
natural evolution of the Deploy card (App Settings → Deploy), which only ever
remembers the SINGLE last server it created; this page sees them all.

Discovered + mounted by the page catalog (app/ui_pages/__init__.py
discover_routers, via this folder's page.json `router` field), so the API comes
and goes with the folder — NO edit to app/main.py. `.py` files under ui/ are
never served to the browser.

Reuses the whole deploy subsystem: each cloud is a drop-in provider under
app/deploy/providers/ that opts in by setting `supports_instances` (today:
google_vm). The cloud key + saved project/zone come from the SAME encrypted vault
+ deploy.json the Deploy card uses; this page never re-implements them and never
returns a key to the browser.

⚠ Credential lifecycle: the Deploy card auto-discards the cloud key after a
successful deploy ("Forget cloud key after deploy", default on), so after a
normal deploy NO key is stored and listing degrades gracefully to a "needs key"
notice + an inline key field (POST /credentials writes the SAME vault entry).

ALL endpoints are admin-only, gated through `resolve_admin_uid` (honours open
mode) exactly like app/api/deploy.py. Sister backend: ui/admin-tools/update/.
REMOVE-WHEN: the Cloud VMs view is dropped from the admin page catalog.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.deploy import manager
from app.deploy.registry import get_provider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/cloud-vms", tags=["admin-cloud-vms"])


async def _require_admin(uid: str) -> None:
    # Honours open mode via the shared chokepoint (mirrors app/api/deploy.py).
    from app.auth.identity import resolve_admin_uid
    if not await resolve_admin_uid(uid):
        raise HTTPException(status_code=403, detail="Admin required")


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


# This module is imported dynamically (page-catalog drop-in) and uses
# `from __future__ import annotations`, so the request models carry STRING
# annotations FastAPI can't resolve lazily later. Resolve them now, while the
# module namespace is live (same fix as ui/admin-tools/update/server.py).
ActionBody.model_rebuild()
ConnectBody.model_rebuild()
ProviderBody.model_rebuild()


# ── Providers the page can manage ──
@router.get("/providers")
async def providers(requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    return await manager.manageable_providers()


# ── List every server one target can see ──
@router.get("/instances")
async def instances(provider: str = Query(...), requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    if not get_provider(provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    return await manager.list_instances(provider)


# ── Connect a cloud account from this page (the sign-in panel) ──
# Saves the 'id' fields (project) to deploy.json and the secret 'key' to the SAME
# vault entry the Deploy card uses, so an account connected here also enables a
# future tear-down there. Never echoes a key back to the browser.
@router.post("/connect")
async def connect(body: ConnectBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    res = await manager.save_connection(body.provider, body.values or {})
    if not res.get("ok"):
        raise HTTPException(status_code=500, detail=res.get("detail") or "Could not connect")
    return res


# ── Sign out / remove an account ──
# Default ("Sign out") forgets only the stored cloud key (keeps the non-secret
# id). With forget_config ("Remove account") the saved id is cleared too, so the
# account leaves the page's list.
@router.post("/disconnect")
async def disconnect(body: ProviderBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    return await manager.disconnect(body.provider, body.forget_config)


# ── Start / Stop / Delete one server (streaming NDJSON) ──
def _stream(agen):
    async def _gen():
        try:
            async for evt in agen:
                yield json.dumps(evt) + "\n"
        except Exception as e:  # never leave the client's reader hanging
            logger.exception("cloud-vms stream crashed")
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

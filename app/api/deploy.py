"""Deploy admin API: /admin/deploy/*

Backs the Deploy card in App Config → App Settings. Live one-click deploy of
WebAgent onto a cloud target. ALL endpoints are admin-only — deploying spends
money on the admin's cloud account and exposes the (admin-gated) tools, so it is
locked to admins, exactly like Remote Access.

Cloud keys ride in via these endpoints into the encrypted vault
(app/deploy/credentials.py) and are never returned to the browser. The deploy /
tear-down endpoints stream NDJSON progress (like the Commit ⭐ button) so the
panel shows a live log.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.deploy import credentials, manager, store
from app.deploy.registry import get_provider

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/deploy", tags=["admin-deploy"])


async def _require_admin(uid: str) -> None:
    # Honors open mode via the shared chokepoint (mirrors app/admin/remote_access).
    from app.auth.identity import resolve_admin_uid
    if not await resolve_admin_uid(uid):
        raise HTTPException(status_code=403, detail="Admin required")


# ── Models ──
class UserBody(BaseModel):
    requesting_user_id: str = ""


class SelectBody(UserBody):
    provider: str


class ConfigBody(UserBody):
    provider: str
    config: dict = {}


class CredentialsBody(UserBody):
    provider: str
    values: dict = {}


# ── Saved servers (profile-aware targets, e.g. ssh_vm) ──
class ServerSaveBody(UserBody):
    provider: str
    server_id: str = ""            # blank = create a new saved server
    label: str = ""                # the nickname shown in the picker
    values: dict = {}              # config + secret fields from the form


class ServerSelectBody(UserBody):
    provider: str
    server_id: str = ""            # blank = "New server…" (clear the form)


class ServerIdBody(UserBody):
    provider: str
    server_id: str


class ManualCommandBody(UserBody):
    provider: str = "termux"       # any "manual" target: termux | windows | macos
    github_url: str = ""
    visibility: str = "public"     # "public" | "private"
    token: str = ""                # only for a private repo; never stored
    install_dir: str = ""          # optional folder to install into (blank = default)
    admin_password: str = ""       # optional; when set it is embedded in the command
                                   # (in plain text), never stored server-side
    persist: bool = False          # save the non-secret URL+visibility+folder (on
                                   # blur, NOT on every live-preview keystroke)


# ── Local deployments (this app + sibling checkouts) — see the instances section ──
class InstFolderBody(UserBody):
    folder: str = ""


class InstAddBody(UserBody):
    label: str = ""
    folder: str = ""
    port: int = 0


class InstUpdateBody(UserBody):
    id: str
    label: str = ""
    folder: str = ""
    port: int = 0


class InstIdBody(UserBody):
    id: str


class HubPortBody(UserBody):
    port: int = 0


# ── Catalog (status) ──
@router.get("/catalog")
async def catalog(requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    return await manager.build_catalog()


# ── Which target is selected ──
@router.post("/select")
async def select(body: SelectBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    store.set_active_provider(body.provider)
    return {"ok": True, "active_provider": body.provider}


# ── Save non-secret settings ──
@router.post("/config")
async def save_config(body: ConfigBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    store.save_config(body.provider, body.config or {})
    return {"ok": True}


# ── Save / clear cloud keys (vault) ──
@router.post("/credentials")
async def save_credentials(body: CredentialsBody):
    await _require_admin(body.requesting_user_id)
    p = get_provider(body.provider)
    if not p:
        raise HTTPException(status_code=400, detail="Unknown target")
    ok = await credentials.save(body.provider, p.credential_fields, body.values or {})
    if not ok:
        raise HTTPException(status_code=500, detail="Could not save credentials")
    view = await credentials.public_view(body.provider, p.credential_fields, p.credential_required)
    return {"ok": True, "configured": view["configured"], "credentials_set": view["secrets_set"]}


@router.post("/credentials/clear")
async def clear_credentials(body: SelectBody):
    await _require_admin(body.requesting_user_id)
    p = get_provider(body.provider)
    if not p:
        raise HTTPException(status_code=400, detail="Unknown target")
    await credentials.forget(body.provider)
    return {"ok": True}


# ── Test connection ──
@router.post("/test")
async def test(body: SelectBody):
    await _require_admin(body.requesting_user_id)
    p = get_provider(body.provider)
    if not p:
        raise HTTPException(status_code=400, detail="Unknown target")
    ok_avail, reason = p.available()
    if not ok_avail:
        return {"ok": False, "detail": reason or "This target is unavailable here."}
    config = {**{f["key"]: f.get("default") for f in p.config_fields if "default" in f},
              **store.get_config(body.provider)}
    creds = await credentials.read(body.provider, p.credential_fields)
    try:
        return await p.test(config, creds)
    except Exception as e:
        logger.exception("deploy test failed for %s", body.provider)
        return {"ok": False, "detail": str(e)}


# ── Saved servers (profile-aware targets: the SSH one) ──
# Let an admin keep several servers as pick-from-a-dropdown profiles. Saving/
# selecting/deleting a server manages the profile store + its vault row; selecting
# also loads it into the working slot so a following Deploy / Test / Tear-down acts
# on it (those endpoints are unchanged — they always use "whatever is loaded").
@router.post("/servers/save")
async def servers_save(body: ServerSaveBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    res = await manager.save_server(body.provider, body.label, body.values or {}, body.server_id)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("detail", "Could not save the server."))
    return res


@router.post("/servers/select")
async def servers_select(body: ServerSelectBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    res = await manager.select_server(body.provider, body.server_id)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("detail", "Could not load the server."))
    return res


@router.post("/servers/delete")
async def servers_delete(body: ServerIdBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    return await manager.delete_server(body.provider, body.server_id)


# ── Manual install (Linux/Termux, Windows, macOS) — build the command + QR ──
# Each "manual" target gets its OWN bespoke row in the Deploy card (not the
# cloud-provider dropdown): the admin gives a GitHub URL + public/private (+ a
# token for private), and we hand back the one command to paste into a terminal /
# PowerShell plus a QR of that command. The row shows the command LIVE as they
# type and calls this on every (debounced) change, so it must never error — a
# blank URL / token comes back as a placeholder command. The token is used only
# to build the command (never stored); the non-secret github_url + visibility
# persist only when ``persist`` is set (on blur), so keystrokes don't churn config.
#
# This is generic over ANY manual provider (it dispatches on ``body.provider``):
# a new platform = a new drop-in provider with a ``build_command``, no edit here.
async def _build_manual_command(body: ManualCommandBody):
    await _require_admin(body.requesting_user_id)
    provider_id = body.provider or "termux"
    p = get_provider(provider_id)
    if not p or not hasattr(p, "build_command") or not getattr(p, "manual", False):
        raise HTTPException(status_code=400, detail="Unknown install target")
    plan = p.build_command(body.github_url, body.visibility, body.token,
                           install_dir=body.install_dir,
                           admin_password=body.admin_password)
    if body.persist:
        # Persist only the NON-secret choices so the row pre-fills next time. The
        # admin PASSWORD is never stored (it only ever lives in the shown command).
        store.save_config(provider_id, {"github_url": (body.github_url or "").strip(),
                                        "visibility": body.visibility or "public",
                                        "install_dir": (body.install_dir or "").strip()})
    qr_svg = None
    try:
        from app.remote_access import netinfo
        qr_svg = netinfo.qr_svg(plan["command"])
    except Exception:
        qr_svg = None
    return {
        "ok": True,
        "command": plan["command"],
        "qr_svg": qr_svg,
        "steps": plan.get("steps", []),
        "instructions": plan.get("instructions", ""),
        "reach_note": plan.get("reach_note", ""),
        "run_command": plan.get("run_command", ""),
        "install_dir": plan.get("install_dir", ""),
        "private": plan.get("private", False),
        "default_repo": plan.get("default_repo", False),
        "placeholder_token": plan.get("placeholder_token", False),
        "prewire": plan.get("prewire", False),
        "warning": plan.get("warning", ""),
    }


@router.post("/command")
async def manual_command(body: ManualCommandBody):
    return await _build_manual_command(body)


# Back-compat alias for the original Termux-only route (older cached clients).
@router.post("/termux/command")
async def termux_command(body: ManualCommandBody):
    body.provider = body.provider or "termux"
    return await _build_manual_command(body)


# ── Deploy / tear down (streaming NDJSON) ──
def _stream(agen):
    async def _gen():
        try:
            async for evt in agen:
                yield json.dumps(evt) + "\n"
        except Exception as e:  # never leave the client's reader hanging
            logger.exception("deploy stream crashed")
            yield json.dumps({"phase": "done", "result": {"ok": False, "message": str(e) or "failed"}}) + "\n"
    return StreamingResponse(
        _gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post("/deploy")
async def deploy(body: SelectBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    return _stream(manager.run_deploy(body.provider))


@router.post("/destroy")
async def destroy(body: SelectBody):
    await _require_admin(body.requesting_user_id)
    if not get_provider(body.provider):
        raise HTTPException(status_code=400, detail="Unknown target")
    return _stream(manager.run_destroy(body.provider))


# ── Local deployments (this app + sibling checkouts on this machine) ──────────
# The Deploy card's "Current deployment" list. Backed by app/local_instances.py:
# this app (the hub) plus any OTHER WebAgent repo folders registered to run as
# background servers on their own ports (8081, 8082, …). Same registry + engine the
# Dashboard's instance-switcher header uses; these are the Deploy card's own thin,
# admin-gated wrappers over it (self-contained, so the card doesn't depend on any
# other page's router). Start/Stop stream NDJSON like Deploy / Tear-down.
@router.get("/instances")
async def instances_list(requesting_user_id: str = ""):
    await _require_admin(requesting_user_id)
    from app import local_instances as li
    return {"instances": await li.list_instances(), "hub_port": li.hub_port()}


@router.post("/instances/validate")
async def instances_validate(body: InstFolderBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    return li.validate_folder(body.folder)


@router.post("/instances/add")
async def instances_add(body: InstAddBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    try:
        return {"ok": True, "instance": li.add_instance(body.label, body.folder, body.port)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/instances/update")
async def instances_update(body: InstUpdateBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    try:
        return {"ok": True, "instance": li.update_instance(
            body.id, label=body.label, folder=body.folder, port=body.port)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/instances/remove")
async def instances_remove(body: InstIdBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    try:
        li.remove_instance(body.id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/instances/start")
async def instances_start(body: InstIdBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    return _stream(li.start_instance(body.id))


@router.post("/instances/stop")
async def instances_stop(body: InstIdBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    return _stream(li.stop_instance(body.id))


# Change the CURRENT app's own port. Persists the new port (run.py reads it on the
# next start) and relaunches this process so it rebinds — the browser is on the OLD
# port, so the response tells the caller where to reopen. The relauncher waits for
# us to exit, then brings us back on the new port (or a supervisor does — run.py
# reads the same saved port either way).
@router.post("/instances/set-hub-port")
async def instances_set_hub_port(body: HubPortBody):
    await _require_admin(body.requesting_user_id)
    from app import local_instances as li
    try:
        new_port = li.set_hub_port(body.port)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    result = {"ok": True, "port": new_port, "url": f"http://localhost:{new_port}/"}
    try:
        from app import relauncher
        restart = relauncher.trigger_restart(port=new_port)
        result["restart"] = restart
        result["auto_restart"] = bool(restart.get("auto_restart"))
    except Exception as e:  # noqa: BLE001  — saved anyway; just couldn't self-relaunch
        logger.exception("hub-port relaunch failed")
        result["auto_restart"] = False
        result["restart"] = {"status": "error", "reason": str(e)}
    return result

"""Deploy admin API: /admin/deploy/*

Backs the Deploy card in App Config → App Settings. Live one-click deploy of
webAgent onto a cloud target. ALL endpoints are admin-only — deploying spends
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


class ManualCommandBody(UserBody):
    provider: str = "termux"       # any "manual" target: termux | windows | macos
    github_url: str = ""
    visibility: str = "public"     # "public" | "private"
    token: str = ""                # only for a private repo; never stored
    persist: bool = False          # save the non-secret URL+visibility (on blur,
                                   # NOT on every live-preview keystroke)


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
    plan = p.build_command(body.github_url, body.visibility, body.token)
    if body.persist:
        # Persist only the NON-secret choices so the row pre-fills next time.
        store.save_config(provider_id, {"github_url": (body.github_url or "").strip(),
                                        "visibility": body.visibility or "public"})
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
        "private": plan.get("private", False),
        "placeholder_repo": plan.get("placeholder_repo", False),
        "placeholder_token": plan.get("placeholder_token", False),
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

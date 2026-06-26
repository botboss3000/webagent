"""Deploy orchestration — the glue between the API and a target provider.

Builds the panel's catalog (every drop-in target + its forms + whether its keys
are present + its last deployment), and runs the streaming deploy / destroy:
loads the saved config + the vault credentials, drives the provider's async
generator, stamps the deployment record on success, and — when the target's
"Forget keys after deploy" option is on — DELETES the cloud key from the vault so
the app holds no standing cloud access.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict

from app.deploy import credentials, store
from app.deploy.base import done, ev
from app.deploy.registry import get_provider, list_providers

logger = logging.getLogger(__name__)


def _field_defaults(fields) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for f in fields:
        if "default" in f:
            out[f["key"]] = f["default"]
    return out


async def build_catalog() -> Dict[str, Any]:
    """Everything the panel needs to render, for every discovered target."""
    providers = []
    for p in list_providers():
        ok_avail, reason = p.available()
        cfg = store.get_config(p.id)
        # Layer saved config over the field defaults so the form shows sensible
        # starting values on a fresh install.
        merged = {**_field_defaults(p.config_fields), **cfg}
        cred_view = await credentials.public_view(p.id, p.credential_fields, p.credential_required)
        providers.append({
            "id": p.id,
            "display_name": p.display_name,
            "icon": p.icon,
            "summary": p.summary,
            "requires": p.requires,
            "manual": bool(getattr(p, "manual", False)),
            "available": ok_avail,
            "unavailable_reason": reason,
            "config_fields": p.config_fields,
            "credential_fields": [
                {k: v for k, v in f.items() if k != "secret"} | {"secret": bool(f.get("secret"))}
                for f in p.credential_fields
            ],
            "config": merged,
            "credentials_set": cred_view["secrets_set"],
            "configured": cred_view["configured"],
            "deployment": store.get_deployment(p.id),
        })
    return {
        "providers": providers,
        "active_provider": store.get_active_provider() or (providers[0]["id"] if providers else ""),
    }


async def run_deploy(provider_id: str) -> AsyncIterator[Dict[str, Any]]:
    p = get_provider(provider_id)
    if not p:
        yield done({"ok": False, "message": "Unknown deploy target."})
        return
    ok_avail, reason = p.available()
    if not ok_avail:
        yield done({"ok": False, "message": reason or "This target is unavailable."})
        return

    config = {**_field_defaults(p.config_fields), **store.get_config(provider_id)}
    creds = await credentials.read(provider_id, p.credential_fields)
    required = p.credential_required if p.credential_required is not None else \
        [f["key"] for f in p.credential_fields if f.get("secret")]
    if required and not all(str(creds.get(k, "")).strip() for k in required):
        yield done({"ok": False, "message": "Cloud key is missing. Fill in the credentials first."})
        return

    final: Dict[str, Any] = {"ok": False, "message": "Deploy produced no result."}
    try:
        async for event in p.deploy(config, creds):
            if event.get("phase") == "done":
                final = event.get("result", final)
            yield event
    except Exception as e:
        logger.exception("deploy run crashed for %s", provider_id)
        yield done({"ok": False, "message": str(e) or "Deploy crashed."})
        return

    if final.get("ok"):
        store.set_deployment(provider_id, {
            "server": final.get("server", ""),
            "ip": final.get("ip", ""),
            "zone": final.get("zone", ""),
            "project": final.get("project", ""),
            "public_url": final.get("public_url", ""),
            "state": final.get("state", "running"),
            # Manual targets (e.g. Termux) return a copy-paste command + how-to
            # instructions instead of a created server; persist them so the panel
            # can re-show the command box on reload. Cloud targets leave these blank.
            "command": final.get("command", ""),
            "instructions": final.get("instructions", ""),
        })
        # Auto-forget the cloud key after a successful deploy — but only for
        # targets that actually HAVE a cloud key. A manual target (no credentials)
        # would otherwise print a misleading "key discarded" line.
        if config.get("forget_keys", True) and p.credential_fields:
            await credentials.forget(provider_id)
            yield ev("Cloud key discarded from the vault.", phase="forgot", level="ok")


async def run_destroy(provider_id: str) -> AsyncIterator[Dict[str, Any]]:
    p = get_provider(provider_id)
    if not p:
        yield done({"ok": False, "message": "Unknown deploy target."})
        return
    config = {**_field_defaults(p.config_fields), **store.get_config(provider_id)}
    creds = await credentials.read(provider_id, p.credential_fields)
    required = p.credential_required if p.credential_required is not None else \
        [f["key"] for f in p.credential_fields if f.get("secret")]
    if required and not all(str(creds.get(k, "")).strip() for k in required):
        yield done({"ok": False, "message": "The key was discarded after deploy — re-enter it to tear down."})
        return
    record = store.get_deployment(provider_id)

    final: Dict[str, Any] = {"ok": False, "message": "Tear-down produced no result."}
    try:
        async for event in p.destroy(config, creds, record):
            if event.get("phase") == "done":
                final = event.get("result", final)
            yield event
    except Exception as e:
        logger.exception("destroy run crashed for %s", provider_id)
        yield done({"ok": False, "message": str(e) or "Tear-down crashed."})
        return

    if final.get("ok") and final.get("deleted"):
        store.clear_deployment(provider_id)


# ── Instance management (the Cloud VMs admin page) ──────────────────────────
#
# A SEPARATE surface from deploy/destroy: the Cloud VMs page manages EVERY server
# in the admin's cloud account, not just the one this app last created. It reuses
# the same vault credentials + saved config a deploy target already has; a target
# only takes part if it sets ``supports_instances`` and a cloud key is currently
# stored (the Deploy card auto-forgets the key after a deploy, so the page must
# degrade gracefully when none is present).


async def _has_key(p) -> bool:
    creds = await credentials.read(p.id, p.credential_fields)
    required = p.credential_required if p.credential_required is not None else \
        [f["key"] for f in p.credential_fields if f.get("secret")]
    return bool(required) and all(str(creds.get(k, "")).strip() for k in required)


def _connect_field_views(p, cfg: Dict[str, Any]) -> list:
    """The descriptors for the 'id' inputs the Cloud VMs sign-in panel shows
    (the provider's ``connect_config_keys``), each pre-filled with the saved
    value so the panel can also act as a 'change account' form. Non-secret, so
    echoing the value back is safe."""
    keys = list(getattr(p, "connect_config_keys", []) or [])
    by_key = {f["key"]: f for f in p.config_fields}
    views = []
    for k in keys:
        f = by_key.get(k, {"key": k, "label": k, "type": "text"})
        views.append({
            "key": k,
            "label": f.get("label", k),
            "type": f.get("type", "text"),
            "placeholder": f.get("placeholder", ""),
            "tip": f.get("tip", ""),
            "required": bool(f.get("required")),
            "value": cfg.get(k, ""),
        })
    return views


def _connect_ready(p, cfg: Dict[str, Any]) -> bool:
    """True when every ``connect_config_keys`` value the target needs is set."""
    return all(str(cfg.get(k, "")).strip() for k in (getattr(p, "connect_config_keys", []) or []))


async def manageable_providers() -> Dict[str, Any]:
    """The targets the Cloud VMs page can offer: every discovered provider that
    declares ``supports_instances``, with whether its cloud key is stored, the
    saved project/zone, and the sign-in panel's 'id' fields (so the page can show
    a clear connect form per target, and a picker only when more than one
    qualifies)."""
    out = []
    for p in list_providers():
        if not getattr(p, "supports_instances", False):
            continue
        ok_avail, reason = p.available()
        cfg = {**_field_defaults(p.config_fields), **store.get_config(p.id)}
        has_key = await _has_key(p)
        connect_ready = _connect_ready(p, cfg)
        out.append({
            "id": p.id,
            "display_name": p.display_name,
            "icon": p.icon,
            "available": ok_avail,
            "unavailable_reason": reason,
            "has_key": has_key,
            # Fully signed in: the cloud key is stored AND the id fields are set.
            "connected": bool(has_key and connect_ready),
            "credential_fields": [
                {k: v for k, v in f.items() if k != "secret"} | {"secret": bool(f.get("secret"))}
                for f in p.credential_fields
            ],
            # The 'id' inputs the sign-in panel renders (pre-filled, non-secret).
            "connect_fields": _connect_field_views(p, cfg),
            "project": cfg.get("project_id") or cfg.get("project") or "",
            "zone": cfg.get("zone") or "",
        })
    return {"providers": out}


async def list_instances(provider_id: str) -> Dict[str, Any]:
    """List the servers one target can see, or a clear reason it can't.

    When the target isn't fully signed in — no cloud key, or a missing 'id' such
    as the project — it returns ``needs_connect: True`` so the page shows its
    sign-in panel instead of a raw error."""
    p = get_provider(provider_id)
    if not p or not getattr(p, "supports_instances", False):
        return {"ok": False, "instances": [], "detail": "This target cannot list servers."}
    ok_avail, reason = p.available()
    if not ok_avail:
        return {"ok": False, "instances": [], "detail": reason or "This target is unavailable."}
    config = {**_field_defaults(p.config_fields), **store.get_config(provider_id)}
    if not await _has_key(p):
        return {"ok": False, "instances": [], "needs_key": True, "needs_connect": True,
                "detail": "Connect your cloud account to manage your servers — enter its id and key."}
    if not _connect_ready(p, config):
        return {"ok": False, "instances": [], "needs_connect": True,
                "detail": "Almost there — finish connecting by entering your cloud account id."}
    creds = await credentials.read(provider_id, p.credential_fields)
    try:
        return await p.list_instances(config, creds)
    except Exception as e:
        logger.exception("list_instances crashed for %s", provider_id)
        return {"ok": False, "instances": [], "detail": str(e) or "Listing servers failed."}


async def save_connection(provider_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """Save what the Cloud VMs sign-in panel collected: the cloud 'id' fields
    (e.g. project) into deploy.json, and the secret 'key' into the SAME encrypted
    vault entry the Deploy card uses. A BLANK secret leaves the stored key
    untouched (so 'change account' can change just the id). Non-secret/unknown
    keys are ignored. Returns ``{ok, detail}``."""
    p = get_provider(provider_id)
    if not p:
        return {"ok": False, "detail": "Unknown cloud target."}
    values = values or {}

    # Split the submitted values: config-field keys → deploy.json (merged over
    # whatever the Deploy card already saved, so machine size / repo survive);
    # credential-field keys → the vault.
    config_keys = {f["key"] for f in p.config_fields}
    cred_keys = {f["key"] for f in p.credential_fields}

    cfg_updates = {k: v for k, v in values.items() if k in config_keys}
    if cfg_updates:
        merged = {**store.get_config(provider_id), **cfg_updates}
        store.save_config(provider_id, merged)

    cred_updates = {k: v for k, v in values.items() if k in cred_keys}
    if cred_updates:
        ok = await credentials.save(provider_id, p.credential_fields, cred_updates)
        if not ok:
            return {"ok": False, "detail": "Could not save the cloud key."}

    return {"ok": True, "detail": "Cloud account connected."}


async def disconnect(provider_id: str, forget_config: bool = False) -> Dict[str, Any]:
    """Sign out / remove an account from the Cloud VMs page.

    Always deletes the stored cloud key from the vault. By default the non-secret
    'id' (project) is KEPT so reconnecting only needs the key again ("Sign out").
    With ``forget_config`` the account is fully removed: the connect 'id' keys are
    also cleared from deploy.json so it drops out of the page's account list
    ("Remove account"). Only the connect-config keys are cleared — other Deploy
    settings (machine size, repo) are left intact."""
    p = get_provider(provider_id)
    if not p:
        return {"ok": False, "detail": "Unknown cloud target."}
    await credentials.forget(provider_id)
    if forget_config:
        keys = list(getattr(p, "connect_config_keys", []) or [])
        if keys:
            cfg = dict(store.get_config(provider_id))
            for k in keys:
                cfg.pop(k, None)
            store.save_config(provider_id, cfg)
        return {"ok": True, "detail": "Account removed — its key and id were cleared."}
    return {"ok": True, "detail": "Signed out — the cloud key was removed from the vault."}


async def run_instance_action(provider_id: str, action: str, zone: str,
                              name: str) -> AsyncIterator[Dict[str, Any]]:
    """Start / Stop / Delete one server, streaming progress. When the acted-on
    server is the one the Deploy card recorded as 'this app', the deployment
    record is updated (stop/start → new state) or cleared (delete) so the Deploy
    card stays in sync."""
    p = get_provider(provider_id)
    if not p or not getattr(p, "supports_instances", False):
        yield done({"ok": False, "message": "This target cannot manage servers."})
        return
    if not await _has_key(p):
        yield done({"ok": False, "message": "No cloud key is stored — add your cloud key to manage servers."})
        return
    config = {**_field_defaults(p.config_fields), **store.get_config(provider_id)}
    creds = await credentials.read(provider_id, p.credential_fields)
    instance = {"name": name, "zone": zone}

    final: Dict[str, Any] = {"ok": False, "message": "No result."}
    try:
        async for event in p.instance_action(config, creds, action, instance):
            if event.get("phase") == "done":
                final = event.get("result", final)
            yield event
    except Exception as e:
        logger.exception("instance action crashed for %s", provider_id)
        yield done({"ok": False, "message": str(e) or "Action failed."})
        return

    # Keep the Deploy card's "last server" line honest when we touched it.
    if final.get("ok"):
        rec = store.get_deployment(provider_id)
        if rec.get("server") and rec.get("server") == name:
            if final.get("deleted") or (action or "").lower() == "delete":
                store.clear_deployment(provider_id)
            elif final.get("new_state"):
                rec["state"] = final["new_state"]
                store.set_deployment(provider_id, rec)

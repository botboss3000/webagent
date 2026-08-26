"""Secret-free user-facing experience-tier capability and BYO model APIs."""

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.identity import request_user_id
from app.entitlements.service import resolve_capabilities

router = APIRouter(prefix="/api/v1/entitlements", tags=["entitlements"])


@router.get("/me")
async def my_capabilities(request: Request):
    uid = request_user_id(request)
    return await resolve_capabilities(uid or "__anonymous__")


class ByoModelsRequest(BaseModel):
    entries: list[dict[str, Any]] = Field(default_factory=list)
    default_entry_id: str = ""


async def _registered_caller(request: Request) -> str:
    uid = request_user_id(request)
    if not uid or str(uid).startswith("anon_"):
        raise HTTPException(status_code=401, detail={"code": "authentication_required"})
    return uid


@router.get("/me/byo-models")
async def get_my_byo_models(request: Request):
    uid = await _registered_caller(request)
    from app.admin.settings import _load_own_llm_config, _public_provider_config

    config = await _load_own_llm_config(uid) or {}
    public = _public_provider_config(config)
    return {
        "entries": public.get("multi_providers") or [],
        "default_entry_id": config.get("_user_default_entry_id") or "",
        "credential_state": "configured" if any(
            entry.get("credential_configured") for entry in public.get("multi_providers") or []
        ) else "missing",
    }


@router.put("/me/byo-models")
async def set_my_byo_models(body: ByoModelsRequest, request: Request):
    uid = await _registered_caller(request)
    from app.db import get_db
    from app.admin.settings import _load_own_llm_config, _persist_llm_config
    from app.entitlements.rosters import RosterError, normalize_entries, validate_roster_entries

    db = get_db()
    capabilities = await resolve_capabilities(uid, db=db, use_cache=False)
    model_policy = capabilities.get("models") or {}
    if not model_policy.get("allow_byo"):
        raise HTTPException(status_code=403, detail={"code": "byo_not_allowed"})
    maximum = model_policy.get("max_byo_entries")
    if maximum is not None and len(body.entries) > int(maximum):
        raise HTTPException(status_code=409, detail={
            "code": "quota_exceeded", "limit": "max_byo_entries", "maximum": int(maximum),
        })
    # Roster normalization deliberately removes secrets for platform metadata.
    # Preserve each caller-owned credential separately and splice it back only
    # into the vault-bound config after stable IDs have been assigned.
    try:
        validated_entries = validate_roster_entries(
            body.entries, allow_credentials=True, require_provider_model=True,
            untrusted_urls=True,
        )
    except RosterError as exc:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_model_entry", "message": str(exc),
        }) from exc
    incoming_credentials = [
        str(entry.get("api_key") or "") if isinstance(entry, dict) else ""
        for entry in validated_entries
    ]
    entries = normalize_entries(f"user-byo:{uid}", validated_entries)
    existing = await _load_own_llm_config(uid) or {}
    existing_by_id = {
        str(entry.get("entry_id") or ""): entry
        for entry in existing.get("multi_providers") or [] if isinstance(entry, dict)
    }
    features = capabilities.get("features") or {}
    for index, entry in enumerate(entries):
        if not entry.get("provider") or not entry.get("model"):
            raise HTTPException(status_code=422, detail="Each BYO model needs provider and model.")
        if entry.get("image_out_capable") and not features.get("image_generation"):
            raise HTTPException(status_code=403, detail={"code": "upgrade_required", "feature": "image_generation"})
        if entry.get("voice_capable") and not features.get("voice_llm"):
            raise HTTPException(status_code=403, detail={"code": "upgrade_required", "feature": "voice_llm"})
        if incoming_credentials[index]:
            entry["api_key"] = incoming_credentials[index]
        elif entry.get("entry_id") in existing_by_id:
            entry["api_key"] = existing_by_id[entry["entry_id"]].get("api_key", "")
    if entries and not all(entry.get("api_key") for entry in entries):
        raise HTTPException(status_code=422, detail="Each BYO model needs a credential.")
    default = next((entry for entry in entries if entry["entry_id"] == body.default_entry_id), None)
    if body.default_entry_id and default is None:
        raise HTTPException(status_code=422, detail={
            "code": "invalid_default_entry", "entry_id": body.default_entry_id,
        })
    default = default or (entries[0] if entries else {})
    config = {
        "provider": default.get("provider", ""), "base_url": default.get("base_url", ""),
        "api_key": default.get("api_key", ""), "model": default.get("model", ""),
        "multi_providers": entries, "providers": {},
        "_user_default_entry_id": default.get("entry_id", ""),
    }
    await _persist_llm_config(uid, config)
    return {"status": "ok", "count": len(entries), "default_entry_id": default.get("entry_id", "")}


@router.delete("/me/byo-models")
async def delete_my_byo_models(request: Request):
    uid = await _registered_caller(request)
    from app.admin.settings import _persist_llm_config, DEFAULT_PROVIDER

    await _persist_llm_config(uid, dict(DEFAULT_PROVIDER))
    return {"status": "deleted"}

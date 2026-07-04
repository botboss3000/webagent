"""
Per-agent external data source API.

Two entity types:
  * data_sources                — registered external sources, owned by a user.
  * agent_data_sources          — link table; an agent attaches a source.

Endpoints
---------
GET    /api/v1/data-sources                          — list user's sources
POST   /api/v1/data-sources                          — create
GET    /api/v1/data-sources/{ds_id}                  — fetch one
PUT    /api/v1/data-sources/{ds_id}                  — update fields
DELETE /api/v1/data-sources/{ds_id}                  — delete (cascades attachments)
POST   /api/v1/data-sources/{ds_id}/test             — probe connection
POST   /api/v1/data-sources/{ds_id}/introspect       — refresh schema cache
POST   /api/v1/data-sources/{ds_id}/ingest           — re-ingest doc_store
GET    /api/v1/data-sources/types                    — list connector types

GET    /api/v1/agents/{agent_id}/data-sources                          — list attachments
POST   /api/v1/agents/{agent_id}/data-sources                          — attach
PUT    /api/v1/agents/{agent_id}/data-sources/{ds_id}                  — update attachment
DELETE /api/v1/agents/{agent_id}/data-sources/{ds_id}                  — detach
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.auth.identity import assert_caller_is
from app.connectors import CONNECTOR_REGISTRY, get_connector
from app.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["data-sources"])


# ── Models ────────────────────────────────────────────────────────────────────

class CreateDataSourceRequest(BaseModel):
    user_id: str
    name: str
    type: str
    config: Optional[Dict[str, Any]] = None
    auth_element_id: Optional[str] = None
    safety_policy: Optional[Dict[str, Any]] = None


class UpdateDataSourceRequest(BaseModel):
    user_id: str
    name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    auth_element_id: Optional[str] = None
    safety_policy: Optional[Dict[str, Any]] = None
    status: Optional[str] = None


class AttachRequest(BaseModel):
    user_id: str
    data_source_id: str
    tool_alias: Optional[str] = None
    inject_schema_in_prompt: Optional[bool] = True


class UpdateAttachmentRequest(BaseModel):
    user_id: str
    tool_alias: Optional[str] = None
    enabled: Optional[bool] = None
    inject_schema_in_prompt: Optional[bool] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _resolve_auth(auth_element_id: Optional[str]) -> Optional[dict]:
    """Sync resolver suitable for passing into connector methods.

    Connectors call this as a regular callable; we wrap a one-shot lookup.
    """
    if not auth_element_id:
        return None
    db = get_db()
    try:
        client = db.get_raw_client()
        res = client.table("auth_elements").select("*").eq("id", auth_element_id).limit(1).execute()
        if res.data:
            return res.data[0]
    except Exception as e:
        logger.warning("auth_element lookup failed: %s", e)
    return None


def _make_resolver():
    """Return a synchronous-looking resolver that wraps the async lookup.

    Connectors are written to call `auth_resolver(id)` as a plain function.
    """
    import asyncio

    def _resolver(aid: Optional[str]) -> Optional[dict]:
        if not aid:
            return None
        try:
            return asyncio.get_event_loop().run_until_complete(_resolve_auth(aid))
        except RuntimeError:
            # Nested loop — fallback to creating a fresh one
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(_resolve_auth(aid))
            finally:
                loop.close()

    return _resolver


async def _auth_lookup(auth_element_id: Optional[str]) -> Optional[dict]:
    return await _resolve_auth(auth_element_id)


async def _require_data_source(ds_id: str, user_id: str) -> dict:
    db = get_db()
    ds = await db.data_source_get(ds_id, user_id)
    if not ds:
        raise HTTPException(404, f"data source {ds_id} not found")
    return ds


def _validate_type(t: str):
    if t not in CONNECTOR_REGISTRY:
        raise HTTPException(400, f"unknown data source type: {t}")


# ── Type catalog ──────────────────────────────────────────────────────────────

@router.get("/data-sources/types")
async def list_data_source_types(request: Request):
    await assert_caller_is(request, None)
    out = []
    for type_name, cls in CONNECTOR_REGISTRY.items():
        out.append({
            "type": type_name,
            "display_name": cls.display_name,
            "requires_credential": cls.requires_credential,
        })
    return {"types": out}


# ── Data source CRUD ──────────────────────────────────────────────────────────

@router.get("/data-sources")
async def list_data_sources(request: Request, user_id: str = Query(...)):
    user_id = await assert_caller_is(request, user_id)
    db = get_db()
    rows = await db.data_source_list(user_id)
    return {"data_sources": rows}


@router.post("/data-sources")
async def create_data_source(request: Request, req: CreateDataSourceRequest):
    await assert_caller_is(request, req.user_id)
    _validate_type(req.type)
    db = get_db()
    row = await db.data_source_create(
        user_id=req.user_id,
        name=req.name,
        type=req.type,
        config=req.config or {},
        auth_element_id=req.auth_element_id,
        safety_policy=req.safety_policy or {},
    )
    return {"data_source": row}


@router.get("/data-sources/{ds_id}")
async def get_data_source(ds_id: str, request: Request, user_id: str = Query(...)):
    user_id = await assert_caller_is(request, user_id)
    ds = await _require_data_source(ds_id, user_id)
    return {"data_source": ds}


@router.put("/data-sources/{ds_id}")
async def update_data_source(ds_id: str, request: Request, req: UpdateDataSourceRequest):
    await assert_caller_is(request, req.user_id)
    await _require_data_source(ds_id, req.user_id)
    db = get_db()
    fields = {k: v for k, v in req.dict().items() if k != "user_id" and v is not None}
    row = await db.data_source_update(ds_id, req.user_id, **fields)
    return {"data_source": row}


@router.delete("/data-sources/{ds_id}")
async def delete_data_source(ds_id: str, request: Request, user_id: str = Query(...)):
    user_id = await assert_caller_is(request, user_id)
    db = get_db()
    ok = await db.data_source_delete(ds_id, user_id)
    if not ok:
        raise HTTPException(404, "not found")
    return {"deleted": True}


# ── Test / introspect / ingest ────────────────────────────────────────────────

@router.post("/data-sources/{ds_id}/test")
async def test_data_source(ds_id: str, request: Request, user_id: str = Query(...)):
    user_id = await assert_caller_is(request, user_id)
    ds = await _require_data_source(ds_id, user_id)
    connector = get_connector(ds["type"])
    auth = await _auth_lookup(ds.get("auth_element_id"))

    def resolver(_aid):
        return auth

    result = await connector.test_connection(ds, resolver)
    db = get_db()
    from datetime import datetime, timezone
    await db.data_source_update(
        ds_id, user_id,
        status="active" if result.get("ok") else "error",
        last_test_message=result.get("message", ""),
        last_tested_at=datetime.now(timezone.utc).isoformat(),
    )
    return result


@router.post("/data-sources/{ds_id}/introspect")
async def introspect_data_source(ds_id: str, request: Request, user_id: str = Query(...)):
    user_id = await assert_caller_is(request, user_id)
    ds = await _require_data_source(ds_id, user_id)
    connector = get_connector(ds["type"])
    auth = await _auth_lookup(ds.get("auth_element_id"))

    def resolver(_aid):
        return auth

    try:
        schema = await connector.introspect(ds, resolver)
    except Exception as e:
        logger.warning("introspect failed for %s: %s", ds_id, e)
        raise HTTPException(500, f"introspect failed: {e}")
    db = get_db()
    from datetime import datetime, timezone
    await db.data_source_update(
        ds_id, user_id,
        schema_cache=schema,
        last_introspected_at=datetime.now(timezone.utc).isoformat(),
    )
    return {"schema_cache": schema}


@router.post("/data-sources/{ds_id}/ingest")
async def ingest_data_source(ds_id: str, request: Request, user_id: str = Query(...)):
    user_id = await assert_caller_is(request, user_id)
    ds = await _require_data_source(ds_id, user_id)
    if ds["type"] != "doc_store":
        raise HTTPException(400, "ingest only supported for doc_store sources")
    connector = get_connector(ds["type"])
    summary = await connector.ingest(ds)  # type: ignore[attr-defined]
    # Refresh introspection so file/chunk count reflects new state.
    try:
        schema = await connector.introspect(ds, lambda _a: None)
        db = get_db()
        from datetime import datetime, timezone
        await db.data_source_update(
            ds_id, user_id,
            schema_cache=schema,
            last_introspected_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception:
        pass
    return summary


# ── Agent attachments ─────────────────────────────────────────────────────────

@router.get("/agents/{agent_id}/data-sources")
async def list_agent_attachments(agent_id: str, request: Request, user_id: str = Query(...)):
    user_id = await assert_caller_is(request, user_id)
    db = get_db()
    rows = await db.agent_data_source_list(agent_id, enabled_only=False)
    return {"attachments": rows}


@router.post("/agents/{agent_id}/data-sources")
async def attach_data_source(agent_id: str, request: Request, req: AttachRequest):
    await assert_caller_is(request, req.user_id)
    db = get_db()
    # Sanity: the source must exist (caller owns it or has access).
    ds = await db.data_source_get(req.data_source_id, req.user_id)
    if not ds:
        raise HTTPException(404, "data source not found")
    row = await db.agent_data_source_attach(
        agent_id=agent_id,
        data_source_id=req.data_source_id,
        tool_alias=req.tool_alias,
        inject_schema_in_prompt=bool(req.inject_schema_in_prompt),
    )
    return {"attachment": row}


@router.put("/agents/{agent_id}/data-sources/{ds_id}")
async def update_attachment(agent_id: str, request: Request, ds_id: str, req: UpdateAttachmentRequest):
    await assert_caller_is(request, req.user_id)
    db = get_db()
    fields = {k: v for k, v in req.dict().items() if k != "user_id" and v is not None}
    row = await db.agent_data_source_update(agent_id, ds_id, **fields)
    if not row:
        raise HTTPException(404, "attachment not found")
    return {"attachment": row}


@router.delete("/agents/{agent_id}/data-sources/{ds_id}")
async def detach_data_source(agent_id: str, request: Request, ds_id: str, user_id: str = Query(...)):
    user_id = await assert_caller_is(request, user_id)
    db = get_db()
    ok = await db.agent_data_source_detach(agent_id, ds_id)
    if not ok:
        raise HTTPException(404, "attachment not found")
    return {"detached": True}

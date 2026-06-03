"""
Render recorder API — browser intake + admin read access.

The render recorder (app/agent/render_recorder.py) is the durable sink for what
the *browser* renders and feels: HTML snapshots, DOM-mutation deltas, lag /
long-task metrics, JS errors, console warnings and failed network calls shipped
by ui/js/recorder.js. Every row carries `session_seq`, so a render moment joins
exactly to the interaction / diagnostics rows beside it.

    GET    /api/v1/recordings/config  — capture knobs the browser reads (light auth)
    POST   /api/v1/recordings         — browser ships a batch of records (light auth)
    GET    /api/v1/recordings         — filtered list of recent records (admin)
    GET    /api/v1/recordings/one     — one record WITH its html payload (admin)
    GET    /api/v1/recordings/stats   — row/byte stats + recorder health (admin)
    DELETE /api/v1/recordings         — clear records (all, older-than, or by filter) (admin)

Intake is intentionally NOT admin-gated — recordings come from the browser of
whoever is using the app (often an anonymous visitor), not from an operator. It
is guarded instead by the master ``render_recording_enabled`` flag (off unless an
admin turns it on) plus hard size caps. The user_id is derived server-side from
the caller's token when present, so it can't be spoofed.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel

from app.api.db_viewer import require_admin
from app.agent.render_recorder import get_render_recorder
from app.auth.jwt import decode_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/recordings", tags=["recordings"])


def _split_csv(v: Optional[str]) -> Optional[list]:
    if not v:
        return None
    parts = [p.strip() for p in v.split(",") if p.strip()]
    return parts or None


def _user_from_token(authorization: str) -> Optional[str]:
    """Best-effort user_id from a Bearer token (None for anonymous/invalid)."""
    if authorization and authorization.startswith("Bearer "):
        try:
            payload = decode_token(authorization[7:])
            if payload:
                return payload.get("user_id") or payload.get("sub")
        except Exception:
            pass
    return None


class RecordingBatch(BaseModel):
    records: List[dict] = []


@router.get("/config")
async def recordings_config(authorization: str = Header("")):
    """Tell the browser whether to record and with what capture knobs.

    Cheap + light-auth so it can be polled on page load. When recording is off,
    ``enabled`` is False and the browser stays completely passive."""
    rec = get_render_recorder()
    enabled = rec.enabled
    return {"enabled": enabled, "config": rec.client_config() if enabled else {}}


@router.post("")
async def ingest_recordings(
    batch: RecordingBatch,
    authorization: str = Header(""),
):
    """Accept a batch of browser render records. No-op when recording is off."""
    rec = get_render_recorder()
    result = rec.ingest(batch.records, user_id=_user_from_token(authorization))
    return {"status": "ok", **result}


@router.get("")
async def list_recordings(
    kinds: Optional[str] = Query(None, description="Comma list: snapshot,mutation,lag,js_error,console,network,nav,meta"),
    levels: Optional[str] = Query(None, description="Comma list: info,warning,error"),
    session_id: Optional[str] = Query(None),
    client_id: Optional[str] = Query(None),
    session_seq: Optional[int] = Query(None, description="Exact correlation key match"),
    since_minutes: Optional[float] = Query(None),
    search: Optional[str] = Query(None, description="Substring match on label/detail/url"),
    limit: int = Query(200, ge=1, le=2000),
    _auth: dict = Depends(require_admin),
):
    """Return recent render records, newest first. The (large) html payload is
    omitted here — fetch it per-row via /one."""
    records = await get_render_recorder().query(
        kinds=_split_csv(kinds),
        levels=_split_csv(levels),
        session_id=session_id,
        client_id=client_id,
        session_seq=session_seq,
        since_minutes=since_minutes,
        search=search,
        include_html=False,
        limit=limit,
    )
    return {"records": records, "count": len(records)}


@router.get("/one")
async def get_recording(
    id: str = Query(..., description="Recording id"),
    _auth: dict = Depends(require_admin),
):
    """Return a single record including its full html payload."""
    rows = await get_render_recorder().query(rec_id=id, include_html=True, limit=1)
    return rows[0] if rows else {"detail": "not found"}


@router.get("/enabled")
async def get_recordings_enabled(_auth: dict = Depends(require_admin)):
    """Current on/off state of the render recorder (for operator toggles)."""
    return {"enabled": get_render_recorder().enabled}


class EnabledBody(BaseModel):
    enabled: bool


@router.post("/enabled")
async def set_recordings_enabled(body: EnabledBody, _auth: dict = Depends(require_admin)):
    """Flip the render recorder on/off by writing render_recording_enabled to
    app-settings.json. The recorder re-reads the flag live, so this takes effect
    immediately (browsers pick it up on their next ~1-min /config poll). Used by
    both the web Admin Tools toggle and the webagent-tui."""
    from app.admin.settings import _load_app_settings, _save_app_settings
    s = _load_app_settings()
    s["render_recording_enabled"] = bool(body.enabled)
    _save_app_settings(s)
    return {"enabled": get_render_recorder().enabled}


@router.get("/stats")
async def recordings_stats(_auth: dict = Depends(require_admin)):
    """Recorder health: enabled flag, pending writes, durable row/byte counts."""
    return await get_render_recorder().stats()


@router.delete("")
async def clear_recordings(
    older_than_minutes: Optional[float] = Query(None, description="Clear records older than N minutes (omit = all)"),
    kinds: Optional[str] = Query(None, description="Comma list — only clear these kinds"),
    session_id: Optional[str] = Query(None, description="Only clear this session"),
    search: Optional[str] = Query(None, description="Only clear records matching this substring"),
    _auth: dict = Depends(require_admin),
):
    """Clear render recordings. No scope → clears everything."""
    result = await get_render_recorder().clear(
        older_than_minutes=older_than_minutes,
        kinds=_split_csv(kinds),
        session_id=session_id,
        search=search,
    )
    return {"status": "ok", **result}

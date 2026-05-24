"""
Admin endpoints for database mode switching + agent template lifecycle.

- Cloud/Local toggle: /admin/db/mode
- Stats:              /admin/db/mode (GET, returns table row counts)
- Templates panel:    /admin/db/templates (GET, POST seed, POST seed-force)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import get_mode, set_db_mode, get_db_stats, get_db
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/db", tags=["admin-db"])


class SetModeRequest(BaseModel):
    mode: str  # "cloud" or "local"


class ModeStatusResponse(BaseModel):
    mode: str
    backend: str
    tables: dict
    db_path: str = ""


@router.get("/mode", response_model=ModeStatusResponse)
async def get_current_mode():
    """
    Get the current database mode and storage statistics.
    
    Returns:
        ModeStatusResponse with mode, backend type, table row counts
    """
    try:
        stats = await get_db_stats()
        return ModeStatusResponse(**stats)
    except Exception as e:
        logger.error(f"Error getting db mode status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/mode")
async def set_mode(request: SetModeRequest):
    """
    Switch the database mode.

    Body:
        {"mode": "local"} or {"mode": "cloud"}

    The switch takes effect immediately. The next request will use the new backend.
    """
    try:
        set_db_mode(request.mode)
        stats = await get_db_stats()
        return {
            "message": f"Switched to {request.mode} mode",
            "status": stats,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error switching db mode: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Agent Prompt Templates panel ────────────────────────────────────────────
#
# Provides admin-side visibility + control over the JSON → agent_templates +
# agent_prompt_templates seed pipeline.
#
# Read:  GET  /admin/db/templates           — list templates + per-slot version/source
# Write: POST /admin/db/templates/seed      — re-seed (manifest-gated, respects admin)
# Write: POST /admin/db/templates/seed-force— re-seed overriding admin edits (destructive)


@router.get("/templates")
async def list_template_state():
    """
    Return per-template metadata: id, slot count, version range, admin-edited slot count.

    Also returns the currently stored manifest hash so the UI can show whether
    the DB is in sync with the on-disk JSON files. Frontend can compare against
    a freshly-computed hash from the JSON manifest endpoint (below) to decide
    whether to nudge the admin to re-seed.
    """
    try:
        from app.context.md_seeder import compute_agent_manifest_hash
        db = get_db()
        raw = db.get_raw_client()

        # Pull template config rows.
        tpl_res = raw.table("agent_templates").select("id, name, description, updated_at").execute()
        templates = tpl_res.data or []

        # Pull slot rows (grouped client-side; small N so it's fine).
        try:
            slot_res = raw.table("agent_prompt_templates").select(
                "template_id, slot_name, version, source, updated_at, updated_by"
            ).execute()
            slot_rows = slot_res.data or []
        except Exception:
            slot_rows = []

        by_tpl: dict = {}
        for r in slot_rows:
            by_tpl.setdefault(r["template_id"], []).append(r)

        out_templates = []
        for tpl in templates:
            tid = tpl["id"]
            slots = by_tpl.get(tid, [])
            versions = [int(s.get("version") or 0) for s in slots]
            admin_n = sum(1 for s in slots if s.get("source") == "admin")
            out_templates.append({
                "id": tid,
                "name": tpl.get("name") or tid,
                "description": tpl.get("description") or "",
                "slot_count": len(slots),
                "min_version": min(versions) if versions else 0,
                "max_version": max(versions) if versions else 0,
                "admin_edited_slots": admin_n,
                "updated_at": tpl.get("updated_at"),
                "slots": [
                    {
                        "slot_name": s["slot_name"],
                        "version": s.get("version"),
                        "source": s.get("source"),
                        "updated_at": s.get("updated_at"),
                        "updated_by": s.get("updated_by"),
                    }
                    for s in sorted(slots, key=lambda r: r["slot_name"])
                ],
            })

        # Stored hash from app_meta (may be missing on fresh DBs).
        stored_hash = ""
        try:
            meta_res = raw.table("app_meta").select("value").eq(
                "key", "last_agent_manifest_hash"
            ).limit(1).execute()
            if meta_res.data:
                stored_hash = meta_res.data[0].get("value") or ""
        except Exception:
            pass

        current_hash = compute_agent_manifest_hash()

        return {
            "templates": sorted(out_templates, key=lambda t: t["id"]),
            "stored_manifest_hash": stored_hash,
            "current_manifest_hash": current_hash,
            "in_sync": (stored_hash == current_hash) and bool(stored_hash),
        }
    except Exception as e:
        logger.error("Error listing template state: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/templates/seed")
async def seed_templates():
    """
    Run the non-destructive JSON → DB seeder.

    Manifest-gated short-circuit fires when the JSON files haven't changed.
    Slot rows whose source = 'admin' are skipped (admin edits sacred).
    Returns the seeder summary dict.
    """
    try:
        db = get_db()
        summary = await db.seed_agent_templates(force=False)
        return summary
    except Exception as e:
        logger.error("Error seeding agent templates: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/templates/seed-force")
async def seed_templates_force():
    """
    Run the seeder with force=True.

    OVERRIDES the manifest short-circuit AND overwrites admin-edited slot rows
    with the JSON content. Use only when JSON should be re-asserted as truth
    (e.g. admin edits were wrong, or DB drifted from intended baseline).
    """
    try:
        db = get_db()
        summary = await db.seed_agent_templates(force=True)
        return summary
    except Exception as e:
        logger.error("Error force-seeding agent templates: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

"""
Ability Delete API — delete an ability file from plugins/abilities/.

Protected: only experimental/beta abilities can be deleted via this endpoint.
Stable abilities require server owner to delete the file directly.
"""
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_ABILITIES_DIR = Path(__file__).resolve().parents[2] / "plugins" / "abilities"


class DeleteAbilityRequest(BaseModel):
    """Admin confirmation to delete an ability."""
    confirmed: bool = True


router = APIRouter(prefix="/api/v1/abilities")


@router.get("/{ability_id}/delete-info")
async def get_delete_info(ability_id: str):
    """Return info about what would be deleted — files, agents using it, status.
    Non-destructive — for showing the admin what they're about to delete.
    """
    from app.abilities import all_raw

    abilities = all_raw()
    feat = abilities.get(ability_id)
    if not feat:
        raise HTTPException(status_code=404, detail=f"Ability '{ability_id}' not found")

    status = feat.get("status", "unknown")

    # Find all files belonging to this ability
    files_to_delete = []
    if _ABILITIES_DIR.is_dir():
        for fpath in sorted(_ABILITIES_DIR.iterdir()):
            if fpath.name.startswith("_"):
                continue
            stem = _resolve_stem(fpath)
            if stem == ability_id:
                files_to_delete.append({
                    "path": str(fpath.relative_to(_ABILITIES_DIR.parent.parent)),
                    "size_bytes": fpath.stat().st_size,
                })

    # Find which agents have this ability enabled
    agents_using = []
    try:
        from app.db import get_db
        db = get_db()
        conn = None
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
        elif hasattr(db, "_conn"):
            conn = db._conn
        if conn:
            rows = conn.execute(
                "SELECT * FROM agent_connections WHERE connection_type = ?",
                (ability_id,),
            ).fetchall()
            agents_using = [
                {"agent_id": r["agent_id"], "enabled": bool(r.get("enabled", 0))}
                for r in rows if r.get("section") == "ability"
            ]
        elif hasattr(db, "get_all_connections_by_type"):
            rows = await db.get_all_connections_by_type(ability_id)
            agents_using = [
                {"agent_id": r["agent_id"], "enabled": bool(r.get("enabled", 0))}
                for r in rows if r.get("section") == "ability"
            ]
    except Exception as e:
        logger.warning("Could not check agent usage for %s: %s", ability_id, e)

    return {
        "status": "ok",
        "ability": {
            "id": ability_id,
            "display_name": feat.get("display_name", ability_id),
            "status": status,
            "files": files_to_delete,
            "agents_using": agents_using,
            "agent_count": len(agents_using),
        },
        "deletable": status in ("experimental", "beta"),
        "warning": (
            "This will delete the ability file and disable it for all agents. "
            "The server needs to restart for the changes to fully take effect."
            if status in ("experimental", "beta")
            else "Stable abilities cannot be deleted through the UI. Delete the file manually from plugins/abilities/."
        ),
    }


@router.delete("/{ability_id}")
async def delete_ability(ability_id: str, req: Optional[DeleteAbilityRequest] = None):
    """Delete an ability file from the filesystem.

    Only `experimental` and `beta` abilities can be deleted through the API.
    This will:
    1. Find and delete all files belonging to this ability (.py, .skill.md, .json)
    2. Disable the ability on all agents that have it enabled
    3. Clear the server-side ability cache
    """
    confirmed = req.confirmed if req else True
    if not confirmed:
        raise HTTPException(status_code=400, detail="Deletion not confirmed")

    from app.abilities import all_raw

    abilities = all_raw()
    feat = abilities.get(ability_id)
    if not feat:
        raise HTTPException(status_code=404, detail=f"Ability '{ability_id}' not found")

    status = feat.get("status", "unknown")
    if status == "stable":
        raise HTTPException(
            status_code=403,
            detail=f"Cannot delete stable ability '{ability_id}' through the API. "
                   f"Delete the file manually from plugins/abilities/.",
        )

    # 1. Delete files
    deleted_files = []
    if _ABILITIES_DIR.is_dir():
        for fpath in list(_ABILITIES_DIR.iterdir()):
            if fpath.name.startswith("_"):
                continue
            stem = _resolve_stem(fpath)
            if stem == ability_id:
                try:
                    fpath.unlink()
                    deleted_files.append(str(fpath.name))
                    logger.info("Deleted ability file: %s", fpath)
                except Exception as e:
                    logger.error("Failed to delete %s: %s", fpath, e)

    if not deleted_files:
        raise HTTPException(status_code=404, detail=f"No files found for ability '{ability_id}'")

    # 2. Disable the ability on all agents
    disabled_count = 0
    try:
        from app.db import get_db
        db = get_db()
        if hasattr(db, "delete_all_ability_connections"):
            disabled_count = await db.delete_all_ability_connections(ability_id)
            logger.info("Disabled ability '%s' on %d agents", ability_id, disabled_count)
    except Exception as e:
        logger.warning("Could not disable ability connections for %s: %s", ability_id, e)

    # 3. Clear the ability cache so the next catalog read reflects the deletion
    try:
        from app.abilities import reload
        reload()
        logger.info("Ability cache cleared after deleting '%s'", ability_id)
    except Exception as e:
        logger.warning("Could not reload ability cache: %s", e)

    return {
        "status": "ok",
        "message": f"Ability '{ability_id}' deleted.",
        "deleted_files": deleted_files,
        "disabled_on_agents": disabled_count,
        "note": "The server should be restarted for changes to fully take effect.",
    }


def _resolve_stem(fpath: Path) -> str:
    """Get the ability stem from a file path — strips .py, .skill.md, .json, .bak."""
    name = fpath.name
    for suffix in (".skill.md", ".py", ".json"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if name.endswith(".bak"):
        name = name[:-4]
    return name
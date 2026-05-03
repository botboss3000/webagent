"""
Admin endpoints for database mode switching.

Lets the admin toggle between Cloud (Supabase) and Local (SQLite) modes,
and retrieve database stats. Used by the Cloud/Local toggle in the terminal UI.
"""

from fastapi import APIRouter, HTTPException
from msgspec import Struct
from app.db import get_mode, set_db_mode, get_db_stats
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/db", tags=["admin-db"])


class SetModeRequest(Struct):
    mode: str  # "cloud" or "local"


class ModeStatusResponse(Struct):
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

"""
Admin endpoints for optimizer configuration and manual runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel

from app.optimizer.config import load_config, save_config
from app.optimizer.runner import run_optimizer_async

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/settings", tags=["admin"])


class OptimizerConfigModel(BaseModel):
    mode: Optional[str] = None
    intensity: Optional[int] = None
    user_feedback: Optional[str] = None
    sessions: Optional[Dict[str, bool]] = None
    schedule: Optional[Dict[str, Any]] = None
    models: Optional[Dict[str, Optional[str]]] = None
    target_metrics: Optional[list] = None
    app_wide: Optional[Dict[str, Any]] = None
    per_user: Optional[Dict[str, Any]] = None
    notifications: Optional[Dict[str, Any]] = None


@router.get("/optimizer")
async def get_optimizer_config():
    """Get current optimizer configuration."""
    cfg = load_config()
    return cfg


@router.post("/optimizer")
async def update_optimizer_config(body: OptimizerConfigModel):
    """Update optimizer configuration. Merges with existing values."""
    current = load_config()
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    current.update(updates)
    save_config(current)
    return {"status": "saved", "config": current}


@router.get("/optimizer/runs")
async def get_optimizer_runs(limit: int = Query(20, ge=1, le=100)):
    """Get recent optimizer runs."""
    try:
        from app.db import get_db
        db = get_db()
        raw = getattr(db, '_get_conn', None)
        if raw:
            conn = raw()
            rows = conn.execute(
                """SELECT * FROM optimizer_runs
                   ORDER BY started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
    except Exception as e:
        logger.error("Failed to fetch optimizer runs: %s", e)
    return []


@router.post("/optimizer/run")
async def trigger_optimizer_run(
    user_id: str = Query("_system"),
    session_id: str = Query("manual"),
):
    """Manually trigger an optimizer run."""
    try:
        # Load provider config so LLM env vars are set
        from app.admin.settings import load_provider_for_user
        await load_provider_for_user(user_id)
        
        opt_session = await run_optimizer_async(
            user_id=user_id,
            session_id=session_id,
        )
        return {"status": "completed", "optimizer_session_id": opt_session}
    except Exception as e:
        logger.error("Manual optimizer run failed: %s", e)
        return {"status": "error", "message": str(e)}

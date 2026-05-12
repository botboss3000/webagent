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
    max_iterations: Optional[int] = None
    trials: Optional[Dict[str, Any]] = None
    app_wide: Optional[Dict[str, Any]] = None
    per_user: Optional[Dict[str, Any]] = None
    notifications: Optional[Dict[str, Any]] = None


@router.get("/optimizer")
async def get_optimizer_config():
    """Get current optimizer configuration."""
    cfg = load_config()
    return cfg


@router.post("/optimizer")
async def update_optimizer_config(body: OptimizerConfigModel, user_id: str = Header(default="test-user-1", alias="x-user-id")):
    """Update optimizer configuration. Merges with existing values."""
    current = load_config()
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    current.update(updates)
    if body.max_iterations is not None:
        try:
            from app.db import get_db
            db = get_db()
            conn = getattr(db, "_get_conn")()
            agent = conn.execute("SELECT id, metadata FROM agents WHERE user_id=? LIMIT 1", (user_id,)).fetchone()
            if agent:
                meta = json.loads(agent[1] or "{}")
                meta["optimizer_max_iterations"] = body.max_iterations
                conn.execute("UPDATE agents SET metadata=? WHERE id=?", (json.dumps(meta), agent[0]))
                conn.commit()
            conn.close()
        except Exception as e:
            logger.error("Failed to update agent max_iterations: %s", e)
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
    feedback: str = Query(""),
):
    """Start an interactive optimizer session. Creates chat session with Planner agent."""
    try:
        from app.admin.settings import load_provider_for_user
        import uuid, json, sqlite3
        from app.db import get_db
        
        await load_provider_for_user(user_id)
        
        # Create optimizer session ID
        opt_sid = f"optimizer-{user_id[:8]}-{str(uuid.uuid4())[:8]}"
        
        db = get_db()
        raw = getattr(db, '_get_conn', None)
        conn = raw()
        
        # Ensure session exists with planner role
        conn.execute("INSERT OR IGNORE INTO sessions (id,user_id,title,metadata,created_at,updated_at) VALUES (?,?,?,?,datetime('now'),datetime('now'))",
                     (opt_sid, user_id, f"Optimizer - {opt_sid[:12]}", '{"opt_role": "planner"}'))
        
        # Get prefilter data (session stats) and inject as first message
        from app.optimizer.prefilter import prefilter
        # If session_id is empty, find the most recent session for this user
        if not session_id:
            import sqlite3 as _sq3
            _conn = _sq3.connect("app/db/local.db")
            _row = _conn.execute(
                "SELECT id FROM sessions WHERE user_id=? AND id NOT LIKE 'optimizer-%' ORDER BY created_at DESC LIMIT 1",
                (user_id,)
            ).fetchone()
            if _row:
                session_id = _row[0]
            _conn.close()
        pf = await prefilter(user_id, session_id)
        turns = pf.get("turns", 1)
        tokens = pf.get("tokens", 100)
        transcript = pf.get("transcript", [])
        
        # Insert a system message with the prefilter data for the Planner
        init_content = f"Session to optimize has {turns} turns, ~{tokens} tokens.\n\nUser feedback: {feedback or '(none)'}\n\nTranscript:\n"
        init_content += "\n".join(transcript[-25:])
        conn.execute(
            "INSERT INTO interactions (id,session_id,role,content,source,channel,created_at) VALUES (?,?,'system',?,'optimizer:init','optimizer',datetime('now'))",
            (str(uuid.uuid4()), opt_sid, init_content)
        )
        conn.commit()
        conn.close()

        # Fire-and-forget: auto-trigger the Planner to respond
        async def _auto_trigger_planner():
            try:
                import httpx
                async with httpx.AsyncClient(timeout=60.0) as hclient:
                    await hclient.post(
                        f"http://127.0.0.1:8000/api/v1/chat",
                        json={
                            "message": f"I need help optimizing this session. User feedback: {feedback or 'No specific feedback.'}",
                            "session_id": opt_sid,
                            "user_id": user_id
                        },
                    )
            except Exception as trigger_e:
                logger.warning("Auto-trigger Planner first response failed (non-fatal): %s", trigger_e)
        asyncio.create_task(_auto_trigger_planner())

        return {"status": "session_created", "optimizer_session_id": opt_sid}
    except Exception as e:
        logger.error("Manual optimizer run failed: %s", e)
        return {"status": "error", "message": str(e)}

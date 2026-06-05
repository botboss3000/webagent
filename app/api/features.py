"""Feature catalog endpoint (admin-only).

Exposes the runtime discovery catalog — every capability the app found, its
maturity, whether it's true drop-in, and whether the active edition switches it
on. Backs the **App Config → Features** report.

    GET /api/v1/features   → { active_edition, editions, counts, features[] }

Phase 1 is report-only; see docs/claude/production-editions.md.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.api.db_viewer import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["features"])


@router.get("/features")
async def get_features(_auth: dict = Depends(require_admin)):
    """Return the full feature catalog + active edition (admin-only)."""
    try:
        from app.features import build_catalog
        return build_catalog()
    except Exception as e:
        logger.exception("Failed to build feature catalog: %s", e)
        raise HTTPException(status_code=500, detail=f"Could not build feature catalog: {e}")

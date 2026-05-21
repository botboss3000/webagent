"""Scheduler provider config + admin endpoints.

Persists the user's choice of scheduler backend (``local`` for in-process,
``google`` for Google Cloud Scheduler stub, etc.) in ``scheduler_config.json``
at the project root. Mirrors the simpler shape of ``provider.json``:

    {
      "provider": "local",
      "settings": { ... per-provider knobs ... }
    }

Switching providers resets the scheduler singleton so the next call to
``get_scheduler()`` re-instantiates the right backend.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_FILE = PROJECT_ROOT / "scheduler_config.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "provider": "local",
    "settings": {},
}

VALID_PROVIDERS = {"local", "google"}


router = APIRouter(prefix="/admin", tags=["admin"])


def _load_config() -> Dict[str, Any]:
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    merged = dict(DEFAULT_CONFIG)
                    merged.update(data)
                    return merged
    except Exception as e:
        logger.warning("Failed to load scheduler_config.json: %s", e)
    return dict(DEFAULT_CONFIG)


def _save_config(data: Dict[str, Any]) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning("Failed to save scheduler_config.json: %s", e)


def get_provider() -> str:
    cfg = _load_config()
    p = (cfg.get("provider") or "local").lower()
    return p if p in VALID_PROVIDERS else "local"


def get_settings() -> Dict[str, Any]:
    cfg = _load_config()
    s = cfg.get("settings") or {}
    return s if isinstance(s, dict) else {}


class SchedulerConfigBody(BaseModel):
    provider: str
    settings: Dict[str, Any] = {}


@router.get("/settings/scheduler")
async def get_scheduler_config():
    cfg = _load_config()
    return {
        "provider": cfg.get("provider", "local"),
        "settings": cfg.get("settings", {}),
        "available": sorted(VALID_PROVIDERS),
    }


@router.post("/settings/scheduler")
async def set_scheduler_config(body: SchedulerConfigBody):
    provider = (body.provider or "").strip().lower()
    if provider not in VALID_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider '{body.provider}'.")
    cfg = {"provider": provider, "settings": body.settings or {}}
    _save_config(cfg)

    # Stop the current backend and reset the singleton so next get_scheduler() rebuilds.
    try:
        from app.scheduler import get_scheduler, reset_scheduler, start_scheduler
        try:
            await get_scheduler().stop()
        except Exception:
            pass
        reset_scheduler()
        await start_scheduler()
    except Exception as e:
        logger.warning("Could not hot-swap scheduler: %s", e)

    return {"ok": True, "provider": provider, "settings": cfg["settings"]}


@router.get("/scheduler/status")
async def scheduler_status():
    try:
        from app.scheduler import get_scheduler
        return await get_scheduler().get_status()
    except Exception as e:
        return {"provider": "unknown", "running": False, "error": str(e)}

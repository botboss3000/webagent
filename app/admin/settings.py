"""
Settings endpoints — toggleable features for the agent.

Currently supports:
  - metadata_logging: stores full prompt context in interactions.metadata
"""

import logging
import os
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/settings", tags=["admin"])

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
METADATA_FLAG = PROJECT_ROOT / ".metadata-enabled"


def _is_metadata_enabled() -> bool:
    return METADATA_FLAG.exists()


# Shared helper for agent loops to import directly
async def is_metadata_enabled() -> bool:
    """Check if metadata logging is enabled. Import this in agent loops."""
    return METADATA_FLAG.exists()


class MetadataSetting(BaseModel):
    enabled: bool


@router.get("/metadata", response_model=MetadataSetting)
async def get_metadata_setting():
    """Check if metadata logging is enabled."""
    return MetadataSetting(enabled=_is_metadata_enabled())


@router.post("/metadata", response_model=MetadataSetting)
async def set_metadata_setting(setting: MetadataSetting):
    """Enable or disable metadata logging."""
    if setting.enabled:
        METADATA_FLAG.touch()
        logger.info("Metadata logging enabled")
    else:
        if METADATA_FLAG.exists():
            METADATA_FLAG.unlink()
        logger.info("Metadata logging disabled")
    return MetadataSetting(enabled=_is_metadata_enabled())

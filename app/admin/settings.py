"""
Settings endpoints — toggleable features for the agent.

Currently supports:
  - metadata_logging: stores full prompt context in interactions.metadata
  - provider: AI provider (openrouter, openai) + API key config
"""

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/settings", tags=["admin"])

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
METADATA_FLAG = PROJECT_ROOT / ".metadata-enabled"
PROVIDER_FILE = PROJECT_ROOT / "provider.json"

DEFAULT_PROVIDER = {
    "provider": "openrouter",
    "api_key": "",
}


def _load_provider() -> dict:
    """Load saved provider config from disk."""
    try:
        if PROVIDER_FILE.exists():
            with open(PROVIDER_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.warning("Failed to load provider.json: %s", e)
    return dict(DEFAULT_PROVIDER)


def _save_provider(config: dict) -> None:
    """Save provider config to disk and set env var immediately."""
    try:
        with open(PROVIDER_FILE, "w") as f:
            json.dump(config, f)
        if config.get("api_key"):
            os.environ["OPENROUTER_API_KEY"] = config["api_key"]
        logger.info("Provider config saved and applied")
    except Exception as e:
        logger.warning("Failed to save provider.json: %s", e)


def apply_provider_config() -> None:
    """Call on startup to apply saved provider config."""
    config = _load_provider()
    if config.get("api_key"):
        os.environ["OPENROUTER_API_KEY"] = config["api_key"]
        logger.info("Applied saved provider config")


def _is_metadata_enabled() -> bool:
    return METADATA_FLAG.exists()


async def is_metadata_enabled() -> bool:
    """Check if metadata logging is enabled."""
    return METADATA_FLAG.exists()


class ProviderConfig(BaseModel):
    provider: str  # "openrouter" or "openai"
    api_key: str


class MetadataSetting(BaseModel):
    enabled: bool


@router.get("/provider", response_model=ProviderConfig)
async def get_provider():
    """Get current provider configuration. API key is masked for security."""
    config = _load_provider()
    masked = dict(config)
    if masked.get("api_key") and len(masked["api_key"]) > 8:
        masked["api_key"] = masked["api_key"][:4] + "..." + masked["api_key"][-4:]
    return ProviderConfig(**masked)


@router.post("/provider", response_model=dict)
async def set_provider(config: ProviderConfig):
    """Set provider configuration. Stored on device, never sent elsewhere."""
    _save_provider({"provider": config.provider, "api_key": config.api_key})
    return {"status": "ok", "message": f"Provider set to {config.provider}"}


@router.get("/metadata", response_model=MetadataSetting)
async def get_metadata_setting():
    return MetadataSetting(enabled=_is_metadata_enabled())


@router.post("/metadata", response_model=MetadataSetting)
async def set_metadata_setting(setting: MetadataSetting):
    if setting.enabled:
        METADATA_FLAG.touch()
    else:
        METADATA_FLAG.unlink() if METADATA_FLAG.exists() else None
    return MetadataSetting(enabled=_is_metadata_enabled())

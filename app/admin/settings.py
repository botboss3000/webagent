"""
Settings endpoints — toggleable features for the agent.

Currently supports:
  - metadata_logging: stores full prompt context in interactions.metadata
  - provider: AI provider (openrouter, openai) + API key + model config
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

import httpx
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
    "model": "",
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
    """Save provider config to disk and set env vars immediately."""
    try:
        with open(PROVIDER_FILE, "w") as f:
            json.dump(config, f)
        if config.get("api_key"):
            os.environ["OPENROUTER_API_KEY"] = config["api_key"]
        if config.get("model"):
            os.environ["OPENROUTER_MODEL"] = config["model"]
        logger.info("Provider config saved and applied")
    except Exception as e:
        logger.warning("Failed to save provider.json: %s", e)


def apply_provider_config() -> None:
    """Call on startup to apply saved provider config."""
    config = _load_provider()
    if config.get("api_key"):
        os.environ["OPENROUTER_API_KEY"] = config["api_key"]
    if config.get("model"):
        os.environ["OPENROUTER_MODEL"] = config["model"]
        logger.info("Applied saved provider config")


def _is_metadata_enabled() -> bool:
    return METADATA_FLAG.exists()


async def is_metadata_enabled() -> bool:
    """Check if metadata logging is enabled."""
    return METADATA_FLAG.exists()


class ProviderConfig(BaseModel):
    provider: str  # "openrouter" or "openai"
    api_key: str
    model: str = ""


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
    """Set provider configuration. Stored on device, never sent elsewhere.

    If api_key is empty, the existing key from disk is preserved.
    """
    existing = _load_provider()
    merged = {
        "provider": config.provider,
        "api_key": config.api_key or existing.get("api_key", ""),
        "model": config.model or existing.get("model", ""),
    }
    _save_provider(merged)
    return {"status": "ok", "message": f"Provider set to {config.provider}"}


@router.post("/provider/clear", response_model=dict)
async def clear_provider():
    """Clear provider configuration (API key and model). Provider resets to openrouter."""
    _save_provider(dict(DEFAULT_PROVIDER))
    if "OPENROUTER_API_KEY" in os.environ:
        del os.environ["OPENROUTER_API_KEY"]
    if "OPENROUTER_MODEL" in os.environ:
        del os.environ["OPENROUTER_MODEL"]
    return {"status": "ok", "message": "Provider settings cleared"}


@router.get("/models")
async def get_models(provider: str = ""):
    """Fetch available models from the selected provider's API using saved API key.

    Query param `provider` can be "openrouter" or "openai".
    If omitted, the saved provider from config is used.
    """
    config = _load_provider()
    api_key = config.get("api_key", "")
    if not api_key:
        return {"error": "No API key configured", "models": []}

    prov = provider or config.get("provider", "openrouter")

    if prov == "openai":
        url = "https://api.openai.com/v1/models"
        name_key = "id"  # OpenAI response has no separate name field
    else:
        url = "https://openrouter.ai/api/v1/models"
        name_key = "name"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            resp = await client.get(url, headers=headers)
            if resp.status_code == 401:
                return {"error": "Invalid API key", "models": []}
            resp.raise_for_status()
            data = resp.json()
            models = []
            for m in data.get("data", []):
                models.append({
                    "id": m["id"],
                    "name": m.get(name_key, m["id"]),
                })
            models.sort(key=lambda x: x["id"])
            return {"error": None, "models": models}
    except httpx.RequestError as e:
        logger.warning(f"Failed to fetch {prov} models: %s", e)
        return {"error": f"Network error: {e}", "models": []}
    except Exception as e:
        logger.warning(f"Failed to fetch {prov} models: %s", e)
        return {"error": str(e), "models": []}


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

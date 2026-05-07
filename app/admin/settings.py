"""
Settings endpoints — toggleable features for the agent.

Currently supports:
  - metadata_logging: stores full prompt context in interactions.metadata
  - provider: AI provider config (provider, base_url, api_key, model)
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
    "base_url": "https://openrouter.ai/api/v1",
    "api_key": "",
    "model": "",
    "providers": {},
}

PROVIDER_PRESETS = {
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
    },
    "groq": {
        "name": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
    },
    "together": {
        "name": "Together AI",
        "base_url": "https://api.together.xyz/v1",
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
    },
    "mistral": {
        "name": "Mistral AI",
        "base_url": "https://api.mistral.ai/v1",
    },
    "fireworks": {
        "name": "Fireworks AI",
        "base_url": "https://api.fireworks.ai/inference/v1",
    },
    "xai": {
        "name": "xAI (Grok)",
        "base_url": "https://api.x.ai/v1",
    },
    "perplexity": {
        "name": "Perplexity",
        "base_url": "https://api.perplexity.ai",
    },
    "ollama": {
        "name": "Ollama (local)",
        "base_url": "http://localhost:11434/v1",
    },
    "deepinfra": {
        "name": "DeepInfra",
        "base_url": "https://api.deepinfra.com/v1/openai",
    },
    "lmstudio": {
        "name": "LM Studio (local)",
        "base_url": "http://localhost:1234/v1",
    },
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
    """Save provider config to disk and set env vars immediately.

    Sets generic LLM_* env vars so the agent loop and compat shim pick them up.
    If the saved provider has a preset base_url with no custom override, fills it.
    """
    # Fill in base_url from preset if missing
    provider = config.get("provider", "")
    if not config.get("base_url") and provider in PROVIDER_PRESETS:
        config["base_url"] = PROVIDER_PRESETS[provider]["base_url"]

    try:
        with open(PROVIDER_FILE, "w") as f:
            json.dump(config, f)
    except Exception as e:
        logger.warning("Failed to save provider.json: %s", e)

    # Set generic env vars (with OPENROUTER_* fallbacks for backward compat)
    provider = config.get("provider", "openrouter")
    os.environ["LLM_PROVIDER"] = provider
    os.environ["OPENROUTER_PROVIDER"] = provider

    if config.get("api_key"):
        os.environ["LLM_API_KEY"] = config["api_key"]
        os.environ["OPENROUTER_API_KEY"] = config["api_key"]
    else:
        os.environ.pop("LLM_API_KEY", None)
        os.environ.pop("OPENROUTER_API_KEY", None)

    if config.get("base_url"):
        os.environ["LLM_BASE_URL"] = config["base_url"]
        os.environ["OPENROUTER_BASE_URL"] = config["base_url"]
    else:
        os.environ.pop("LLM_BASE_URL", None)
        os.environ.pop("OPENROUTER_BASE_URL", None)

    if config.get("model"):
        os.environ["LLM_MODEL"] = config["model"]
        os.environ["OPENROUTER_MODEL"] = config["model"]
    else:
        os.environ.pop("LLM_MODEL", None)
        os.environ.pop("OPENROUTER_MODEL", None)

    logger.info("Provider config saved and applied: %s", config.get("provider"))


def apply_provider_config() -> None:
    """Call on startup to apply saved provider config."""
    config = _load_provider()
    _apply_config_to_env(config)


def _apply_config_to_env(config: dict) -> None:
    """Apply provider config dict to environment variables."""
    provider = config.get("provider", "")
    base_url = config.get("base_url")
    if not base_url and provider in PROVIDER_PRESETS:
        base_url = PROVIDER_PRESETS[provider]["base_url"]

    if provider:
        os.environ["LLM_PROVIDER"] = provider
    if config.get("api_key"):
        os.environ["LLM_API_KEY"] = config["api_key"]
        os.environ["OPENROUTER_API_KEY"] = config["api_key"]
    if base_url:
        os.environ["LLM_BASE_URL"] = base_url
        os.environ["OPENROUTER_BASE_URL"] = base_url
    if config.get("model"):
        os.environ["LLM_MODEL"] = config["model"]
        os.environ["OPENROUTER_MODEL"] = config["model"]


def _is_metadata_enabled() -> bool:
    return METADATA_FLAG.exists()


async def is_metadata_enabled() -> bool:
    """Check if metadata logging is enabled."""
    return METADATA_FLAG.exists()


class ProviderConfig(BaseModel):
    provider: str
    base_url: str = ""
    api_key: str
    model: str = ""
    providers: dict = {}


class MetadataSetting(BaseModel):
    enabled: bool


@router.get("/providers")
async def get_providers():
    """Return known provider presets (id → name + base_url)."""
    return PROVIDER_PRESETS


@router.get("/provider", response_model=ProviderConfig)
async def get_provider():
    """Get current provider configuration. API key is masked for security.
    Returns the full `providers` map so the frontend can persist per-provider
    key/model across switches.
    """
    config = _load_provider()
    # Ensure providers dict exists
    if "providers" not in config:
        config["providers"] = {}
    masked = dict(config)
    # Mask current provider's key
    if masked.get("api_key") and len(masked["api_key"]) > 8:
        masked["api_key"] = masked["api_key"][:4] + "..." + masked["api_key"][-4:]
    # Mask all keys in the providers map
    providers = masked.get("providers", {})
    if providers:
        masked_providers = {}
        for pk, pv in providers.items():
            entry = dict(pv)
            key = entry.get("api_key", "")
            if key and len(key) > 8:
                entry["api_key"] = key[:4] + "..." + key[-4:]
            masked_providers[pk] = entry
        masked["providers"] = masked_providers
    # Ensure base_url is present
    if not masked.get("base_url"):
        prov = masked.get("provider", "")
        if prov in PROVIDER_PRESETS:
            masked["base_url"] = PROVIDER_PRESETS[prov]["base_url"]
    return ProviderConfig(**masked)


@router.post("/provider", response_model=dict)
async def set_provider(config: ProviderConfig):
    """Set provider configuration. Stored on device, never sent elsewhere.

    Merges the `providers` map from the request so per-provider key+model
    persist across switches. If api_key is empty, the existing key is preserved.
    If base_url is empty, the preset URL for the provider is filled in.
    """
    existing = _load_provider()
    existing_providers = existing.get("providers", {})
    request_providers = config.providers or {}

    # Merge incoming providers map over existing
    merged_providers = dict(existing_providers)
    for pk, pv in request_providers.items():
        merged_providers[pk] = pv

    # Ensure current provider's key+model+url live in root too
    current_key = config.api_key or existing.get("api_key", "")
    current_model = config.model or existing.get("model", "")
    current_url = config.base_url or existing.get("base_url", "")
    
    # Sync the active provider in the map
    merged_providers[config.provider] = {
        "api_key": current_key,
        "model": current_model,
        "base_url": current_url,
    }

    merged = {
        "provider": config.provider,
        "base_url": current_url,
        "api_key": current_key,
        "model": current_model,
        "providers": merged_providers,
    }
    _save_provider(merged)
    return {"status": "ok", "message": f"Provider set to {config.provider}"}


@router.post("/provider/clear", response_model=dict)
async def clear_provider():
    """Clear provider configuration (API key, model, base_url). Provider resets to openrouter."""
    _save_provider(dict(DEFAULT_PROVIDER))
    for var in ["LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_PROVIDER",
                "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "OPENROUTER_MODEL", "OPENROUTER_PROVIDER"]:
        os.environ.pop(var, None)
    return {"status": "ok", "message": "Provider settings cleared"}


@router.get("/models")
async def get_models(provider: str = ""):
    """Fetch available models from the configured provider's API.

    Uses the saved base_url to determine the models endpoint.
    Query param `provider` overrides the saved provider.
    """
    config = _load_provider()
    api_key = config.get("api_key", "")
    if not api_key:
        return {"error": "No API key configured", "models": []}

    prov = provider or config.get("provider", "openrouter")
    base_url = config.get("base_url", "")

    # If no saved base_url, get from preset
    if not base_url and prov in PROVIDER_PRESETS:
        base_url = PROVIDER_PRESETS[prov]["base_url"]

    if not base_url:
        return {"error": "No base URL configured for this provider", "models": []}

    # Build models endpoint: <base_url>/models (handle trailing slash)
    models_url = base_url.rstrip("/") + "/models"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            resp = await client.get(models_url, headers=headers)
            if resp.status_code == 401:
                return {"error": "Invalid API key", "models": []}
            resp.raise_for_status()
            data = resp.json()
            models = []
            for m in data.get("data", []):
                models.append({
                    "id": m["id"],
                    "name": m.get("name", m["id"]),
                })
            models.sort(key=lambda x: x["id"])
            return {"error": None, "models": models}
    except httpx.RequestError as e:
        logger.warning(f"Failed to fetch models from {models_url}: %s", e)
        return {"error": f"Network error: {e}", "models": []}
    except Exception as e:
        logger.warning(f"Failed to fetch models from {models_url}: %s", e)
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

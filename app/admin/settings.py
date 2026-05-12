"""
Settings endpoints — toggleable features for the agent.

Supports:
  - metadata_logging: stores full prompt context in interactions.metadata
  - provider: per-user AI provider config (provider, base_url, api_key, model)

Provider configs are stored per user_id in provider.json:
  {
    "__anonymous__": { ... config ... },
    "admin_default": { ... config ... },
    ...
  }
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Header, Query
from pydantic import BaseModel

from app.auth.jwt import decode_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/settings", tags=["admin"])

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
METADATA_FLAG = PROJECT_ROOT / ".metadata-enabled"
PROVIDER_FILE = PROJECT_ROOT / "provider.json"

ANONYMOUS_KEY = "__anonymous__"

DEFAULT_PROVIDER = {
    "provider": "openrouter",
    "base_url": "https://openrouter.ai/api/v1",
    "api_key": "",
    "model": "",
    "providers": {},
    "parallel_mode": False,
    "multi_providers": [],
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


# ── User ID extraction ──────────────────────────────────────────────────────

def _resolve_user_id(authorization: str = "", token_qs: str = "") -> str:
    """Extract user_id from Authorization header or query param.
    Returns ANONYMOUS_KEY if no valid token.
    """
    raw = ""
    if authorization.startswith("Bearer "):
        raw = authorization[7:]
    if not raw and token_qs:
        raw = token_qs

    if raw:
        payload = decode_token(raw)
        if payload:
            return payload.get("user_id", ANONYMOUS_KEY)

    return ANONYMOUS_KEY


# ── Per-user provider storage ──────────────────────────────────────────────

def _load_all_providers() -> dict:
    """Load the full provider.json (map of user_id → config)."""
    try:
        if PROVIDER_FILE.exists():
            with open(PROVIDER_FILE) as f:
                data = json.load(f)
                # Migration: if old flat format (has "provider" key at root),
                # wrap under ANONYMOUS_KEY
                if "provider" in data:
                    data = {ANONYMOUS_KEY: data}
                    _save_all_providers(data)
                return data
    except Exception as e:
        logger.warning("Failed to load provider.json: %s", e)
    return {}


def _save_all_providers(data: dict) -> None:
    """Save the full provider.json."""
    try:
        with open(PROVIDER_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning("Failed to save provider.json: %s", e)


def _load_provider(user_id: str) -> dict:
    """Load provider config for a specific user. Returns DEFAULT_PROVIDER if none."""
    all_configs = _load_all_providers()
    config = all_configs.get(user_id)
    if config:
        return dict(config)
    # Fall back to admin user's config (so anonymous users get a working LLM)
    config = all_configs.get("admin_default")
    if config:
        return dict(config)
    # Fall back to anonymous config
    config = all_configs.get(ANONYMOUS_KEY)
    if config:
        return dict(config)
    return dict(DEFAULT_PROVIDER)


def _save_provider(user_id: str, config: dict) -> None:
    """Save provider config for a specific user."""
    # Fill in base_url from preset if missing
    provider = config.get("provider", "")
    if not config.get("base_url") and provider in PROVIDER_PRESETS:
        config["base_url"] = PROVIDER_PRESETS[provider]["base_url"]

    all_configs = _load_all_providers()
    all_configs[user_id] = config
    _save_all_providers(all_configs)

    # Apply this config as the active env vars (current user's session)
    _apply_config_to_env(config)

    logger.info("Provider config saved for user %s: %s", user_id[:12], config.get("provider"))


async def load_provider_for_user(user_id: str) -> None:
    """Load a user's provider config into env vars.
    Tries auth_elements DB table first, falls back to provider.json.
    Called at the start of each agent loop.
    """
    # Try DB first — own config, then admin fallback
    try:
        from app.db import get_db
        db = get_db()
        # Try this user's config first
        elem = await db.auth_element_get(user_id, "llm", "default")
        if elem:
            cfg = json.loads(elem.get("config", "{}"))
            cfg["api_key"] = elem.get("secret_ref", "")
            _apply_config_to_env(cfg)
            return
        # Fall back to admin user's config (anonymous visitors get a working LLM)
        elem = await db.auth_element_get("admin_default", "llm", "default")
        if elem:
            cfg = json.loads(elem.get("config", "{}"))
            cfg["api_key"] = elem.get("secret_ref", "")
            _apply_config_to_env(cfg)
            return
    except Exception:
        pass

    # Fall back to provider.json
    config = _load_provider(user_id)
    _apply_config_to_env(config)


def apply_provider_config() -> None:
    """Call on startup to apply saved anonymous provider config."""
    config = _load_provider(ANONYMOUS_KEY)
    _apply_config_to_env(config)


def _apply_config_to_env(config: dict) -> None:
    """Apply provider config dict to environment variables.
    Also sets MULTI_PROVIDERS and PARALLEL_MODE for the race engine.
    """
    provider = config.get("provider", "")
    base_url = config.get("base_url")
    if not base_url and provider in PROVIDER_PRESETS:
        base_url = PROVIDER_PRESETS[provider]["base_url"]

    if provider:
        os.environ["LLM_PROVIDER"] = provider
        os.environ["OPENROUTER_PROVIDER"] = provider
    if config.get("api_key"):
        os.environ["LLM_API_KEY"] = config["api_key"]
        os.environ["OPENROUTER_API_KEY"] = config["api_key"]
    else:
        os.environ.pop("LLM_API_KEY", None)
        os.environ.pop("OPENROUTER_API_KEY", None)
    if base_url:
        os.environ["LLM_BASE_URL"] = base_url
        os.environ["OPENROUTER_BASE_URL"] = base_url
    else:
        os.environ.pop("LLM_BASE_URL", None)
        os.environ.pop("OPENROUTER_BASE_URL", None)
    if config.get("model"):
        os.environ["LLM_MODEL"] = config["model"]
        os.environ["OPENROUTER_MODEL"] = config["model"]
    else:
        os.environ.pop("LLM_MODEL", None)
        os.environ.pop("OPENROUTER_MODEL", None)

    # Multi-provider env vars for the race engine
    parallel_mode = config.get("parallel_mode", False)
    multi_providers = config.get("multi_providers", [])
    os.environ["PARALLEL_MODE"] = "true" if parallel_mode and len(multi_providers) >= 2 else "false"
    if parallel_mode and multi_providers:
            # Sanitize: ensure each entry has required fields, strip None
        # Only include enabled providers
        cleaned = []
        for p in multi_providers:
            if p.get("enabled", True) and p.get("api_key") and p.get("base_url"):
                cleaned.append({
                    "provider": p.get("provider", "custom"),
                    "base_url": p["base_url"],
                    "api_key": p["api_key"],
                    "model": p.get("model", ""),
                    "rating": p.get("rating", 0),
                })
        os.environ["MULTI_PROVIDERS"] = json.dumps(cleaned)
    else:
        os.environ.pop("MULTI_PROVIDERS", None)


def _is_metadata_enabled() -> bool:
    return METADATA_FLAG.exists()


async def is_metadata_enabled() -> bool:
    """Check if metadata logging is enabled."""
    return METADATA_FLAG.exists()


# ── Pydantic models ────────────────────────────────────────────────────────

class ProviderConfig(BaseModel):
    provider: str
    base_url: str = ""
    api_key: str
    model: str = ""
    providers: dict = {}


class MultiProviderEntry(BaseModel):
    provider: str
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    enabled: bool = True
    rating: int = 0


class MultiProvidersRequest(BaseModel):
    parallel_mode: bool = False
    providers: list[MultiProviderEntry] = []


class MetadataSetting(BaseModel):
    enabled: bool


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.get("/providers")
async def get_providers():
    """Return known provider presets (id → name + base_url)."""
    return PROVIDER_PRESETS


@router.get("/provider", response_model=ProviderConfig)
async def get_provider(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Get current provider configuration for the requesting user.
    Reads from the auth_elements table in the DB (per-user), falls back to provider.json.
    API key is returned as plaintext.
    """
    user_id = _resolve_user_id(authorization or "", token or "")

    # Try DB first (auth_elements table) — own config, then admin fallback
    config = None
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(user_id, "llm", "default")
        if elem:
            cfg = json.loads(elem.get("config", "{}"))
            cfg["api_key"] = elem.get("secret_ref", "")
            config = cfg
        else:
            # Fall back to admin user's config (so anonymous visitors see working LLM in settings)
            elem = await db.auth_element_get("admin_default", "llm", "default")
            if elem:
                cfg = json.loads(elem.get("config", "{}"))
                cfg["api_key"] = elem.get("secret_ref", "")
                config = cfg
    except Exception:
        pass

    # Fall back to provider.json
    if config is None:
        config = _load_provider(user_id)

    # Ensure providers dict exists
    if "providers" not in config:
        config["providers"] = {}

    # Ensure base_url is present
    if not config.get("base_url"):
        prov = config.get("provider", "")
        if prov in PROVIDER_PRESETS:
            config["base_url"] = PROVIDER_PRESETS[prov]["base_url"]
    return ProviderConfig(**config)


@router.post("/provider", response_model=dict)
async def set_provider(
    config: ProviderConfig,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Set provider configuration for the requesting user.
    Stored in auth_elements table (DB) AND provider.json fallback.
    Never shared between users.
    """
    user_id = _resolve_user_id(authorization or "", token or "")
    existing = _load_provider(user_id)
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

    # Save to DB (auth_elements table) — primary storage
    try:
        from app.db import get_db
        db = get_db()
        await db.auth_element_set(
            user_id=user_id,
            service="llm",
            config={
                "provider": config.provider,
                "base_url": current_url,
                "model": current_model,
                "providers": merged_providers,
            },
            secret_ref=current_key,
            label="default",
        )
    except Exception as e:
        logger.warning("Failed to save to auth_elements DB: %s", e)

    # Also save to provider.json (fallback)
    _save_provider(user_id, merged)

    logger.info("Provider config set for user %s: %s", user_id[:12], config.provider)
    return {"status": "ok", "message": f"Provider set to {config.provider}", "user": user_id}


@router.post("/provider/clear", response_model=dict)
async def clear_provider(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Clear provider configuration for the requesting user."""
    user_id = _resolve_user_id(authorization or "", token or "")
    _save_provider(user_id, dict(DEFAULT_PROVIDER))
    logger.info("Provider config cleared for user %s", user_id[:12])
    return {"status": "ok", "message": "Provider settings cleared", "user": user_id}


async def update_multi_provider_rating(user_id: str, provider: str, model: str, delta: int):
    """Update a specific parallel provider's rating in the dedicated DB table. If rating < -5, auto-disable in config."""
    try:
        from app.db import get_db
        db = get_db()
        new_rating = await db.update_provider_rating(user_id, provider, model, delta)
    except Exception as e:
        logger.warning(f"Failed to update provider rating table: {e}")
        new_rating = None

    if new_rating is not None and new_rating < -5:
        # Actually disable them in the flat config structure
        existing = _load_provider(user_id)
        multi = existing.get("multi_providers", [])
        changed = False
        
        for p in multi:
            if p.get("provider") == provider and p.get("model") == model:
                if p.get("enabled", True):
                    p["enabled"] = False
                    logger.info(f"Auto-disabled provider {provider} {model} due to rating {new_rating}")
                    changed = True
                break
                
        if changed:
            try:
                db_config = {
                    "provider": existing.get("provider", ""),
                    "base_url": existing.get("base_url", ""),
                    "model": existing.get("model", ""),
                    "providers": existing.get("providers", {}),
                    "parallel_mode": existing.get("parallel_mode", False),
                    "multi_providers": multi,
                }
                await db.auth_element_set(
                    user_id=user_id,
                    service="llm",
                    config=db_config,
                    secret_ref=existing.get("api_key", ""),
                    label="default",
                )
            except Exception:
                pass
            _save_provider(user_id, existing)

@router.get("/multi-providers")
async def get_multi_providers(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Get multi-provider parallel config for the requesting user.
    Returns parallel_mode flag and list of provider entries.
    """
    user_id = _resolve_user_id(authorization or "", token or "")
    config = None

    # Try DB first
    try:
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(user_id, "llm", "default")
        if elem:
            cfg = json.loads(elem.get("config", "{}"))
            cfg["api_key"] = elem.get("secret_ref", "")
            config = cfg
        else:
            elem = await db.auth_element_get("admin_default", "llm", "default")
            if elem:
                cfg = json.loads(elem.get("config", "{}"))
                cfg["api_key"] = elem.get("secret_ref", "")
                config = cfg
    except Exception:
        pass

    if config is None:
        config = _load_provider(user_id)

    parallel_mode = config.get("parallel_mode", False)
    raw_providers = config.get("multi_providers", [])

    # Fetch dedicated ratings
    ratings_map = {}
    try:
        from app.db import get_db
        db = get_db()
        ratings_map = await db.get_provider_ratings(user_id)
    except Exception as e:
        logger.warning(f"Failed to fetch DB ratings: {e}")

    result_providers = []
    for p in raw_providers:
        entry = dict(p)
        db_rating = ratings_map.get((entry.get("provider"), entry.get("model")), 0)
        entry["rating"] = db_rating
        result_providers.append(entry)

    return {
        "parallel_mode": parallel_mode,
        "providers": result_providers,
    }


@router.post("/multi-providers", response_model=dict)
async def set_multi_providers(
    body: MultiProvidersRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Set multi-provider parallel config for the requesting user.
    Saves to both DB auth_elements and provider.json.
    When parallel_mode is off or providers has < 2 entries,
    system falls back to the existing single-provider path.
    """
    user_id = _resolve_user_id(authorization or "", token or "")
    existing = _load_provider(user_id)

    merged = dict(existing)
    merged["parallel_mode"] = body.parallel_mode
    merged["multi_providers"] = [p.model_dump() for p in body.providers]

    # Also mirror the first provider's key up to root for backward compat
    if body.providers:
        first = body.providers[0]
        merged["provider"] = first.provider
        if first.base_url:
            merged["base_url"] = first.base_url
        if first.api_key:
            merged["api_key"] = first.api_key
        if first.model:
            merged["model"] = first.model

    # Save to DB (auth_elements table)
    try:
        from app.db import get_db
        db = get_db()
        db_config = {
            "provider": merged.get("provider", ""),
            "base_url": merged.get("base_url", ""),
            "model": merged.get("model", ""),
            "providers": merged.get("providers", {}),
            "parallel_mode": merged.get("parallel_mode", False),
            "multi_providers": merged.get("multi_providers", []),
        }
        await db.auth_element_set(
            user_id=user_id,
            service="llm",
            config=db_config,
            secret_ref=merged.get("api_key", ""),
            label="default",
        )
    except Exception as e:
        logger.warning("Failed to save multi-providers to DB: %s", e)

    # Save to provider.json (fallback)
    _save_provider(user_id, merged)

    count = len(body.providers)
    mode = "parallel" if body.parallel_mode and count >= 2 else "single"
    logger.info("Multi-provider config saved for user %s: mode=%s, count=%d", user_id[:12], mode, count)
    return {
        "status": "ok",
        "mode": mode,
        "count": count,
        "message": f"Multi-provider config saved. Mode: {mode}, {count} provider(s).",
    }


@router.get("/models")
async def get_models(
    provider: str = "",
    api_key: str = Query("", alias="api_key"),
    base_url: str = Query("", alias="base_url"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Fetch available models from the configured provider's API.
    Uses the requesting user's saved config, or the explicit api_key/base_url
    params passed by the frontend (for per-row model fetching in parallel providers UI).
    """
    user_id = _resolve_user_id(authorization or "", token or "")

    # Explicit params override saved config (used by parallel provider rows)
    if api_key and base_url:
        # Use the explicitly provided key and URL directly
        pass
    else:
        # Fall back to saved provider config
        config = _load_provider(user_id)
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

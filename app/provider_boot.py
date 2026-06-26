"""Boot-time LLM provider config — deliberately OUTSIDE app/admin/.

The server applies the saved provider config (model, API key, base URL) to
environment variables at startup, before the modules that build LLM clients are
imported. That step must not hard-depend on the admin package, otherwise the
agent's LLM would stop working the moment app/admin/ is removed.

Behaviour:
  * Normal case (admin present): delegate to app.admin.settings.apply_provider_config,
    which also keeps the multi-provider / race-engine env vars in sync.
  * Removable case (admin absent): fall back to a lean self-contained loader that
    reads data/config/provider.json and applies the primary provider fields.

See app/admin/README.md — removing app/admin/ deactivates admin tools but the
core app (including this boot step) keeps working.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# app/provider_boot.py -> app/ -> repo root -> data/config/provider.json
PROVIDER_FILE = Path(__file__).resolve().parent.parent / "data" / "config" / "provider.json"
ANONYMOUS_KEY = "__anonymous__"


def _lean_apply_to_env(config: dict) -> None:
    """Apply the primary provider fields to env vars (no multi-provider race setup)."""
    provider = config.get("provider", "")
    base_url = config.get("base_url", "")
    if provider:
        os.environ["LLM_PROVIDER"] = provider
        os.environ["OPENROUTER_PROVIDER"] = provider
    if config.get("api_key"):
        os.environ["LLM_API_KEY"] = config["api_key"]
        os.environ["OPENROUTER_API_KEY"] = config["api_key"]
    if base_url:
        os.environ["LLM_BASE_URL"] = base_url
        os.environ["OPENROUTER_BASE_URL"] = base_url
    if config.get("model"):
        os.environ["LLM_MODEL"] = config["model"]
        os.environ["OPENROUTER_MODEL"] = config["model"]


def _lean_apply_provider_config() -> None:
    """Self-contained boot applier used when app/admin/ is unavailable."""
    try:
        if not PROVIDER_FILE.is_file():
            return
        data = json.loads(PROVIDER_FILE.read_text(encoding="utf-8")) or {}
        if "provider" in data:  # legacy flat (single-user) format
            data = {ANONYMOUS_KEY: data}
        config = data.get(ANONYMOUS_KEY) or data.get("admin") or {}
        if config:
            _lean_apply_to_env(config)
    except Exception as e:
        logger.warning("Lean provider-config boot apply failed: %s", e)


def apply_provider_config() -> None:
    """Apply saved provider config to environment variables at startup.

    Prefers the full admin implementation (multi-provider aware); falls back to
    the lean loader above if app/admin/ has been removed.
    """
    try:
        from app.admin.settings import apply_provider_config as _full
    except Exception:
        _full = None
    if _full is not None:
        _full()
    else:
        _lean_apply_provider_config()

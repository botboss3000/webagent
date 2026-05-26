"""
Page-store factory: pick a PageStore based on persisted config.

Mode is stored in `pages_mode.json` alongside `db_mode.json` and
`secrets_mode.json`. Default is `filesystem` because the app's primary
target is local-hosted use, where files-on-disk is the simplest and most
editable option. Cloud / multi-user deploys should switch to `database`
(or `hybrid`) via the storage panel.

Available stores:
  - "filesystem"  (default)
  - "database"
  - "hybrid"
"""

import json
import logging
import os
from typing import Optional

from app.pages_store.interface import PageStore

logger = logging.getLogger(__name__)

_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODE_FILE = os.path.join(_AGENT_DIR, "pages_mode.json")

_AVAILABLE = ("filesystem", "database", "hybrid")

_store: Optional[PageStore] = None
_mode: Optional[str] = None


def _load_saved_mode() -> Optional[str]:
    try:
        if os.path.exists(_MODE_FILE):
            with open(_MODE_FILE, "r") as f:
                return json.load(f).get("mode")
    except Exception as e:
        logger.warning("Failed to load pages_mode.json: %s", e)
    return None


def _save_mode(mode: str) -> None:
    if os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env":
        return
    try:
        with open(_MODE_FILE, "w") as f:
            json.dump({"mode": mode}, f)
    except Exception as e:
        logger.warning("Failed to save pages_mode.json: %s", e)


def get_mode() -> str:
    """Return the active page-store mode name."""
    global _mode
    if _mode is None:
        env_choice = os.environ.get("WEBAGENT_PAGES_STORE", "").strip().lower()
        if env_choice in _AVAILABLE:
            _mode = env_choice
        else:
            _mode = _load_saved_mode() or "filesystem"
    return _mode


def set_mode(mode: str) -> None:
    """Switch the page store. Persists to disk and resets the cached instance."""
    global _store, _mode
    if mode not in _AVAILABLE:
        raise ValueError(f"Invalid mode '{mode}'. Must be one of {_AVAILABLE}")
    _mode = mode
    _store = None
    _save_mode(mode)
    logger.info("Page store switched to '%s'", mode)


def list_modes() -> list:
    return list(_AVAILABLE)


def _construct(mode: str) -> PageStore:
    if mode == "filesystem":
        from app.pages_store.filesystem import FilesystemPageStore
        return FilesystemPageStore()
    if mode == "database":
        from app.pages_store.database import DatabasePageStore
        return DatabasePageStore()
    if mode == "hybrid":
        from app.pages_store.hybrid import HybridPageStore
        return HybridPageStore()
    raise ValueError(f"Unknown page store mode: {mode}")


def get_pages_store() -> PageStore:
    """Return the active PageStore instance (lazy)."""
    global _store
    if _store is None:
        mode = get_mode()
        try:
            _store = _construct(mode)
            logger.info("Initialized page store: %s", mode)
        except Exception as e:
            logger.warning(
                "Failed to construct page store '%s' (%s); falling back to filesystem",
                mode, e,
            )
            from app.pages_store.filesystem import FilesystemPageStore
            _store = FilesystemPageStore()
    return _store


def get_status() -> dict:
    """For UI: current mode + availability info."""
    return {
        "mode": get_mode(),
        "available": list(_AVAILABLE),
        "env_locked": os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env",
    }

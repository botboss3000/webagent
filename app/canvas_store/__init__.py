"""
Page-store factory: pick a CanvasStore based on persisted config.

Mode is stored in `canvas_mode.json` alongside `db_mode.json` and
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

from app.canvas_store.interface import CanvasStore

logger = logging.getLogger(__name__)

_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODE_FILE = os.path.join(_AGENT_DIR, "canvas_mode.json")

_AVAILABLE = ("filesystem", "database", "hybrid")

_store: Optional[CanvasStore] = None
_mode: Optional[str] = None


def _load_saved_mode() -> Optional[str]:
    try:
        if os.path.exists(_MODE_FILE):
            with open(_MODE_FILE, "r") as f:
                return json.load(f).get("mode")
    except Exception as e:
        logger.warning("Failed to load canvas_mode.json: %s", e)
    return None


def _save_mode(mode: str) -> None:
    if os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env":
        return
    try:
        with open(_MODE_FILE, "w") as f:
            json.dump({"mode": mode}, f)
    except Exception as e:
        logger.warning("Failed to save canvas_mode.json: %s", e)


def get_mode() -> str:
    """Return the active canvas-store mode name."""
    global _mode
    if _mode is None:
        env_choice = os.environ.get("WEBAGENT_CANVAS_STORE", "").strip().lower()
        if env_choice in _AVAILABLE:
            _mode = env_choice
        else:
            _mode = _load_saved_mode() or "filesystem"
    return _mode


def set_mode(mode: str) -> None:
    """Switch the canvas store. Persists to disk and resets the cached instance."""
    global _store, _mode
    if mode not in _AVAILABLE:
        raise ValueError(f"Invalid mode '{mode}'. Must be one of {_AVAILABLE}")
    _mode = mode
    _store = None
    _save_mode(mode)
    logger.info("Canvas store switched to '%s'", mode)


def list_modes() -> list:
    return list(_AVAILABLE)


def _construct(mode: str) -> CanvasStore:
    if mode == "filesystem":
        from app.canvas_store.filesystem import FilesystemCanvasStore
        return FilesystemCanvasStore()
    if mode == "database":
        from app.canvas_store.database import DatabaseCanvasStore
        return DatabaseCanvasStore()
    if mode == "hybrid":
        from app.canvas_store.hybrid import HybridCanvasStore
        return HybridCanvasStore()
    raise ValueError(f"Unknown canvas store mode: {mode}")


get_canvases_store = get_pages_store = lambda: _get_store()


def _get_store() -> CanvasStore:
    """Return the active CanvasStore instance (lazy)."""
    global _store
    if _store is None:
        mode = get_mode()
        try:
            _store = _construct(mode)
            logger.info("Initialized canvas store: %s", mode)
        except Exception as e:
            logger.warning(
                "Failed to construct canvas store '%s' (%s); falling back to filesystem",
                mode, e,
            )
            from app.canvas_store.filesystem import FilesystemCanvasStore
            _store = FilesystemCanvasStore()
    return _store


def get_status() -> dict:
    """For UI: current mode + availability info."""
    return {
        "mode": get_mode(),
        "available": list(_AVAILABLE),
        "env_locked": os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env",
    }

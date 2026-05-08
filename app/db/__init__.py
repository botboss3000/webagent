"""
Database backend factory for webAgent.

Provides get_db() which returns the current StorageBackend instance
(Cloud/Supabase or Local/SQLite) based on the persisted mode.

Mode is stored in a small mode.json file so it survives restarts.
"""

import json
import logging
import os
from typing import Optional
from app.db.interface import StorageBackend

logger = logging.getLogger(__name__)

_db_instance: Optional[StorageBackend] = None
_db_mode: Optional[str] = None  # "cloud" or "local"

# Where we persist the mode choice
_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODE_FILE = os.path.join(_AGENT_DIR, "db_mode.json")


def _load_saved_mode() -> Optional[str]:
    """Load the persisted mode from disk."""
    try:
        if os.path.exists(_MODE_FILE):
            with open(_MODE_FILE, "r") as f:
                data = json.load(f)
                return data.get("mode")
    except Exception as e:
        logger.warning("Failed to load db_mode.json: %s", e)
    return None


def _save_mode(mode: str) -> None:
    """Persist the current mode to disk."""
    try:
        with open(_MODE_FILE, "w") as f:
            json.dump({"mode": mode}, f)
    except Exception as e:
        logger.warning("Failed to save db_mode.json: %s", e)


def get_mode() -> str:
    """
    Get the current database mode.
    
    Returns:
        "cloud" or "local"
    """
    global _db_mode
    if _db_mode is None:
        _db_mode = _load_saved_mode() or "cloud"
    return _db_mode


def set_db_mode(mode: str) -> None:
    """
    Switch the database mode.
    
    This resets the cached backend instance. The next call to get_db()
    will return the new backend. Mode is persisted to disk.
    
    Args:
        mode: "cloud" or "local"
    
    Raises:
        ValueError: If mode is not "cloud" or "local"
    """
    global _db_mode, _db_instance
    if mode not in ("cloud", "local"):
        raise ValueError(f"Invalid mode '{mode}'. Must be 'cloud' or 'local'.")
    
    _db_mode = mode
    _db_instance = None  # Force re-create on next get_db()
    _save_mode(mode)
    logger.info("Database mode switched to '%s'", mode)


def get_db() -> StorageBackend:
    """
    Get the current storage backend instance.
    
    Lazily creates the backend based on the current mode.
    
    Returns:
        StorageBackend implementation (SupabaseBackend or LocalBackend)
    """
    global _db_instance, _db_mode
    
    if _db_instance is None:
        mode = get_mode()
        if mode == "local":
            from app.db.local import LocalBackend
            _db_instance = LocalBackend()
            logger.info("Initialized LocalBackend (SQLite)")
        else:
            try:
                from app.db.supabase import SupabaseBackend
                _db_instance = SupabaseBackend()
                logger.info("Initialized SupabaseBackend (Cloud)")
            except Exception as e:
                logger.warning("Supabase init failed (%s), falling back to local", e)
                from app.db.local import LocalBackend
                _db_instance = LocalBackend()
                _db_mode = "local"
                logger.info("Fell back to LocalBackend (SQLite)")
    
    return _db_instance


async def get_db_stats() -> dict:
    """
    Get statistics about the current database.
    
    Returns:
        dict with mode and table row counts
    """
    db = get_db()
    raw = db.get_raw_client()
    
    tables = [
        "sessions", "interactions", "session_summaries", "context_documents",
        "context_templates", "memories", "memory_chunks", "memory_links", "memory_timeline",
        "tools", "agent_credentials", "skills",
    ]
    
    stats = {}
    for table in tables:
        try:
            result = raw.table(table).select("id", count="exact").limit(1).execute()
            # Supabase returns count in response, SQLite doesn't
            # Fallback: just count with a separate query
            if hasattr(result, 'count') and result.count is not None:
                stats[table] = result.count
            else:
                # Count all rows for local mode
                all_rows = raw.table(table).select("id").execute()
                stats[table] = len(all_rows.data) if all_rows.data else 0
        except Exception:
            stats[table] = -1  # Table doesn't exist or error
    
    # Try to get the actual file size for local mode
    db_path = ""
    if get_mode() == "local":
        try:
            from app.db.local import DEFAULT_DB_PATH
            db_path = DEFAULT_DB_PATH
            if os.path.exists(db_path):
                stats["db_size_bytes"] = os.path.getsize(db_path)
        except Exception:
            pass
    
    return {
        "mode": get_mode(),
        "backend": type(db).__name__,
        "tables": stats,
        "db_path": db_path,
    }

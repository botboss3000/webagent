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
    """Persist the current mode to disk (skip when env-locked for Cloud Run)."""
    if os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env":
        logger.info("Config is env-locked; skipping db_mode.json write")
        return
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

    Env override: WEBAGENT_DB_MODE wins when WEBAGENT_CONFIG_SOURCE=env.
    """
    global _db_mode
    if _db_mode is None:
        if os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env":
            env_mode = os.environ.get("WEBAGENT_DB_MODE", "").strip().lower()
            if env_mode in ("cloud", "local"):
                _db_mode = env_mode
                return _db_mode
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


def _maybe_wrap_encryption(backend: StorageBackend) -> StorageBackend:
    """
    If the active encryption level is non-trivial, wrap the backend in an
    EncryptedStorageBackend so sensitive fields are transparently encrypted
    on write and decrypted on read.
    """
    try:
        from app.encryption import get_level, get_encryption
        level = get_level()
        if level == "none":
            return backend
        from app.db.interface import EncryptedStorageBackend
        enc = get_encryption()
        wrapped = EncryptedStorageBackend(backend, enc)
        logger.info("Wrapped backend with EncryptedStorageBackend (level=%s)", level)
        return wrapped
    except Exception as e:
        logger.warning("Encryption wrap failed (%s); using backend unwrapped", e)
        return backend


def reset_db_instance() -> None:
    """Drop the cached backend so the next get_db() rebuilds it.

    Used after changing the encryption level so the wrapper is re-evaluated.
    """
    global _db_instance
    _db_instance = None


def get_db() -> StorageBackend:
    """
    Get the current storage backend instance.

    Lazily creates the backend based on the current mode.

    Returns:
        StorageBackend implementation (SupabaseBackend or LocalBackend), possibly
        wrapped by EncryptedStorageBackend when encryption is enabled.
    """
    global _db_instance, _db_mode

    if _db_instance is None:
        mode = get_mode()
        if mode == "local":
            from app.db.local import LocalBackend
            base = LocalBackend()
            logger.info("Initialized LocalBackend (SQLite)")
        else:
            _supa_url = os.environ.get("SUPABASE_URL", "")
            _supa_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
            if not _supa_url or not _supa_key:
                logger.info(
                    "Database mode is 'cloud' but Supabase credentials are not configured "
                    "— using local backend until credentials are set in the Database tab"
                )
                from app.db.local import LocalBackend
                base = LocalBackend()
            else:
                try:
                    from app.db.supabase import SupabaseBackend
                    base = SupabaseBackend()
                    logger.info("Initialized SupabaseBackend (Cloud)")
                except Exception as e:
                    logger.warning("Supabase init failed (%s), falling back to local", e)
                    from app.db.local import LocalBackend
                    base = LocalBackend()
                    _db_mode = "local"
                    logger.info("Fell back to LocalBackend (SQLite)")
        _db_instance = _maybe_wrap_encryption(base)

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
        "sessions", "interactions", "session_summaries", "agent_prompts",
        "memories", "memory_chunks", "memory_links", "memory_timeline",
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

"""
File storage backends for attachment uploads.

Dispatches to local filesystem or Supabase Storage based on app DB mode.
"""

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

from app.db import get_mode

logger = logging.getLogger(__name__)

# Where files land on disk in local mode
_LOCAL_UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "uploads"))


def _make_storage_name(filename: str) -> str:
    """Generate a unique storage filename while preserving extension."""
    ext = ""
    if "." in filename:
        e = filename.rsplit(".", 1)[1]
        ext = f".{e.lower()}"
    return f"{uuid.uuid4().hex}{ext}"


def _format_size(bytes_val: int) -> str:
    """Human-readable size."""
    if bytes_val < 1024:
        return f"{bytes_val}B"
    elif bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f}KB"
    else:
        return f"{bytes_val / (1024 * 1024):.1f}MB"


# ── Public API ──────────────────────────────────────────────────────────────


async def store_file(
    user_id: str,
    session_id: str,
    file_bytes: bytes,
    filename: str,
    mime_type: str,
) -> dict:
    """
    Store a file's bytes persistently.

    Args:
        user_id: Owning user.
        session_id: Session this attachment belongs to.
        file_bytes: Raw file content.
        filename: Original filename (used to derive extension).
        mime_type: Content type (e.g. "image/jpeg").

    Returns:
        dict with keys:
          - storage_path (str): opaque path ref saved in attachments table
          - public_url   (str): URL the frontend uses to display/download the file

    Raises:
        NotImplementedError: If DB mode is "cloud" (Supabase Storage not yet wired).
        IOError: If file cannot be written.
    """
    mode = get_mode()
    if mode == "local":
        return _store_local(user_id, file_bytes, filename)
    elif mode == "cloud":
        raise NotImplementedError("Supabase Storage not yet wired — use local mode")
    else:
        raise ValueError(f"Unknown DB mode: {mode}")


async def read_file(storage_path: str) -> Optional[bytes]:
    """
    Read a stored file's bytes.

    Args:
        storage_path: The opaque path returned from store_file().

    Returns:
        Raw file bytes, or None if the file does not exist.
    """
    mode = get_mode()
    if mode == "local":
        return _read_local(storage_path)
    elif mode == "cloud":
        raise NotImplementedError("Supabase Storage not yet wired — use local mode")
    else:
        raise ValueError(f"Unknown DB mode: {mode}")


async def delete_file(storage_path: str) -> bool:
    """
    Delete a stored file.

    Args:
        storage_path: The opaque path returned from store_file().

    Returns:
        True if the file was deleted, False if it didn't exist.
    """
    mode = get_mode()
    if mode == "local":
        return _delete_local(storage_path)
    elif mode == "cloud":
        raise NotImplementedError("Supabase Storage not yet wired — use local mode")
    else:
        raise ValueError(f"Unknown DB mode: {mode}")


# ── Local filesystem backend ────────────────────────────────────────────────


def _store_local(user_id: str, file_bytes: bytes, filename: str) -> dict:
    """Save file to uploads/{user_id}/{uuid}.ext."""
    storage_name = _make_storage_name(filename)
    rel_path = f"{user_id}/{storage_name}"

    dest = _LOCAL_UPLOAD_DIR / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        dest.write_bytes(file_bytes)
    except OSError as e:
        logger.error("Failed to write file %s: %s", dest, e)
        raise IOError(f"Cannot write upload: {e}") from e

    size_str = _format_size(len(file_bytes))
    logger.debug("Stored file locally: %s (%s)", rel_path, size_str)

    return {
        "storage_path": rel_path,
        "public_url": f"/uploads/{rel_path}",
    }


def _read_local(storage_path: str) -> Optional[bytes]:
    """Read file bytes from uploads/{storage_path}."""
    path = _LOCAL_UPLOAD_DIR / storage_path
    if not path.exists():
        logger.warning("File not found: %s", path)
        return None
    try:
        return path.read_bytes()
    except OSError as e:
        logger.error("Failed to read file %s: %s", path, e)
        return None


def _delete_local(storage_path: str) -> bool:
    """Delete file from uploads/{storage_path}."""
    path = _LOCAL_UPLOAD_DIR / storage_path
    if not path.exists():
        return False
    try:
        path.unlink()
        logger.debug("Deleted file: %s", storage_path)
        return True
    except OSError as e:
        logger.error("Failed to delete file %s: %s", path, e)
        return False

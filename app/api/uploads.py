"""Upload endpoint for attachments (images, voice, files)."""

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse

from app.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/upload", tags=["upload"])

# ── Config ──
_MAX_SIZE_MB = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "25"))
_MAX_SIZE_BYTES = _MAX_SIZE_MB * 1024 * 1024
_UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "uploads"))

_ALLOWED_MIME_PREFIXES = (
    "image/",     # jpeg, png, gif, webp, svg
    "audio/",     # webm, wav, mp3, ogg, m4a
    "video/",     # mp4, webm
    "application/pdf",
    "text/plain",
)


def _ensure_upload_dir(user_id: str) -> Path:
    """Create per-user upload directory if needed."""
    user_dir = _UPLOAD_DIR / user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


@router.post("")
async def upload_file(
    file: UploadFile = File(...),
    user_id: str = Form(...),
    session_id: str = Form(...),
):
    """
    Upload a file attachment.

    Accepts multipart/form-data with:
      - file: the file to upload
      - user_id: owning user id
      - session_id: session this attachment belongs to

    Returns attachment metadata including id and serving URL.
    """
    # Validate file
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    # Check mime type
    mime_type = file.content_type or "application/octet-stream"
    if not mime_type.startswith(_ALLOWED_MIME_PREFIXES):
        raise HTTPException(
            status_code=400,
            detail=f"File type '{mime_type}' not allowed. Allowed: image/*, audio/*, video/*, application/pdf, text/plain",
        )

    # Check file size (read first bytes to detect oversized)
    contents = await file.read()
    file_size = len(contents)

    if file_size > _MAX_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max {_MAX_SIZE_MB}MB. Got {file_size / 1024 / 1024:.1f}MB",
        )

    # Generate unique storage path
    ext = ""
    if "." in file.filename:
        ext = file.filename.rsplit(".", 1)[1]
        ext = f".{ext.lower()}"
    storage_name = f"{uuid.uuid4().hex}{ext}"
    storage_rel = f"{user_id}/{storage_name}"

    user_dir = _ensure_upload_dir(user_id)
    dest = user_dir / storage_name

    # Write file
    with open(dest, "wb") as f:
        f.write(contents)

    # Compute optional metadata (e.g. audio duration placeholder)
    meta = {}
    if mime_type.startswith("audio/"):
        meta["encoding"] = "recorded"
    elif mime_type.startswith("image/"):
        meta["capture"] = "uploaded"

    # Insert DB record
    try:
        db = get_db()
        att_id = await db.insert_attachment(
            user_id=user_id,
            session_id=session_id,
            original_name=file.filename,
            mime_type=mime_type,
            size_bytes=file_size,
            storage_path=storage_rel,
            metadata=meta,
        )
    except Exception as e:
        # Clean up file on DB failure
        if dest.exists():
            dest.unlink()
        logger.error(f"Failed to insert attachment record: {e}")
        raise HTTPException(status_code=500, detail="Failed to store attachment metadata")

    return {
        "attachment_id": att_id,
        "url": f"/uploads/{storage_rel}",
        "original_name": file.filename,
        "mime_type": mime_type,
        "size_bytes": file_size,
    }


@router.get("/{attachment_id}")
async def get_attachment_meta(attachment_id: str):
    """Get attachment metadata."""
    db = get_db()
    att = await db.get_attachment(attachment_id)
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")
    return att


@router.delete("/{attachment_id}")
async def delete_attachment(attachment_id: str):
    """Delete an attachment and its file."""
    db = get_db()
    att = await db.get_attachment(attachment_id)
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")

    # Delete file
    file_path = _UPLOAD_DIR / att["storage_path"]
    if file_path.exists():
        file_path.unlink()

    # Delete DB record
    await db.delete_attachment(attachment_id)
    return {"status": "deleted", "attachment_id": attachment_id}

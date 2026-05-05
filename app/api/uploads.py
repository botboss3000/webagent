"""Upload endpoint for attachments (images, voice, files).

File bytes are stored via app/db/attachments/ (local filesystem or Supabase Storage).
"""

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form

from app.db import get_db
from app.db.attachments import store_file, read_file, delete_file as storage_delete

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/upload", tags=["upload"])

# ── Config ──
_MAX_SIZE_MB = int(os.environ.get("MAX_UPLOAD_SIZE_MB", "25"))
_MAX_SIZE_BYTES = _MAX_SIZE_MB * 1024 * 1024

_ALLOWED_MIME_PREFIXES = (
    "image/",     # jpeg, png, gif, webp, svg
    "audio/",     # webm, wav, mp3, ogg, m4a
    "video/",     # mp4, webm
    "application/pdf",
    "text/plain",
)


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
    if not file.filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    # Validate mime type
    mime_type = file.content_type or "application/octet-stream"
    if not mime_type.startswith(_ALLOWED_MIME_PREFIXES):
        raise HTTPException(
            status_code=400,
            detail=f"File type '{mime_type}' not allowed. Allowed: image/*, audio/*, video/*, application/pdf, text/plain",
        )

    # Read file and check size
    contents = await file.read()
    file_size = len(contents)

    if file_size > _MAX_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max {_MAX_SIZE_MB}MB. Got {file_size / 1024 / 1024:.1f}MB",
        )

    # Store bytes via the attachment storage layer (local FS or cloud)
    try:
        result = await store_file(
            user_id=user_id,
            session_id=session_id,
            file_bytes=contents,
            filename=file.filename,
            mime_type=mime_type,
        )
    except NotImplementedError:
        raise HTTPException(status_code=501, detail="File storage not configured for current DB mode")
    except IOError as e:
        raise HTTPException(status_code=500, detail=f"Failed to store file: {e}")

    # Compute optional metadata hints
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
            storage_path=result["storage_path"],
            metadata=meta,
        )
    except Exception as e:
        # Clean up file on DB failure
        await storage_delete(result["storage_path"])
        logger.error(f"Failed to insert attachment record: {e}")
        raise HTTPException(status_code=500, detail="Failed to store attachment metadata")

    return {
        "attachment_id": att_id,
        "url": result["public_url"],
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

    # Delete bytes
    await storage_delete(att["storage_path"])
    # Delete DB record
    await db.delete_attachment(attachment_id)
    return {"status": "deleted", "attachment_id": attachment_id}

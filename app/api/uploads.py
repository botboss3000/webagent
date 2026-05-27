"""Upload endpoint for attachments (images, voice, files).

File bytes are stored via the active backend in `app/attachments_store/`.

Two flows:
  1. Default — multipart/form-data with `file=`. Bytes hit the server, are
     written to the active backend, and a row is inserted.
  2. Browser backend — the frontend keeps bytes in IndexedDB and POSTs only
     metadata (original_name, mime_type, size_bytes, optional blob_key) so
     the server can still insert an attachment row and return an id. The
     row's storage_provider = "browser" and storage_path = "indexeddb://{id}".
"""

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form

from app.db import get_db
from app.db.attachments import (
    store_file,
    read_file,
    delete_file as storage_delete,
    active_backend_name,
)

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


def _validate_mime(mime_type: str) -> None:
    if not mime_type.startswith(_ALLOWED_MIME_PREFIXES):
        raise HTTPException(
            status_code=400,
            detail=(
                f"File type '{mime_type}' not allowed. "
                "Allowed: image/*, audio/*, video/*, application/pdf, text/plain"
            ),
        )


@router.post("")
async def upload_file(
    file: Optional[UploadFile] = File(None),
    user_id: str = Form(...),
    session_id: str = Form(...),
    original_name: Optional[str] = Form(None),
    mime_type: Optional[str] = Form(None),
    size_bytes: Optional[int] = Form(None),
    blob_key: Optional[str] = Form(None),
):
    """
    Upload a file attachment.

    Normal mode (any backend except `browser`): provide `file=` multipart bytes.
    Browser mode: omit `file=`; provide `original_name`, `mime_type`, `size_bytes`,
    and `blob_key` (the IndexedDB key the browser will store under). Server
    inserts metadata only.
    """
    backend = active_backend_name()

    # ── Browser backend: metadata-only path ──
    if backend == "browser" and file is None:
        if not original_name or not mime_type or size_bytes is None:
            raise HTTPException(
                status_code=400,
                detail="Browser backend requires original_name, mime_type, size_bytes.",
            )
        _validate_mime(mime_type)
        if size_bytes > _MAX_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Max {_MAX_SIZE_MB}MB. Got {size_bytes / 1024 / 1024:.1f}MB",
            )

        meta = {"browser_blob_key": blob_key} if blob_key else {}
        if mime_type.startswith("audio/"):
            meta["encoding"] = "recorded"
        elif mime_type.startswith("image/"):
            meta["capture"] = "uploaded"

        # Synthetic storage_path keyed by a fresh id; the row id we assign
        # *is* the IndexedDB lookup key on the client.
        db = get_db()
        # We need the att_id first to make the storage_path self-referential.
        # insert_attachment generates its own id; cheapest path is to insert
        # with a placeholder storage_path, then read it back.
        att_id = await db.insert_attachment(
            user_id=user_id,
            session_id=session_id,
            original_name=original_name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            storage_path="indexeddb://pending",
            metadata=meta,
            storage_provider="browser",
        )
        # Best-effort update of the storage_path to embed the id. If the DB
        # backend doesn't support a generic update method here, the
        # placeholder is fine — the client uses attachment_id as its IDB key.
        try:
            from sqlite3 import Connection  # noqa: F401
            raw = db.get_raw_client() if hasattr(db, "get_raw_client") else None
            if raw is not None:
                raw.table("attachments").update({"storage_path": f"indexeddb://{att_id}"}).eq("id", att_id).execute()
        except Exception:
            pass

        return {
            "attachment_id": att_id,
            "url": "",
            "original_name": original_name,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "storage_provider": "browser",
            "browser_blob_key": blob_key or att_id,
        }

    # ── Normal multipart path ──
    if file is None or not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    if backend == "browser":
        # Multipart upload with bytes while the browser backend is active: the
        # bytes have nowhere to live server-side. Reject so the client falls
        # back to the metadata-only path.
        raise HTTPException(
            status_code=409,
            detail="Browser backend is active — send metadata only (omit `file=`).",
        )

    mime_type_eff = file.content_type or mime_type or "application/octet-stream"
    _validate_mime(mime_type_eff)

    contents = await file.read()
    file_size = len(contents)

    if file_size > _MAX_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Max {_MAX_SIZE_MB}MB. Got {file_size / 1024 / 1024:.1f}MB",
        )

    # Store bytes via the active backend.
    try:
        result = await store_file(
            user_id=user_id,
            session_id=session_id,
            file_bytes=contents,
            filename=file.filename,
            mime_type=mime_type_eff,
        )
    except RuntimeError as e:
        # Backend not configured (missing SUPABASE_URL, no boto3, etc.).
        raise HTTPException(status_code=501, detail=f"Storage backend not ready: {e}")
    except IOError as e:
        raise HTTPException(status_code=500, detail=f"Failed to store file: {e}")

    # Metadata hints
    meta = {}
    if mime_type_eff.startswith("audio/"):
        meta["encoding"] = "recorded"
    elif mime_type_eff.startswith("image/"):
        meta["capture"] = "uploaded"

    provider = result.get("storage_provider", backend)

    # Insert DB record
    try:
        db = get_db()
        att_id = await db.insert_attachment(
            user_id=user_id,
            session_id=session_id,
            original_name=file.filename,
            mime_type=mime_type_eff,
            size_bytes=file_size,
            storage_path=result["storage_path"],
            metadata=meta,
            storage_provider=provider,
        )
    except Exception as e:
        # Clean up the file on DB failure.
        await storage_delete(result["storage_path"], storage_provider=provider)
        logger.error(f"Failed to insert attachment record: {e}")
        raise HTTPException(status_code=500, detail="Failed to store attachment metadata")

    return {
        "attachment_id": att_id,
        "url": result["public_url"],
        "original_name": file.filename,
        "mime_type": mime_type_eff,
        "size_bytes": file_size,
        "storage_provider": provider,
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

    provider = att.get("storage_provider") or "local"
    if provider != "browser":
        await storage_delete(att["storage_path"], storage_provider=provider)
    await db.delete_attachment(attachment_id)
    return {"status": "deleted", "attachment_id": attachment_id}

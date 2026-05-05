"""
Tool for reading attachment content (images, text files, PDFs).

Registered dynamically so the agent can call read_attachment(attachment_id)
to inspect files the user uploaded.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from app.db import get_db

logger = logging.getLogger(__name__)

_UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "uploads"))


TOOL_DEFINITION = {
    "name": "read_attachment",
    "description": (
        "Read the contents of a file the user attached to the conversation. "
        "For text files, returns the text content. For images, returns a "
        "description (via OCR placeholder). For audio, returns metadata about the recording. "
        "Returns the attachment info including original filename, type, size, and available content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "attachment_id": {
                "type": "string",
                "description": "The attachment ID (e.g., from the USER ATTACHMENTS section in the system prompt)."
            }
        },
        "required": ["attachment_id"]
    },
}


async def read_attachment(attachment_id: str) -> str:
    """
    Read content from an uploaded attachment.

    Args:
        attachment_id: The UUID of the attachment to read.

    Returns:
        A string with the file's content or metadata description.
    """
    db = get_db()
    att = await db.get_attachment(attachment_id)
    if not att:
        return json.dumps({
            "status": "error",
            "message": f"Attachment '{attachment_id}' not found.",
        })

    mime = att["mime_type"]
    path = _UPLOAD_DIR / att["storage_path"]

    if not path.exists():
        return json.dumps({
            "status": "error",
            "message": f"File not found on disk: {att['storage_path']}",
        })

    size_str = _format_size(att["size_bytes"])

    # ── Text files ──
    if mime in ("text/plain", "text/markdown", "text/csv", "text/html", "application/json"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            # Truncate to avoid blowing the context
            max_chars = 10000
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n\n... [truncated at {max_chars} chars]"
            return json.dumps({
                "status": "ok",
                "attachment_id": attachment_id,
                "original_name": att["original_name"],
                "mime_type": mime,
                "size": size_str,
                "content": text,
            }, indent=2)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"Failed to read text file: {e}",
            })

    # ── Images (placeholder — real OCR requires tesseract or vision model) ──
    elif mime.startswith("image/"):
        return json.dumps({
            "status": "ok",
            "attachment_id": attachment_id,
            "original_name": att["original_name"],
            "mime_type": mime,
            "size": size_str,
            "content": (
                f"[Image file: {att['original_name']} ({mime}, {size_str})]\n"
                f"The image is accessible at: /uploads/{att['storage_path']}\n"
                "Note: Local OCR is not yet available. If you have vision capabilities, "
                "describe the image to the user based on its filename and metadata."
            ),
        }, indent=2)

    # ── Audio ──
    elif mime.startswith("audio/"):
        meta = {}
        try:
            meta = json.loads(att.get("metadata", "{}"))
        except json.JSONDecodeError:
            pass
        duration = meta.get("duration_seconds", "unknown")
        return json.dumps({
            "status": "ok",
            "attachment_id": attachment_id,
            "original_name": att["original_name"],
            "mime_type": mime,
            "size": size_str,
            "duration_seconds": duration,
            "content": (
                f"[Audio file: {att['original_name']} ({mime}, {size_str})]\n"
                f"Playable at: /uploads/{att['storage_path']}\n"
                "Transcription not yet available. Summarize based on filename."
            ),
        }, indent=2)

    # ── PDF / others ──
    elif mime == "application/pdf":
        return json.dumps({
            "status": "ok",
            "attachment_id": attachment_id,
            "original_name": att["original_name"],
            "mime_type": mime,
            "size": size_str,
            "content": (
                f"[PDF file: {att['original_name']} ({size_str})]\n"
                f"Downloadable at: /uploads/{att['storage_path']}\n"
                "PDF text extraction not yet available."
            ),
        }, indent=2)

    # ── Fallback ──
    else:
        return json.dumps({
            "status": "ok",
            "attachment_id": attachment_id,
            "original_name": att["original_name"],
            "mime_type": mime,
            "size": size_str,
            "content": f"[File: {att['original_name']} ({mime}, {size_str})]",
        }, indent=2)


def _format_size(bytes_val: int) -> str:
    if bytes_val < 1024:
        return f"{bytes_val}B"
    elif bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f}KB"
    else:
        return f"{bytes_val / (1024 * 1024):.1f}MB"

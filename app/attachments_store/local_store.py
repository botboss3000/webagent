"""Local filesystem attachment backend.

Files land at $UPLOAD_DIR/{user_id}/{uuid}.ext (UPLOAD_DIR defaults to
`uploads`). Served by the /uploads StaticFiles mount in main.py.
"""

import logging
import os
import uuid
from pathlib import Path
from typing import Optional

from app.attachments_store.interface import AttachmentStore

logger = logging.getLogger(__name__)


def _make_storage_name(filename: str) -> str:
    ext = ""
    if "." in filename:
        ext = f".{filename.rsplit('.', 1)[1].lower()}"
    return f"{uuid.uuid4().hex}{ext}"


class LocalAttachmentStore(AttachmentStore):
    name = "local"

    def __init__(self, upload_dir: Optional[str] = None):
        self._dir = Path(upload_dir or os.environ.get("UPLOAD_DIR", "uploads"))

    async def store(self, user_id: str, file_bytes: bytes, filename: str, mime_type: str) -> dict:
        rel_path = f"{user_id}/{_make_storage_name(filename)}"
        dest = self._dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.write_bytes(file_bytes)
        except OSError as e:
            logger.error("Failed to write %s: %s", dest, e)
            raise IOError(f"Cannot write upload: {e}") from e
        return {"storage_path": rel_path, "public_url": f"/uploads/{rel_path}"}

    async def read(self, storage_path: str) -> Optional[bytes]:
        path = self._dir / storage_path
        if not path.exists():
            return None
        try:
            return path.read_bytes()
        except OSError as e:
            logger.error("Failed to read %s: %s", path, e)
            return None

    async def delete(self, storage_path: str) -> bool:
        path = self._dir / storage_path
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except OSError as e:
            logger.error("Failed to delete %s: %s", path, e)
            return False

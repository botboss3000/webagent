"""Local filesystem attachment backend.

Inbound attachments land in the user's own data home:
``data/user_data/{user_id}/uploads/{uuid}.ext`` (see app/user_workspace.py),
served by the /user_data StaticFiles mount in main.py. The ``storage_path``
recorded on each row is ``{user_id}/uploads/{uuid}.ext`` (relative to the
user-data base), so read/delete/public_url all resolve against that base.

``UPLOAD_DIR`` (or ``WEBAGENT_USER_DATA_DIR``) still overrides the base dir.
"""

import logging
import os
import uuid
from pathlib import Path
from typing import Optional

from app import user_workspace as ws
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
        # Base = the per-user data home root. An explicit upload_dir or the legacy
        # UPLOAD_DIR env still wins (so a custom mount point keeps working).
        override = upload_dir or os.environ.get("UPLOAD_DIR", "").strip()
        self._dir = Path(override) if override else ws.base_dir()

    async def store(self, user_id: str, file_bytes: bytes, filename: str, mime_type: str) -> dict:
        safe_uid = ws.safe_segment(user_id)
        rel_path = f"{safe_uid}/uploads/{_make_storage_name(filename)}"
        dest = self._dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            dest.write_bytes(file_bytes)
        except OSError as e:
            logger.error("Failed to write %s: %s", dest, e)
            raise IOError(f"Cannot write upload: {e}") from e
        return {"storage_path": rel_path, "public_url": f"/user_data/{rel_path}"}

    def public_url(self, storage_path: str) -> str:
        """Serve from the /user_data mount (storage_path already includes the
        user-id/uploads/ prefix)."""
        return f"/user_data/{storage_path}"

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

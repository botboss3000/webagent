"""Supabase Storage attachment backend.

Uses the same Supabase project as the active SupabaseBackend (DB). The
bucket name is configurable; default `attachments`. The bucket must exist
and be readable per the URL signing strategy chosen (public or signed).
"""

import logging
import os
import uuid
from typing import Optional

from app.attachments_store.interface import AttachmentStore

logger = logging.getLogger(__name__)


def _make_object_name(filename: str) -> str:
    ext = ""
    if "." in filename:
        ext = f".{filename.rsplit('.', 1)[1].lower()}"
    return f"{uuid.uuid4().hex}{ext}"


class SupabaseAttachmentStore(AttachmentStore):
    name = "supabase"

    def __init__(self, bucket: Optional[str] = None, public: bool = True):
        self._bucket = bucket or os.environ.get("SUPABASE_ATTACHMENTS_BUCKET", "attachments")
        self._public = public
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            from supabase import create_client
        except ImportError as e:
            raise RuntimeError(
                "supabase-py is not installed. Add `supabase` to requirements.txt."
            ) from e
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set to use Supabase Storage."
            )
        self._client = create_client(url, key)
        return self._client

    async def store(self, user_id: str, file_bytes: bytes, filename: str, mime_type: str) -> dict:
        client = self._get_client()
        object_path = f"{user_id}/{_make_object_name(filename)}"
        try:
            client.storage.from_(self._bucket).upload(
                path=object_path,
                file=file_bytes,
                file_options={"content-type": mime_type, "upsert": "false"},
            )
        except Exception as e:
            logger.error("Supabase upload failed: %s", e)
            raise IOError(f"Supabase upload failed: {e}") from e

        if self._public:
            url = client.storage.from_(self._bucket).get_public_url(object_path)
        else:
            signed = client.storage.from_(self._bucket).create_signed_url(object_path, 60 * 60 * 24 * 7)
            url = signed.get("signedURL") or signed.get("signed_url") or ""

        return {"storage_path": object_path, "public_url": url}

    async def read(self, storage_path: str) -> Optional[bytes]:
        client = self._get_client()
        try:
            return client.storage.from_(self._bucket).download(storage_path)
        except Exception as e:
            logger.warning("Supabase download failed for %s: %s", storage_path, e)
            return None

    async def delete(self, storage_path: str) -> bool:
        client = self._get_client()
        try:
            client.storage.from_(self._bucket).remove([storage_path])
            return True
        except Exception as e:
            logger.warning("Supabase delete failed for %s: %s", storage_path, e)
            return False

    def public_url(self, storage_path: str) -> str:
        try:
            client = self._get_client()
            if self._public:
                return client.storage.from_(self._bucket).get_public_url(storage_path)
            signed = client.storage.from_(self._bucket).create_signed_url(storage_path, 60 * 60 * 24 * 7)
            return signed.get("signedURL") or signed.get("signed_url") or ""
        except Exception:
            return ""

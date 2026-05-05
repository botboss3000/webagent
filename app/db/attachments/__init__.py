"""File storage abstraction for attachments (images, voice, docs).

Delegates to the appropriate backend based on DB mode:
  - local  → filesystem (uploads/ directory)
  - cloud  → Supabase Storage (not yet wired — raises NotImplementedError)

Usage:
    from app.db.attachments import store_file, read_file, delete_file

    result = await store_file(user_id, session_id, file_bytes, filename, mime_type)
    # → { storage_path: "user_id/uuid.ext", public_url: "/uploads/user_id/uuid.ext" }

    contents = await read_file(storage_path)
    # → bytes | None

    await delete_file(storage_path)
"""

from .file_store import store_file, read_file, delete_file

__all__ = ["store_file", "read_file", "delete_file"]

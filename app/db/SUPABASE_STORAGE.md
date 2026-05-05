# Supabase Storage Implementation

## Overview

File bytes currently live on the local filesystem (`uploads/` dir). To deploy
with Supabase (cloud mode), swap the local file I/O in
`app/db/attachments/file_store.py` for Supabase Storage API calls.

Only one file changes — `app/db/attachments/file_store.py`. Everything else
(upload endpoint, read_attachment tool, frontend) uses the `store_file()` /
`read_file()` / `delete_file()` abstraction and needs zero edits.

## Prerequisites

1. A Supabase project with Storage enabled
2. A Storage bucket named `attachments` (create in Supabase Dashboard → Storage → New bucket)
3. The bucket must allow public reads OR you use signed URLs (recommended for private files)
4. `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` already in `.env` (required for cloud DB mode)
5. Optional: set `SUPABASE_STORAGE_BUCKET=attachments` in `.env` (defaults to `attachments`)

## Implementation

### 1. Add storage helpers to `file_store.py`

```python
import os
import uuid

SUPABASE_STORAGE_BUCKET = os.environ.get("SUPABASE_STORAGE_BUCKET", "attachments")


async def _store_supabase(user_id: str, file_bytes: bytes, filename: str) -> dict:
    """Upload file to Supabase Storage bucket.

    Returns:
        dict with storage_path (opaque ref for DB column) and public_url.
    """
    from supabase import create_client

    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    client = create_client(url, key)

    ext = ""
    if "." in filename:
        e = filename.rsplit(".", 1)[1]
        ext = f".{e.lower()}"
    storage_name = f"{uuid.uuid4().hex}{ext}"
    storage_path = f"{user_id}/{storage_name}"

    # Upload
    client.storage.from_(SUPABASE_STORAGE_BUCKET).upload(
        path=storage_path,
        file=file_bytes,
        file_options={"content-type": mime_type},
    )

    # Get public URL
    public_url = client.storage.from_(SUPABASE_STORAGE_BUCKET).get_public_url(storage_path)

    return {
        "storage_path": storage_path,
        "public_url": public_url,
    }


async def _read_supabase(storage_path: str) -> Optional[bytes]:
    """Download file bytes from Supabase Storage."""
    from supabase import create_client

    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    client = create_client(url, key)

    try:
        res = client.storage.from_(SUPABASE_STORAGE_BUCKET).download(storage_path)
        return res
    except Exception as e:
        logger.error("Failed to download %s from Supabase Storage: %s", storage_path, e)
        return None


async def _delete_supabase(storage_path: str) -> bool:
    """Remove file from Supabase Storage."""
    from supabase import create_client

    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    client = create_client(url, key)

    try:
        client.storage.from_(SUPABASE_STORAGE_BUCKET).remove([storage_path])
        return True
    except Exception as e:
        logger.error("Failed to delete %s from Supabase Storage: %s", storage_path, e)
        return False
```

### 2. Wire into dispatch functions

In `file_store.py`, add `elif mode == "cloud"` branches:

```python
# In store_file():
elif mode == "cloud":
    return await _store_supabase(user_id, file_bytes, filename)

# In read_file():
elif mode == "cloud":
    return await _read_supabase(storage_path)

# In delete_file():
elif mode == "cloud":
    return await _delete_supabase(storage_path)
```

Replace the current `raise NotImplementedError(...)` stubs.

## Filesystem cleanup

After switching to cloud mode, you can remove:

- `app/main.py` — the `/uploads` StaticFiles mount (no longer needed; files served via Supabase CDN)
- Root `uploads/` directory and `.gitkeep` (if no longer used in local mode)

Or keep both in parallel — local mode still writes to disk, cloud mode hits Supabase.

## RLS / Security (optional)

For private attachments, use signed URLs instead of public bucket:

```python
# Generate a signed URL that expires in 60 minutes
signed_url = client.storage.from_(bucket).create_signed_url(storage_path, expires_in=3600)
```

If using signed URLs, the frontend will need to refresh them when they expire.
Simplest approach for MVP: make the bucket public and rely on the `session_id`
check in the API layer.

## Test the switch

1. Set `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` in `.env`
2. Create bucket "attachments" in Supabase Dashboard → Storage
3. Set bucket privacy to "Public" (or configure RLS)
4. Switch to cloud mode: `POST /api/v1/db/cloud` (or edit `app/db_mode.json` to `{"mode": "cloud"}`)
5. Upload a file via the UI 📎 button — should return a Supabase CDN URL
6. Verify the read_attachment tool works
7. Verify the image/audio renders in the chat bubble

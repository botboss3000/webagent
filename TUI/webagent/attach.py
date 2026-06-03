"""Image attachments for the Server Manager chat.

The TUI talks to an OpenAI-compatible ``/chat/completions`` endpoint (see
``llm.py``), which accepts a *multimodal* user message: a list of parts where
each part is either ``{"type": "text", ...}`` or
``{"type": "image_url", "image_url": {"url": "data:<mime>;base64,<…>"}}``. This
module turns a pasted clipboard image — or a dragged-in image file — into a
saved file on disk plus the metadata the rest of the app needs.

Files are copied into a per-session folder under the manager's data dir so the
conversation history can be replayed later (we store only the *path + mime* in
the DB, then re-read the file and base64-encode it at send time — keeping the
SQLite store small). Everything is best-effort: a failure returns ``None`` and
the caller falls back to plain text.
"""

from __future__ import annotations

import base64
import shutil
import uuid
from pathlib import Path
from typing import Any, Optional

from .config import data_dir

# Formats the common vision models accept. ``.bmp`` is kept as a last-resort
# fallback for a Windows clipboard that only offered a device-independent
# bitmap; most providers handle the common four.
_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}
_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}

# Guard against pasting something enormous into a request body.
MAX_BYTES = 20 * 1024 * 1024  # 20 MB


def attach_dir(session_id: str) -> Path:
    d = data_dir() / "attachments" / (session_id or "default")
    d.mkdir(parents=True, exist_ok=True)
    return d


def mime_for_path(p: str | Path) -> Optional[str]:
    return _EXT_MIME.get(Path(p).suffix.lower())


def is_image_path(p: str | Path) -> bool:
    try:
        path = Path(p)
    except Exception:
        return False
    return path.suffix.lower() in _EXT_MIME and path.is_file()


def _unique(dirpath: Path, name: str) -> Path:
    target = dirpath / name
    if not target.exists():
        return target
    stem, suffix = Path(name).stem, Path(name).suffix
    return dirpath / f"{stem}-{uuid.uuid4().hex[:6]}{suffix}"


def save_bytes(
    data: bytes, mime: str, session_id: str, name: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Persist raw image bytes into the session folder. Returns an attachment
    dict ``{id, path, mime, name}`` or ``None`` on failure / oversize."""
    if not data or len(data) > MAX_BYTES:
        return None
    ext = _MIME_EXT.get(mime, ".png")
    aid = uuid.uuid4().hex[:8]
    fname = name or f"pasted-{aid}{ext}"
    if not Path(fname).suffix:
        fname += ext
    try:
        path = _unique(attach_dir(session_id), fname)
        path.write_bytes(data)
    except OSError:
        return None
    return {"id": aid, "path": str(path), "mime": mime, "name": path.name}


def save_path(src: str | Path, session_id: str) -> Optional[dict[str, Any]]:
    """Copy an existing image file into the session folder (drag-drop / typed
    path). Returns an attachment dict or ``None`` if it isn't a usable image."""
    src = Path(str(src).strip().strip('"').strip("'"))
    mime = mime_for_path(src)
    if mime is None or not src.is_file():
        return None
    try:
        if src.stat().st_size > MAX_BYTES:
            return None
        dest = _unique(attach_dir(session_id), src.name)
        shutil.copyfile(src, dest)
    except OSError:
        return None
    return {"id": uuid.uuid4().hex[:8], "path": str(dest), "mime": mime, "name": dest.name}


def data_url(att: dict[str, Any]) -> Optional[str]:
    """Read an attachment's file and return a base64 ``data:`` URL, or ``None``
    if the file is gone (history pointing at a since-deleted image)."""
    try:
        raw = Path(att["path"]).read_bytes()
    except (OSError, KeyError):
        return None
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{att.get('mime', 'image/png')};base64,{b64}"


def to_content_parts(text: str, images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the OpenAI-compatible multimodal content list for a user message:
    the text part (if any) followed by one image part per readable attachment."""
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    for img in images:
        url = data_url(img)
        if url:
            parts.append({"type": "image_url", "image_url": {"url": url}})
    # Never return an empty list — the API needs at least a (possibly empty) text part.
    if not parts:
        parts.append({"type": "text", "text": text or ""})
    return parts

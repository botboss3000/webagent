"""File base editor API for webAgent.

Provides admin-only endpoints for browsing, reading, and editing files
inside the project root. Intended to back the VS Code-style file editor
page in the web UI.

Path safety: every path is resolved relative to _PROJECT_ROOT and any
attempt to escape the root (via `..` or absolute paths) is rejected with
a 400. Hidden dotfiles and common build/cache directories are skipped
from listings by default.
"""

import base64
import logging
import os
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.jwt import decode_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/files", tags=["files"])

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ADMIN_USER_ID = "admin_default"

# Names to hide from the tree by default. Users can still address them
# directly if they know the path.
_HIDDEN_NAMES = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".source-backups",
    ".DS_Store", "dist", "build", ".next",
}

# Max bytes returned by read endpoint. Files above this come back with a
# `truncated: true` flag and `binary: true` if non-utf8.
_MAX_READ_BYTES = 2 * 1024 * 1024  # 2 MB
_MAX_WRITE_BYTES = 5 * 1024 * 1024  # 5 MB


# ── Auth ────────────────────────────────────────────────────────────

def _user_id(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        payload = decode_token(auth[7:])
        if payload:
            return payload.get("user_id", "")
    return ""


def _require_admin(request: Request) -> None:
    if _user_id(request) != _ADMIN_USER_ID:
        raise HTTPException(status_code=403, detail="Admin access required")


# ── Path resolution ─────────────────────────────────────────────────

def _resolve(rel_path: str) -> Path:
    """Resolve a user-supplied relative path inside _PROJECT_ROOT.

    Rejects absolute paths and any resolved path that falls outside the
    project root, even if symlinks point elsewhere.
    """
    rel = (rel_path or "").strip().lstrip("/").replace("\\", "/")
    candidate = (_PROJECT_ROOT / rel).resolve()
    try:
        candidate.relative_to(_PROJECT_ROOT.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="Path escapes project root")
    return candidate


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(_PROJECT_ROOT.resolve())).replace(os.sep, "/")
    except ValueError:
        return ""


# ── Models ──────────────────────────────────────────────────────────

class WriteRequest(BaseModel):
    path: str = Field(..., min_length=1)
    content: str = ""
    encoding: str = "utf-8"  # "utf-8" or "base64"


class CreateRequest(BaseModel):
    path: str = Field(..., min_length=1)
    kind: str = Field("file", pattern="^(file|dir)$")


class RenameRequest(BaseModel):
    path: str = Field(..., min_length=1)
    new_path: str = Field(..., min_length=1)


class DeleteRequest(BaseModel):
    path: str = Field(..., min_length=1)


# ── Endpoints ───────────────────────────────────────────────────────

@router.get("/check-access")
async def check_access(request: Request):
    """Return whether the requesting user can use the file editor."""
    return {"is_admin": _user_id(request) == _ADMIN_USER_ID}


@router.get("/tree")
async def list_tree(request: Request, path: str = "", show_hidden: bool = False):
    """List the immediate children of the given directory.

    Returns directories first (alphabetical), then files. Path is
    relative to project root; empty string means the root itself.
    """
    _require_admin(request)
    target = _resolve(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")

    dirs, files = [], []
    try:
        for entry in target.iterdir():
            name = entry.name
            if not show_hidden and (name.startswith(".") or name in _HIDDEN_NAMES):
                # Always allow .env.example since it's useful; otherwise hide
                if name not in {".env.example", ".gitignore", ".github"}:
                    continue
            try:
                is_dir = entry.is_dir()
                size = 0 if is_dir else entry.stat().st_size
            except OSError:
                continue
            item = {
                "name": name,
                "path": _rel(entry),
                "is_dir": is_dir,
                "size": size,
            }
            (dirs if is_dir else files).append(item)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    dirs.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())
    return {
        "path": _rel(target),
        "entries": dirs + files,
    }


@router.get("/read")
async def read_file(request: Request, path: str):
    """Read a file's contents. Returns utf-8 text when possible,
    otherwise base64-encoded bytes with `binary: true`."""
    _require_admin(request)
    target = _resolve(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")

    size = target.stat().st_size
    truncated = False
    read_size = size
    if size > _MAX_READ_BYTES:
        truncated = True
        read_size = _MAX_READ_BYTES

    with target.open("rb") as f:
        raw = f.read(read_size)

    try:
        text = raw.decode("utf-8")
        return {
            "path": _rel(target),
            "content": text,
            "encoding": "utf-8",
            "binary": False,
            "size": size,
            "truncated": truncated,
        }
    except UnicodeDecodeError:
        return {
            "path": _rel(target),
            "content": base64.b64encode(raw).decode("ascii"),
            "encoding": "base64",
            "binary": True,
            "size": size,
            "truncated": truncated,
        }


@router.post("/write")
async def write_file(request: Request, body: WriteRequest):
    """Write a file. Creates parent directories as needed."""
    _require_admin(request)
    target = _resolve(body.path)
    if target.exists() and target.is_dir():
        raise HTTPException(status_code=400, detail="Path is a directory")

    if body.encoding == "base64":
        try:
            data = base64.b64decode(body.content)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid base64 content")
    else:
        data = body.content.encode("utf-8")

    if len(data) > _MAX_WRITE_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds {_MAX_WRITE_BYTES} bytes")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"path": _rel(target), "size": len(data), "status": "ok"}


@router.post("/create")
async def create_entry(request: Request, body: CreateRequest):
    """Create an empty file or a directory."""
    _require_admin(request)
    target = _resolve(body.path)
    if target.exists():
        raise HTTPException(status_code=409, detail="Path already exists")

    if body.kind == "dir":
        target.mkdir(parents=True, exist_ok=False)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=False)
    return {"path": _rel(target), "kind": body.kind, "status": "ok"}


@router.post("/rename")
async def rename_entry(request: Request, body: RenameRequest):
    """Rename or move a file/directory inside the project root."""
    _require_admin(request)
    src = _resolve(body.path)
    dst = _resolve(body.new_path)
    if not src.exists():
        raise HTTPException(status_code=404, detail="Source not found")
    if dst.exists():
        raise HTTPException(status_code=409, detail="Destination already exists")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    return {"from": _rel(src), "to": _rel(dst), "status": "ok"}


@router.post("/delete")
async def delete_entry(request: Request, body: DeleteRequest):
    """Delete a file or directory (recursive for directories)."""
    _require_admin(request)
    target = _resolve(body.path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if target.resolve() == _PROJECT_ROOT.resolve():
        raise HTTPException(status_code=400, detail="Cannot delete project root")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"path": _rel(target), "status": "deleted"}

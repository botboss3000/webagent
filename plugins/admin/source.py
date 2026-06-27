"""
Source file management endpoints — unrestricted filesystem access.

Allows reading, writing, deleting files, and running shell commands.
Used by agent tools (read_source, write_source, edit_source, delete_source, run_command).

To disable: delete this file and source_tools.py, remove the import from main.py.
Guardrails can be added by implementing plugins/admin/guardrails.py.
"""

import ast
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

# Optional guardrails — delete guardrails.py to remove restrictions
try:
    from plugins.admin.guardrails import check_path, check_command
    _HAS_GUARDRAILS = True
except ImportError:
    _HAS_GUARDRAILS = False

    async def check_path(path: str, action: str = "read") -> None:
        pass

    async def check_command(command: str) -> None:
        pass

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/source", tags=["admin"])

# Project root for resolving relative paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

BACKUP_DIR = PROJECT_ROOT / ".source-backups"
BACKUP_DIR.mkdir(exist_ok=True)

# Syntax checkers for safety on write
SYNTAX_CHECKERS = {
    ".py": lambda code: ast.parse(code),
    ".json": lambda code: __import__("json").loads(code),
}


# ── Models ────────────────────────────────────────────────────────────────────

class FileInfo(BaseModel):
    path: str
    size_bytes: int
    is_dir: bool


class FileContent(BaseModel):
    path: str
    content: str
    size_bytes: int
    extension: str


class WriteRequest(BaseModel):
    path: str
    content: str
    create_backup: bool = True


class WriteResponse(BaseModel):
    path: str
    wrote: bool
    backup_path: Optional[str] = None
    message: str


class DeleteRequest(BaseModel):
    path: str
    recursive: bool = False


class DeleteResponse(BaseModel):
    path: str
    deleted: bool
    message: str


class CommandRequest(BaseModel):
    command: str
    timeout: int = 30


class CommandResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def resolve_path(raw_path: str) -> Path:
    """
    Resolve a path. If relative, it's relative to the project root.
    Unrestricted — any path the OS user can access is allowed.

    On Windows, paths like ``/tmp`` have ``is_absolute() == False`` but
    ``p.root == '\\'``, and Python's Path division (``PROJECT_ROOT / p``)
    *still* treats them as absolute (it sees the root separator and replaces
    the left side, producing ``C:\\tmp``).  To prevent this we strip the
    leading separator before joining so they land inside the project tree.
    """
    p = Path(raw_path)
    if not p.is_absolute() or (os.name == 'nt' and not p.drive and p.root and len(p.root) == 1):
        # Strip leading separator on Windows so Path division doesn't treat
        # it as absolute (e.g. /tmp -> tmp -> PROJECT_ROOT/tmp).
        p = PROJECT_ROOT / str(p).lstrip('\\/')
    return p.resolve()


def validate_syntax(content: str, ext: str) -> Optional[str]:
    """Validate syntax. Returns error message or None."""
    checker = SYNTAX_CHECKERS.get(ext)
    if checker is None:
        return None
    try:
        checker(content)
        return None
    except (SyntaxError, ValueError) as e:
        return str(e)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/read", response_model=FileContent)
async def read_file(path: str = Query(..., description="Path to file (relative or absolute)")):
    """Read any file on the system."""
    resolved = resolve_path(path)
    try:
        await check_path(str(resolved), "read")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {resolved}")
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {resolved}")

    content = resolved.read_text(encoding="utf-8")
    return FileContent(
        path=str(resolved),
        content=content,
        size_bytes=resolved.stat().st_size,
        extension=resolved.suffix.lower(),
    )


@router.post("/write", response_model=WriteResponse)
async def write_file(request: WriteRequest):
    """
    Create a new file or overwrite an existing one.
    - Creates parent directories automatically
    - Validates Python/JSON syntax before writing
    - Backs up existing files to .source-backups/
    """
    resolved = resolve_path(request.path)
    ext = resolved.suffix.lower()

    try:
        await check_path(str(resolved), "write")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    is_new = not resolved.exists()

    # Validate syntax before writing
    error = validate_syntax(request.content, ext)
    if error:
        raise HTTPException(status_code=400, detail=f"Syntax error: {error}")

    # Backup existing file
    backup_path = None
    if not is_new and request.create_backup:
        backup_filename = f"{resolved.name}.{int(time.time())}.bak"
        backup_file = BACKUP_DIR / backup_filename
        try:
            shutil.copy2(resolved, backup_file)
            backup_path = str(backup_file)
        except Exception as e:
            logger.warning("Backup failed: %s", e)

    # Write
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(request.content, encoding="utf-8")

    action = "Created" if is_new else "Updated"
    logger.info("%s: %s", action, resolved)
    return WriteResponse(
        path=str(resolved),
        wrote=True,
        backup_path=backup_path,
        message=f"{action} {resolved}",
    )


@router.post("/delete", response_model=DeleteResponse)
async def delete_file(request: DeleteRequest):
    """
    Delete a file or directory.
    - Files are deleted permanently (not sent to trash)
    - Directories require recursive=True
    """
    resolved = resolve_path(request.path)

    try:
        await check_path(str(resolved), "delete")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"Not found: {resolved}")

    try:
        if resolved.is_file():
            resolved.unlink()
            logger.info("Deleted file: %s", resolved)
            return DeleteResponse(
                path=str(resolved), deleted=True,
                message=f"Deleted file {resolved}",
            )
        elif resolved.is_dir():
            if not request.recursive:
                raise HTTPException(
                    status_code=400,
                    detail=f"'{resolved}' is a directory. Set recursive=true to delete it.",
                )
            shutil.rmtree(resolved)
            logger.info("Deleted directory: %s", resolved)
            return DeleteResponse(
                path=str(resolved), deleted=True,
                message=f"Deleted directory {resolved}",
            )
    except Exception as e:
        logger.error("Delete failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    raise HTTPException(status_code=500, detail="Unexpected error")


@router.post("/exec", response_model=CommandResponse)
async def run_command(request: CommandRequest):
    """
    Execute a shell command on the server.
    Returns stdout, stderr, and exit code.
    """
    try:
        await check_command(request.command)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    logger.info("Executing command: %s", request.command)
    try:
        result = subprocess.run(
            request.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=request.timeout,
        )
        return CommandResponse(
            exit_code=result.returncode,
            stdout=result.stdout[-50000:],
            stderr=result.stderr[-50000:],
        )
    except subprocess.TimeoutExpired:
        return CommandResponse(
            exit_code=-1,
            stdout="",
            stderr=f"Command timed out after {request.timeout} seconds",
            timed_out=True,
        )
    except Exception as e:
        return CommandResponse(
            exit_code=-1,
            stdout="",
            stderr=str(e),
        )


@router.get("/backups")
async def list_backups():
    """List available backups from previous writes."""
    if not BACKUP_DIR.exists():
        return []
    backups = []
    for f in sorted(BACKUP_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file():
            backups.append({
                "filename": f.name,
                "size_bytes": f.stat().st_size,
                "modified": f.stat().st_mtime,
            })
    return backups

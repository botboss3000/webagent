"""Admin-only local Codex CLI login launcher and status probe."""
import asyncio
import os
import shutil
import subprocess
from fastapi import APIRouter, HTTPException, Request

from app.api.claude_auth import _require_admin

router = APIRouter(prefix="/api/v1/codex")


def _codex() -> str:
    path = shutil.which("codex")
    if not path:
        raise HTTPException(status_code=404, detail="Codex is not installed or not on PATH on this device.")
    return path


def _status() -> dict:
    """Use the CLI's own status command; never inspect auth.json/keychain data."""
    try:
        result = subprocess.run([_codex(), "login", "status"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=10)
        detail = (result.stdout or result.stderr or "").strip()
        return {"installed": True, "signed_in": result.returncode == 0, "detail": detail[:500]}
    except subprocess.TimeoutExpired:
        return {"installed": True, "signed_in": False, "detail": "Codex did not return a login status in time."}
    except OSError as exc:
        return {"installed": False, "signed_in": False, "detail": str(exc)}


@router.get("/auth/status")
async def auth_status(request: Request):
    await _require_admin(request)
    return await asyncio.to_thread(_status)


@router.post("/login/start")
async def login_start(request: Request):
    await _require_admin(request)
    exe = _codex()
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        subprocess.Popen([exe, "login"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not start Codex sign-in: {exc}")
    return {"ok": True, "message": "Codex opened its browser sign-in flow. Finish it, then refresh this status."}


@router.post("/auth/signout")
async def auth_signout(request: Request):
    await _require_admin(request)
    try:
        result = await asyncio.to_thread(subprocess.run, [_codex(), "logout"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=500, detail=f"Could not sign out of Codex: {exc}")
    if result.returncode:
        raise HTTPException(status_code=500, detail=(result.stderr or result.stdout or "Codex sign-out failed.")[:500])
    return {"ok": True}

"""User-approved installers for managed Remote Access helpers.

The web process deliberately does not have root privileges. Managed binaries
therefore live under "data/bin": the same persistent data mount as the rest of
this installation's runtime state. Downloads come only from Cloudflare's
official GitHub release assets, are written atomically, and must execute a
successful "--version" probe before becoming active.
"""

from __future__ import annotations

import os
import platform
import subprocess
import threading
import urllib.request
import uuid
from pathlib import Path
from typing import Tuple

from . import store

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTALL_DIR = PROJECT_ROOT / "data" / "bin"
MAX_DOWNLOAD_BYTES = 150 * 1024 * 1024
_INSTALL_LOCK = threading.Lock()

_RELEASE_BASE = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
_ASSETS = {
    ("linux", "amd64"): "cloudflared-linux-amd64",
    ("linux", "386"): "cloudflared-linux-386",
    ("linux", "arm"): "cloudflared-linux-arm",
    ("linux", "arm64"): "cloudflared-linux-arm64",
    ("windows", "amd64"): "cloudflared-windows-amd64.exe",
    ("windows", "386"): "cloudflared-windows-386.exe",
}


def _normalise_arch(machine: str) -> str:
    value = (machine or "").strip().lower()
    if value in {"x86_64", "amd64"}:
        return "amd64"
    if value in {"i386", "i486", "i586", "i686", "x86"}:
        return "386"
    if value in {"aarch64", "arm64"}:
        return "arm64"
    if value.startswith("armv") or value == "arm":
        return "arm"
    return value


def cloudflared_download_spec(
    system: str | None = None, machine: str | None = None
) -> Tuple[str, str]:
    """Return the official release URL and persistent filename for this host."""
    os_name = (system or platform.system()).strip().lower()
    arch = _normalise_arch(machine or platform.machine())
    asset = _ASSETS.get((os_name, arch))
    if not asset:
        raise RuntimeError(
            f"automatic cloudflared installation is not supported on "
            f"{os_name or 'unknown OS'}/{arch or 'unknown architecture'}"
        )
    filename = "cloudflared.exe" if os_name == "windows" else "cloudflared"
    return _RELEASE_BASE + asset, filename


def _probe_version(path: Path) -> str:
    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception as exc:
        raise RuntimeError(f"downloaded cloudflared could not run: {exc}") from exc
    output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    if result.returncode != 0 or "cloudflared version" not in output.lower():
        detail = output[:300] or f"exit code {result.returncode}"
        raise RuntimeError(f"downloaded file failed the cloudflared version check: {detail}")
    return output.splitlines()[0][:200]


def install_cloudflared() -> dict:
    """Download, verify, persist, and select Cloudflare Quick Tunnel mode."""
    with _INSTALL_LOCK:
        url, filename = cloudflared_download_spec()
        INSTALL_DIR.mkdir(parents=True, exist_ok=True)
        target = INSTALL_DIR / filename
        suffix = ".tmp.exe" if filename.endswith(".exe") else ".tmp"
        temporary = INSTALL_DIR / f".cloudflared-{uuid.uuid4().hex}{suffix}"

        request = urllib.request.Request(
            url,
            headers={"User-Agent": "WebAgent cloudflared installer"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("cloudflared download is unexpectedly large")
                total = 0
                with open(temporary, "wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise RuntimeError("cloudflared download exceeded the size limit")
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            if total < 1024 * 1024:
                raise RuntimeError("cloudflared download was unexpectedly small")
            if os.name != "nt":
                temporary.chmod(0o755)
            version = _probe_version(temporary)
            os.replace(temporary, target)
            if os.name != "nt":
                target.chmod(0o755)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

        store.update_config({
            "active_method": "cloudflare",
            "cloudflare": {
                "bin_path": str(target),
                "quick": True,
            },
        })
        return {
            "path": str(target),
            "version": version,
            "source": url,
        }

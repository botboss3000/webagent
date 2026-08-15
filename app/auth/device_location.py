"""Best-effort client IP and coarse login-location resolution."""

from __future__ import annotations

import ipaddress
import time
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import Request


_LOCATION_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_SECONDS = 24 * 60 * 60


def request_client_ip(request: Request) -> str:
    """Return a validated client IP, honoring forwarding only from trusted peers."""
    peer = request.client.host if request.client else ""
    forwarded = ""
    try:
        from app.admin.integrations import _is_trusted_proxy

        if _is_trusted_proxy(request):
            forwarded = (
                request.headers.get("cf-connecting-ip", "").strip()
                or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            )
    except Exception:
        forwarded = ""
    candidate = forwarded or peer
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return ""


async def location_for_ip(ip_address: str) -> str:
    """Resolve a city/region/country label without ever blocking sign-in on failure."""
    if not ip_address:
        return "Unknown"
    try:
        address = ipaddress.ip_address(ip_address)
    except ValueError:
        return "Unknown"
    if address.is_private or address.is_loopback or address.is_link_local:
        return "Local network"

    cached = _LOCATION_CACHE.get(ip_address)
    now = time.time()
    if cached and cached[0] > now:
        return cached[1]

    location = "Unknown"
    try:
        url = f"https://ipwho.is/{quote(ip_address, safe='')}"
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
        if payload.get("success", True):
            parts = [
                str(payload.get("city") or "").strip(),
                str(payload.get("region") or "").strip(),
                str(payload.get("country") or "").strip(),
            ]
            location = ", ".join(dict.fromkeys(part for part in parts if part)) or "Unknown"
    except Exception:
        pass
    _LOCATION_CACHE[ip_address] = (now + _CACHE_SECONDS, location)
    return location

"""
HTTP error capture for the diagnostic flight-recorder.

Records every 4xx / 5xx response — status, method, path, the **cause** (the
HTTPException detail / validation errors, or the traceback for an unhandled
500), and the requesting user — into the recorder's ``http`` category (and
``logs/http.log``).

Why this exists: a deliberate ``raise HTTPException(400, "...")`` is treated by
FastAPI as a *normal* client-error response and is **not logged**, so it is
invisible to the recorder otherwise. This middleware closes that gap.

Implemented as a **pure-ASGI** middleware (not BaseHTTPMiddleware): it forwards
every ASGI message the instant it arrives, so it adds **zero** latency or
buffering to the success path — including SSE streams and large downloads. It
only *accumulates* the body for responses that are already ``>= 400`` (tiny
JSON error bodies), purely to extract the cause, and even those chunks are
forwarded immediately.
"""

import json
import logging
import os
import time
import traceback
from typing import List, Optional

logger = logging.getLogger(__name__)

SETTINGS_FILE = "app-settings.json"

# 404s for these are page-load noise (missing assets), not app problems.
_STATIC_404_EXT = (
    ".js", ".css", ".map", ".ico", ".svg", ".png", ".jpg", ".jpeg", ".gif",
    ".webp", ".woff", ".woff2", ".ttf", ".json", ".txt",
)
_STATIC_404_PREFIX = ("/ui/", "/screenshots/", "/visuals/", "/web-terminal", "/static", "/assets")


def _capture_enabled() -> bool:
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return bool((json.load(f) or {}).get("diagnostics_http_capture", True))
    except Exception:
        pass
    return True


def _should_record(path: str, status: int) -> bool:
    if status < 400:
        return False
    if status == 404:
        if path.endswith(_STATIC_404_EXT):
            return False
        if path.startswith(_STATIC_404_PREFIX):
            return False
    return True


def _header(headers: List, name: bytes) -> str:
    for k, v in headers or []:
        if k.lower() == name:
            try:
                return v.decode("latin-1")
            except Exception:
                return ""
    return ""


def _user_from_request(request) -> Optional[str]:
    try:
        auth = request.headers.get("authorization", "")
        raw = auth[7:] if auth.startswith("Bearer ") else request.query_params.get("token")
        if raw:
            from app.auth.jwt import decode_token
            p = decode_token(raw)
            if p:
                return p.get("user_id") or p.get("sub")
    except Exception:
        pass
    return None


def _extract_cause(body: bytes, content_type: str) -> Optional[str]:
    if not body:
        return None
    try:
        if "json" in (content_type or ""):
            data = json.loads(body.decode("utf-8", "replace"))
            if isinstance(data, dict) and "detail" in data:
                d = data["detail"]
                return d if isinstance(d, str) else json.dumps(d)[:1200]
    except Exception:
        pass
    return body.decode("utf-8", "replace")[:400] or None


def _record(scope, status: int, cause: Optional[str], dur_ms: float, traceback_str: Optional[str] = None) -> None:
    try:
        from app.agent.diagnostics import get_recorder
        rec = get_recorder()
        if not rec.enabled:
            return
        from starlette.requests import Request
        request = Request(scope)
        method = request.method
        path = request.url.path
        detail = {
            "status": status,
            "method": method,
            "path": path,
            "query": (str(request.url.query)[:300] or None),
            "duration_ms": round(dur_ms, 1),
            "cause": cause,
            "client": (request.client.host if request.client else None),
        }
        if traceback_str:
            detail["traceback"] = traceback_str[:8000]
        lvl = "error" if status >= 500 else "warning"
        rec.record(lvl, "http", f"{status} {method} {path}", source=f"HTTP {status}",
                   detail=detail, user_id=_user_from_request(request), persist=True)
    except Exception:
        pass


class HttpDiagnosticsMiddleware:
    """Pure-ASGI middleware — observes 4xx/5xx without disturbing the stream."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not _capture_enabled():
            await self.app(scope, receive, send)
            return

        start = time.time()
        st = {"status": None, "ct": "", "record": False, "chunks": []}

        async def send_wrapper(message):
            mtype = message["type"]
            if mtype == "http.response.start":
                st["status"] = message["status"]
                st["ct"] = _header(message.get("headers", []), b"content-type")
                st["record"] = _should_record(scope.get("path", ""), message["status"])
                await send(message)
            elif mtype == "http.response.body":
                # Forward immediately — never delay the client.
                await send(message)
                if st["record"]:
                    st["chunks"].append(message.get("body", b"") or b"")
                    if not message.get("more_body", False):
                        cause = _extract_cause(b"".join(st["chunks"]), st["ct"])
                        _record(scope, st["status"], cause, (time.time() - start) * 1000.0)
                        st["record"] = False  # don't double-record
            else:
                await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            # Unhandled → becomes a 500 (handled by the outer ServerErrorMiddleware).
            # Record here with the cause + traceback, then re-raise so normal
            # 500 handling proceeds unchanged.
            _record(
                scope, 500, f"{type(exc).__name__}: {exc}", (time.time() - start) * 1000.0,
                traceback_str="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            )
            raise


def install_http_diagnostics(app) -> None:
    """Register the HTTP-error capture middleware (outermost user middleware)."""
    app.add_middleware(HttpDiagnosticsMiddleware)

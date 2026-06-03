"""TUI Bridge Server — a lightweight HTTP server that lets the web app forward
messages to the TUI's agent loop.

The bridge runs as a background thread inside the TUI process. It exposes:
  POST /bridge/send   — receive a message, process through the TUI agent, return reply
  GET  /bridge/health — health check (used by the web app to detect the bridge)

The bridge uses the TUI's own Store for session history and the TUI's own
ServerManagerAgent for processing — so messages sent via the web UI produce
exactly the same result as typing in the terminal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ── Global bridge state ──────────────────────────────────────────────────────
# Set by the TUI app when it starts the bridge.
_handler: Optional[Callable] = None
_server: Optional[Any] = None
_port: int = 0
_loop: Optional[asyncio.AbstractEventLoop] = None


def find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def set_handler(handler: Callable) -> None:
    """Set the async handler function that processes incoming bridge messages.
    
    The handler receives (session_id: str, message: str, user_id: str) and
    returns a dict with at least {"reply": str}.
    """
    global _handler
    _handler = handler


def get_port() -> int:
    """Return the port the bridge server is listening on, or 0 if not started."""
    return _port


# ── ASGI app (FastAPI-like, minimal) ─────────────────────────────────────────

async def _app(scope: dict, receive: Callable, send: Callable) -> None:
    """Minimal ASGI application for the bridge server."""
    if scope["type"] != "http":
        return

    path = scope["path"]
    method = scope["method"]

    if method == "GET" and path == "/bridge/health":
        await _send_json(send, 200, {"status": "ok", "port": _port})
        return

    if method == "POST" and path == "/bridge/send":
        try:
            body_bytes = b""
            more_body = True
            while more_body:
                msg = await receive()
                if msg["type"] == "http.request":
                    body_bytes += msg.get("body", b"")
                    more_body = msg.get("more_body", False)

            body = json.loads(body_bytes.decode("utf-8"))
            session_id = body.get("session_id", "")
            message = body.get("message", "")
            user_id = body.get("user_id", "")

            if not session_id or not message:
                await _send_json(send, 400, {"error": "session_id and message are required"})
                return

            if _handler is None:
                await _send_json(send, 503, {"error": "TUI bridge handler not ready"})
                return

            result = await _handler(session_id, message, user_id)
            await _send_json(send, 200, result)
            return
        except json.JSONDecodeError:
            await _send_json(send, 400, {"error": "invalid JSON"})
            return
        except Exception as e:
            logger.error("Bridge handler error: %s", e, exc_info=True)
            await _send_json(send, 500, {"error": str(e)})
            return

    await _send_json(send, 404, {"error": "not found"})


async def _send_json(send: Callable, status: int, data: dict) -> None:
    body = json.dumps(data).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({
        "type": "http.response.body",
        "body": body,
    })


# ── Server lifecycle ─────────────────────────────────────────────────────────

def _run_server(port: int) -> None:
    """Run the ASGI server in a background thread using uvicorn."""
    import uvicorn
    config = uvicorn.Config(
        app=_app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    global _server
    _server = server
    server.run()


async def start(handler: Callable) -> int:
    """Start the bridge server in a background thread.
    
    Args:
        handler: Async callable(session_id, message, user_id) -> dict
        
    Returns:
        The port number the server is listening on.
    """
    global _handler, _port, _loop
    _handler = handler
    _loop = asyncio.get_event_loop()
    _port = find_free_port()

    thread = threading.Thread(target=_run_server, args=(_port,), daemon=True)
    thread.start()

    # Wait for the server to start
    import httpx
    for _ in range(20):
        await asyncio.sleep(0.1)
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"http://127.0.0.1:{_port}/bridge/health", timeout=2.0)
                if r.status_code == 200:
                    logger.info("TUI bridge server started on port %d", _port)
                    return _port
        except Exception:
            pass

    logger.warning("TUI bridge server may not have started on port %d", _port)
    return _port


async def stop() -> None:
    """Stop the bridge server."""
    global _server
    if _server is not None:
        _server.should_exit = True
        _server = None
    global _port
    _port = 0

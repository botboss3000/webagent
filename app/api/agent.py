"""
WebSocket endpoint — user event subscriber (receive-only).

Frontend connects once per user. Server broadcasts ALL agent events
(session responses, tool calls, pipeline steps) to the user's WebSocket.
The frontend filters which events to display where:
  - "stream" / "response" → chat bubbles (only for current session)
  - "tool_call" / "tool_result" / "pipeline" / "db" → stream/loop/flow panels
"""

import asyncio
import json
import logging
import datetime
from typing import Any
from websockets.exceptions import ConnectionClosedOK

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.api.chat import register_user_listener, unregister_user_listener

logger = logging.getLogger(__name__)

router = APIRouter()


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


@router.websocket("/api/v1/agent/ws")
async def agent_websocket(websocket: WebSocket):
    """
    Per-user event subscriber WebSocket.

    Client sends on connect:
      {"mode": "user_subscriber", "user_id": "uuid-here"}

    Server replies with heartbeat pings and streams all agent events
    (stream, response, tool_call, tool_result, pipeline, db, etc.)
    for ALL sessions belonging to that user.
    """
    await websocket.accept()

    user_id: str | None = None
    HEARTBEAT_INTERVAL = 25  # seconds

    async def _heartbeat():
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                try:
                    await websocket.send_text(json.dumps({"type": "ping"}, default=_json_default))
                except (WebSocketDisconnect, ConnectionClosedOK):
                    break
        except asyncio.CancelledError:
            pass

    heartbeat_task = asyncio.create_task(_heartbeat())

    try:
        # ── Wait for initial handshake ──
        raw = await websocket.receive_text()
        data = json.loads(raw)

        mode = data.get("mode", "").strip()
        claimed_user_id = data.get("user_id", "").strip()
        token = (data.get("token") or "").strip()

        if mode != "user_subscriber" or not claimed_user_id:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": "First message must be {\"mode\": \"user_subscriber\", \"user_id\": \"<uuid>\", \"token\": \"<jwt>\"}",
            }, default=_json_default))
            return

        # The WebSocket endpoint is exempted from the HTTP auth middleware
        # (`PUBLIC_PREFIXES` in app/auth/middleware.py), so we have to verify
        # the caller's identity ourselves here. Without this, an authenticated
        # user A could subscribe with user_id=B and start receiving every
        # event broadcast for user B — full cross-tenant leak. The handshake
        # MUST carry a JWT whose subject matches `claimed_user_id`.
        from app.auth.identity import verify_token_matches_user
        verified = verify_token_matches_user(token, claimed_user_id)
        if not verified:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": "Invalid or missing token, or token subject does not match user_id",
            }, default=_json_default))
            return
        user_id = verified

        # Register as a per-user listener — receives ALL events for this user
        register_user_listener(user_id, websocket)
        logger.info(f"User subscriber registered for user {user_id}")

        await websocket.send_text(json.dumps({
            "type": "subscribed",
            "user_id": user_id,
            "message": f"Receiving events for user {user_id}",
        }, default=_json_default))

        # ── Stay connected until client disconnects ──
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            if data.get("type") == "disconnect":
                break

    except (WebSocketDisconnect, ConnectionClosedOK):
        logger.info(f"User subscriber [{user_id}]: disconnected")
    except json.JSONDecodeError:
        try:
            await websocket.send_text(json.dumps({
                "type": "error", "message": "Invalid JSON",
            }, default=_json_default))
        except Exception:
            pass
    except Exception as e:
        logger.error(f"User subscriber error [{user_id}]: {e}", exc_info=True)
    finally:
        if user_id:
            unregister_user_listener(user_id, websocket)
        if heartbeat_task and not heartbeat_task.done():
            heartbeat_task.cancel()
        try:
            await heartbeat_task
        except (asyncio.CancelledError, Exception):
            pass
        logger.info(f"User subscriber [{user_id}]: cleaned up")

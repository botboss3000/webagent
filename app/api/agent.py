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
from app.agent.run_buffer import get_registry as get_run_buffer_registry

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
      {
        "mode": "user_subscriber",
        "user_id": "uuid-here",
        "token": "<jwt>",
        // OPTIONAL — resume any in-flight runs the client knows about.
        // Map of session_id -> last_session_seq the client has already seen.
        // Server replays buffered events with session_seq > that value
        // BEFORE switching to live streaming.
        "resume": { "<session_id>": <int_seq>, ... }
      }

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

        # ── Replay buffered events from active in-flight runs ──
        # If the client knows about active turns it was watching before the
        # reconnect, replay anything it missed BEFORE confirming the
        # subscription. This way the client never sees a gap between its
        # "last seen seq" and the next live event.
        resume_map = data.get("resume") or {}
        replayed_summary: dict = {}
        if isinstance(resume_map, dict) and resume_map:
            try:
                reg = get_run_buffer_registry()
                for sid, last_seq in resume_map.items():
                    if not isinstance(sid, str):
                        continue
                    try:
                        last_seq_int = int(last_seq)
                    except (TypeError, ValueError):
                        last_seq_int = 0
                    buf = reg.get(sid)
                    if buf is None:
                        continue
                    # Optional tenancy guard: only replay buffers owned by this user.
                    if buf.user_id and buf.user_id != user_id:
                        continue
                    missed = buf.replay_after(last_seq_int)
                    replayed_summary[sid] = len(missed)
                    for ev in missed:
                        try:
                            await websocket.send_text(json.dumps(
                                {**ev, "replayed": True},
                                default=_json_default,
                            ))
                        except Exception:
                            break
            except Exception as _re:
                logger.warning("Resume replay failed for user %s: %s", user_id, _re)

        # Inform client which active sessions the server is currently buffering.
        # Used by the UI to know which sessions have an in-flight run it could
        # subscribe to even if it doesn't have any local state for them yet.
        try:
            active_sessions = [
                sid for sid, b in get_run_buffer_registry()._buffers.items()
                if b.completed_at is None and b.user_id == user_id
            ]
        except Exception:
            active_sessions = []

        await websocket.send_text(json.dumps({
            "type": "subscribed",
            "user_id": user_id,
            "message": f"Receiving events for user {user_id}",
            "replayed": replayed_summary,
            "active_sessions": active_sessions,
        }, default=_json_default))

        # ── Stay connected until client disconnects ──
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            mtype = data.get("type")
            if mtype == "disconnect":
                break
            # Client can also request mid-session replay (e.g. when the user
            # switches back into a session it previously left). Payload:
            #   {"type": "resume", "session_id": "...", "last_session_seq": N}
            if mtype == "resume":
                sid = data.get("session_id")
                try:
                    last_seq_int = int(data.get("last_session_seq") or 0)
                except (TypeError, ValueError):
                    last_seq_int = 0
                if isinstance(sid, str):
                    try:
                        reg = get_run_buffer_registry()
                        buf = reg.get(sid)
                        if buf is not None and (not buf.user_id or buf.user_id == user_id):
                            for ev in buf.replay_after(last_seq_int):
                                await websocket.send_text(json.dumps(
                                    {**ev, "replayed": True},
                                    default=_json_default,
                                ))
                    except Exception as _re2:
                        logger.warning("Mid-session resume failed: %s", _re2)

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

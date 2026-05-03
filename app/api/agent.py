"""
WebSocket endpoint for agent interaction with streaming events.

Frontend connects here for both the side panel chat and the
terminal tool-call visibility. Events are routed by type:
  - "stream" / "response" → side panel (chat bubbles)
  - "tool_call" / "tool_result" → terminal (detailed logs)
"""

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.agent.prompts import build_system_prompt
from app.agent.streaming_loop import stream_agent_events
from app.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter()


def _ws_client_is_loopback(host: Optional[str]) -> bool:
    if host is None:
        return True
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    if host.startswith("::ffff:"):
        return host.removeprefix("::ffff:") == "127.0.0.1"
    return False


@router.websocket("/api/v1/agent/ws")
async def agent_websocket(websocket: WebSocket):
    """
    WebSocket for agent interaction.

    Client sends:  {"message": "what's in my documents?"}
                   {"message": "...", "session_id": "abc123"}

    Server streams events (JSON lines):
      {"type":"stream","content":"..."}
      {"type":"tool_call","tool":"web_search","args":{...}}
      {"type":"tool_result","tool":"web_search","result":"...","duration_ms":1234}
      {"type":"response","content":"Final answer"}
      {"type":"error","message":"..."}
    """

    client_host = websocket.client.host if websocket.client else None
    if not _ws_client_is_loopback(client_host):
        await websocket.close(code=4001, reason="Localhost only")
        return

    await websocket.accept()

    # Session-level state
    session_id: Optional[str] = None
    history: list = []

    # ── Heartbeat ping to keep connection alive ──
    HEARTBEAT_INTERVAL = 25  # seconds

    async def _heartbeat():
        """Send periodic ping frames to keep WS alive through proxies."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                try:
                    await websocket.send_text(json.dumps({"type": "ping"}))
                except (WebSocketDisconnect, Exception):
                    break
        except asyncio.CancelledError:
            pass

    heartbeat_task = asyncio.ensure_future(_heartbeat())

    try:
        while True:
            raw = await websocket.receive_text()

            # Parse incoming message
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Invalid JSON. Send {\"message\": \"...\"}",
                }))
                continue

            msg = data.get("message", "").strip()
            if not msg:
                continue

            # Require user_id (will be validated by auth later)
            user_id = data.get("user_id", "").strip()
            if not user_id:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Missing 'user_id' — provide a valid user UUID",
                }))
                continue

            # Session management
            if data.get("session_id"):
                session_id = data["session_id"]
            elif session_id is None:
                session_id = data.get("user_id") + "_" + str(id(websocket))

            # Reset flag
            if data.get("reset"):
                history = []

            logger.info(f"Agent WS [{session_id}]: {msg[:100]}")

            db = get_db()

            # Ensure the session exists in the database before querying
            try:
                await db.assert_session_owned(user_id, session_id)
            except PermissionError:
                # Create the session — it doesn't exist yet
                raw = db.get_raw_client()
                try:
                    raw.table("sessions").insert({
                        "id": session_id,
                        "user_id": user_id,
                        "title": f"Terminal — {session_id[:12]}",
                    }).execute()
                    logger.info(f"Created session {session_id} for user {user_id}")
                except Exception as create_err:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": f"Failed to create session: {create_err}",
                    }))
                    continue

            # Fetch context documents; if empty, copy defaults for this user
            context_docs = await db.fetch_context_documents(
                user_id,
                ["agent", "user", "skills", "tools", "tasks", "memory", "project", "jobs"],
            )
            if not context_docs:
                copied = await db.copy_defaults_to_user(user_id)
                if copied > 0:
                    context_docs = await db.fetch_context_documents(
                        user_id,
                        ["agent", "user", "skills", "tools", "tasks", "memory", "project", "jobs"],
                    )

            # ── PHASE 1: Brain-first lookup (visible as tool interaction) ──
            brain_results = await db.memory_search(user_id, msg, limit=5)
            brain_context = None

            # Format brain context for system prompt injection
            if brain_results:
                lines = []
                for r in brain_results:
                    slug = r.get("slug", "?")
                    title = r.get("title", slug)
                    ct = r.get("compiled_truth", "")[:300]
                    rank = r.get("rank", 0)
                    lines.append(f"## {slug} — {title} (score: {rank:.2f})")
                    if ct:
                        lines.append(ct)
                    lines.append("")
                brain_context = "\n".join(lines)

            # Build system prompt with dynamic tools
            system_prompt = await build_system_prompt(
                context_docs, brain_context, user_id
            )

            # Add user message to history
            history.append({"role": "user", "content": msg})

            # Save user message to database
            user_interaction_id = None
            try:
                user_interaction_id = await db.insert_interaction(
                    user_id, session_id, role="user", content=msg,
                    metadata=json.dumps({"source": "terminal"}),
                )
            except Exception as save_err:
                logger.warning(f"Failed to save user message: {save_err}")

            # Always save memory_search as tool interaction (even empty)
            search_content = json.dumps({
                "query": msg,
                "results": [
                    {"slug": r["slug"], "title": r.get("title",""),
                     "score": round(r.get("rank", 0), 2),
                     "snippet": r.get("compiled_truth", "")[:150]}
                    for r in (brain_results or [])
                ],
                "count": len(brain_results or []),
            }, indent=2)
            try:
                parent_id = await db.insert_interaction(
                    user_id, session_id, role="tool",
                    content=search_content,
                    parent_id=user_interaction_id,
                    tool_name="memory_search",
                    metadata=json.dumps({
                        "count": len(brain_results or []),
                        "brain": True,
                        "has_results": bool(brain_results),
                    }),
                )
            except Exception as save_err:
                logger.warning(f"Failed to save memory search interaction: {save_err}")
                parent_id = user_interaction_id

            # Stream agent events
            async for event in stream_agent_events(
                user_id=user_id,
                session_id=session_id,
                user_message=msg,
                system_prompt=system_prompt,
                history=history[:-1],  # Don't include current message twice
                parent_interaction_id=parent_id,
            ):
                # Send event to frontend
                try:
                    await websocket.send_text(json.dumps(event, default=str))
                except WebSocketDisconnect:
                    return

                # Accumulate assistant response into history
                if event["type"] == "response":
                    history.append({"role": "assistant", "content": event["content"]})
                    # Background memory save
                    asyncio.create_task(_save_chat_to_memory(
                        db, user_id, session_id, msg, event["content"], parent_id,
                    ))
                elif event["type"] == "stream":
                    pass  # Part of a streaming response being built
                elif event["type"] == "error":
                    history.append({"role": "assistant", "content": f"Error: {event['message']}"})

    except WebSocketDisconnect:
        logger.info(f"Agent WS [{session_id}]: disconnected")
    except Exception as e:
        logger.error(f"Agent WS error: {e}", exc_info=True)
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": str(e)}))
        except Exception:
            pass
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass


async def _save_chat_to_memory(
    db, user_id: str, session_id: str,
    user_message: str, assistant_reply: str,
    parent_interaction_id: str | None = None,
) -> None:
    """Save chat conversation to memory as visible tool interaction."""
    try:
        slug = f"chat/{session_id[:8]}"
        await db.memory_upsert(
            user_id, slug, "meeting",
            title=f"Session {session_id[:8]}",
            compiled_truth=assistant_reply[:500],
            timeline=user_message[:200],
        )

        save_content = json.dumps({
            "action": "upserted",
            "slug": slug,
            "summary": f"Saved chat: {user_message[:60]}...",
        }, indent=2)
        await db.insert_interaction(
            user_id, session_id, role="tool",
            content=save_content,
            parent_id=parent_interaction_id,
            tool_name="memory_save",
            metadata=json.dumps({"brain": True, "slug": slug}),
        )
        logger.debug("Saved chat to memory: %s", slug)
    except Exception as e:
        logger.warning("Failed to save chat to memory: %s", e)

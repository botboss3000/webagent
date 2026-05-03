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
import datetime # Added for JSON serialization
from typing import Optional, Any, Dict, List # Expanded typing imports
from websockets.exceptions import ConnectionClosedOK # Added for handling WebSocket disconnects more gracefully

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


# Helper function for JSON serialization (not part of the class, just a utility)
def _json_default_serializer(obj: Any) -> Any:
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


@router.websocket("/api/v1/agent/ws")
async def agent_websocket(websocket: WebSocket):
    """
    WebSocket for agent interaction.

    Client sends:  {"message": "what's in my documents?"}
                   {"message": "...", "session_id": "abc123"}

    Server streams events (JSON lines):
      {"type":"stream","content":"..."}       — token-by-token LLM output
      {"type":"tool_call","tool":"...","args":{...}}     — agent invoked a tool
      {"type":"tool_result","tool":"...","result":"...","duration_ms":1234}  — tool returned
      {"type":"response","content":"Final answer"}     — final answer (no more tool calls)
      {"type":"error","message":"..."}        — something went wrong
    """

    client_host = websocket.client.host if websocket.client else None
    if not _ws_client_is_loopback(client_host):
        await websocket.close(code=4001, reason="Localhost only")
        return

    await websocket.accept()

    # Session-level state, initialized on first message received
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    # All messages, including partial streaming responses, are accumulated here for context.
    history: List[Dict[str, str]] = []

    # Queue for incoming user messages from the WebSocket reader task
    user_message_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
    # Event to signal a new message has arrived, potentially interrupting the agent's current processing
    interrupt_agent_event = asyncio.Event()

    # ── Heartbeat ping to keep connection alive ──
    HEARTBEAT_INTERVAL = 25  # seconds

    async def _heartbeat():
        """Send periodic ping frames to keep WS alive through proxies."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                try:
                    # Using _json_default_serializer to handle datetimelike objects if any
                    await websocket.send_text(json.dumps({"type": "ping"}, default=_json_default_serializer))
                except (WebSocketDisconnect, ConnectionClosedOK):
                    break
        except asyncio.CancelledError:
            pass

    # ── Task to continuously read from the WebSocket ──
    async def read_websocket_messages():
        # nonlocal allows modifying user_id and session_id from the enclosing scope
        nonlocal user_id, session_id 
        try:
            while True:
                raw_message = await websocket.receive_text()
                data = json.loads(raw_message)

                # Initialize user_id and session_id from the first message if not already set
                if user_id is None: # user_id must be set first as session_id might derive from it
                    user_id = data.get("user_id", "").strip()
                    if not user_id:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": "Missing 'user_id' in first message — provide a valid user UUID",
                        }, default=_json_default_serializer))
                        continue # Skip this message; wait for one with user_id

                if session_id is None: # session_id can now be set, potentially using user_id
                    if data.get("session_id"):
                        session_id = data["session_id"]
                    else:
                        # Generate a unique session_id if not provided, based on user_id and websocket object ID
                        session_id = user_id + "_" + str(id(websocket)) 
                        await websocket.send_text(json.dumps({
                            "type": "session_id_assigned",
                            "session_id": session_id,
                            "message": "No session_id provided, assigned a new one.",
                        }, default=_json_default_serializer))


                # Put the incoming message into the queue
                await user_message_queue.put(data)
                # Signal that a new message has arrived, potentially interrupting the agent
                interrupt_agent_event.set()  

        except (WebSocketDisconnect, ConnectionClosedOK):
            logger.info(f"Agent WS [{session_id}]: Reader disconnected")
            # Signal the main loop to clean up or exit by putting a special message in the queue
            await user_message_queue.put({"type": "disconnect"}) 
        except json.JSONDecodeError:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": "Invalid JSON received. Please send {"message": "..."}",
            }, default=_json_default_serializer))
        except Exception as e:
            logger.error(f"Agent WS reader error: {e}", exc_info=True)
            try:
                await websocket.send_text(json.dumps({"type": "error", "message": str(e)}, default=_json_default_serializer))
            except (WebSocketDisconnect, ConnectionClosedOK):
                pass # Client already disconnected


    heartbeat_task = asyncio.create_task(_heartbeat())
    read_task = asyncio.create_task(read_websocket_messages())
    agent_processing_task: Optional[asyncio.Task] = None
    
    # Store the most recent message data that initiated the *currently running* agent task.
    # This is used to rebuild context if an agent task is cancelled and restarted with a new message.
    current_agent_prompt_payload: Optional[Dict[str, Any]] = None

    try:
        while True:
            # Wait for a new user message to process from the queue
            current_message_data = await user_message_queue.get()

            if current_message_data.get("type") == "disconnect":
                logger.info(f"Agent WS [{session_id}]: Received disconnect signal, exiting main loop.")
                break

            msg = current_message_data.get("message", "").strip()
            if not msg:
                continue
            
            # Ensure user_id and session_id are initialized. They should be by read_websocket_messages.
            if user_id is None or session_id is None:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Initialization error: user_id or session_id not set in agent_websocket main loop before processing message.",
                }, default=_json_default_serializer))
                continue # Try getting next message

            # If there's an ongoing agent task, cancel it because a new message has arrived
            # The agent_processing_task.done() check is important to avoid cancelling an already finished task
            if agent_processing_task and not agent_processing_task.done():
                logger.info(f"Agent WS [{session_id}]: Interrupting current agent task due to new user message.")
                agent_processing_task.cancel() # Request cancellation
                # Wait for the task to acknowledge and clean up, suppressing CancelledError
                try: 
                    await agent_processing_task 
                except asyncio.CancelledError:
                    pass
                # Send an acknowledgment to the client about the interruption
                await websocket.send_text(json.dumps({"type": "interrupted", "message": "Agent interrupted by new message."}, default=_json_default_serializer))
                
                # After interruption, if there was a previous LLM response being built, save it partially
                if current_agent_prompt_payload:
                    partial_reply_content = "(interrupted by user)" # This is a placeholder; actual partial content would need to be passed from stream_agent_events
                    asyncio.create_task(_save_chat_to_memory(
                        db, user_id, session_id,
                        user_message=current_agent_prompt_payload.get("user_message", "N/A"),
                        assistant_reply=partial_reply_content,
                        parent_interaction_id=current_agent_prompt_payload.get("parent_interaction_id")
                    ))
                current_agent_prompt_payload = None # Reset payload after interruption / partial save

            logger.info(f"Agent WS [{session_id}]: Processing new message: {msg[:100]}")

            db = get_db()

            # Ensure the session exists in the database before querying or creating
            try:
                await db.assert_session_owned(user_id, session_id)
            except PermissionError:
                # If session doesn't exist, create it. This is a common scenario for new sessions.
                raw = db.get_raw_client()
                try:
                    raw.table("sessions").insert({
                        "id": session_id,
                        "user_id": user_id,
                        "title": f"Terminal — {session_id[:12]}", # Default title
                    }).execute()
                    logger.info(f"Created session {session_id} for user {user_id}")
                except Exception as create_err:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": f"Failed to create session: {create_err}",
                    }, default=_json_default_serializer))
                    continue # Skip current message if session creation fails

            # Handle reset flag: clear history if client requests it
            if current_message_data.get("reset"):
                history = []
            
            # Fetch context documents (agent, user, skills, tools, etc.)
            context_docs = await db.fetch_context_documents(
                user_id,
                ["agent", "user", "skills", "tools", "tasks", "memory", "project", "jobs"],
            )
            if not context_docs:
                # If no context docs, try copying default ones for the user
                copied = await db.copy_defaults_to_user(user_id)
                if copied > 0:
                    context_docs = await db.fetch_context_documents(
                        user_id,
                        ["agent", "user", "skills", "tools", "tasks", "memory", "project", "jobs"],
                    )

            # ── PHASE 1: Brain-first lookup (visible as tool interaction) ──
            # Perform a memory search based on the user's message
            brain_results = await db.memory_search(user_id, msg, limit=5)
            brain_context = None

            # Format search results into a readable context for system prompt injection
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

            # Build the complete system prompt for the LLM
            system_prompt = await build_system_prompt(
                context_docs, brain_context, user_id
            )

            # Add the current user message to the session history
            history.append({"role": "user", "content": msg})

            # Save the user's interaction message to the database
            user_interaction_id = None
            try:
                user_interaction_id = await db.insert_interaction(
                    user_id, session_id, role="user", content=msg,
                    metadata=json.dumps({"source": "terminal"}, default=_json_default_serializer),
                )
            except Exception as save_err:
                logger.warning(f"Failed to save user message: {save_err}")

            # Always save memory_search interaction as a tool call (even if no results)
            search_content = json.dumps({
                "query": msg,
                "results": [
                    {"slug": r["slug"], "title": r.get("title",""),
                     "score": round(r.get("rank", 0), 2),
                     "snippet": r.get("compiled_truth", "")[:150]}
                    for r in (brain_results or [])
                ],
                "count": len(brain_results or []),
            }, indent=2, default=_json_default_serializer)
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
                    }, default=_json_default_serializer),
                )
            except Exception as save_err:
                logger.warning(f"Failed to save memory search interaction: {save_err}")
                parent_id = user_interaction_id

            # Clear the interrupt signal for the new agent task about to start
            interrupt_agent_event.clear()

            # Prepare the payload for the agent streaming task
            current_agent_prompt_payload = {
                "user_id": user_id,
                "session_id": session_id,
                "user_message": msg,
                "system_prompt": system_prompt,
                "history": history[:-1], # Current message already appended above
                "parent_interaction_id": parent_id
            }

            async def run_agent_stream_wrapper(
                _user_id: str, _session_id: str, _user_message: str, 
                _system_prompt: str, _history: List[Dict[str, str]], _parent_id: Optional[str]
            ):
                # nonlocal allows modifying agent_processing_task from the enclosing scope
                nonlocal agent_processing_task 
                try:
                    async for event in stream_agent_events(
                        user_id=_user_id,
                        session_id=_session_id,
                        user_message=_user_message,
                        system_prompt=_system_prompt,
                        history=_history, 
                        parent_interaction_id=_parent_id,
                        interrupt_event=interrupt_agent_event, # Pass the event down to stream_agent_events
                    ):
                        # Exit the streaming loop if an interrupt signal was received during processing
                        if interrupt_agent_event.is_set():
                            logger.info(f"Agent WS [{_session_id}]: Stream interrupted, stopping processing of current message.")
                            await websocket.send_text(json.dumps({"type": "interrupt_ack", "message": "Agent processing stopped due to interruption."}, default=_json_default_serializer))
                            return # Exit the stream_agent_events loop and this wrapper function
                            
                        # Send events (stream, tool_call, tool_result, response, error) to the frontend
                        try:
                            await websocket.send_text(json.dumps(event, default=_json_default_serializer))
                        except (WebSocketDisconnect, ConnectionClosedOK):
                            logger.info(f"Agent WS [{_session_id}]: Client disconnected during streaming events.")
                            return

                        # Accumulate assistant response into history for future turns
                        if event["type"] == "response":
                            history.append({"role": "assistant", "content": event["content"]})
                            # Asynchronously save the completed chat turn to memory
                            asyncio.create_task(_save_chat_to_memory(
                                db, _user_id, _session_id, _user_message, event["content"], _parent_id,
                            ))
                        elif event["type"] == "stream":
                            pass  # Parts of a streaming response are being built, not a final message
                        elif event["type"] == "error":
                            history.append({"role": "assistant", "content": f"Error: {event['message']}"})

                except asyncio.CancelledError:
                    logger.info(f"Agent WS [{_session_id}]: Agent task was cancelled externally (new user message). Original message: \"{_user_message[:50]}...\"")
                    # Mark in history that this response was interrupted
                    history.append({"role": "assistant", "content": "(Agent task interrupted externally)"})
                except Exception as e:
                    logger.error(f"Agent WS [{_session_id}]: Unhandled error during agent streaming: {e}", exc_info=True)
                    try:
                        await websocket.send_text(json.dumps({"type": "error", "message": str(e)}, default=_json_default_serializer))
                    except (WebSocketDisconnect, ConnectionClosedOK):
                        pass # Client likely already disconnected

                finally:
                    # Clear the agent_processing_task reference when this task finishes or is cancelled,
                    # but only if it's still the task we're tracking (to avoid race conditions)
                    if asyncio.current_task() == agent_processing_task: 
                        agent_processing_task = None
            
            # Start the agent's processing in a new, non-blocking task
            agent_processing_task = asyncio.create_task(
                run_agent_stream_wrapper(
                    user_id=user_id, 
                    session_id=session_id, 
                    user_message=msg, 
                    system_prompt=system_prompt, 
                    history=history[:-1], # history passed without the current user message (which might be handled internally by LLM)
                    parent_interaction_id=parent_id
                )
            )
            # The main loop *does not* await agent_processing_task here.
            # It continues immediately to wait for new user messages from `user_message_queue`,
            # enabling continuous input and interruption.

    except (WebSocketDisconnect, ConnectionClosedOK):
        logger.info(f"Agent WS [{session_id}]: Main handler disconnected cleanly.")
    except Exception as e:
        logger.error(f"Agent WS error in main loop: {e}", exc_info=True)
        try:
            await websocket.send_text(json.dumps({"type": "error", "message": str(e)}, default=_json_default_serializer))
        except (WebSocketDisconnect, ConnectionClosedOK):
            pass # Client likely already disconnected
    finally:
        # Ensure all spawned tasks are properly cancelled and awaited on exit to prevent resource leaks
        if heartbeat_task and not heartbeat_task.done():
            heartbeat_task.cancel()
        if read_task and not read_task.done():
            read_task.cancel()
        if agent_processing_task and not agent_processing_task.done():
            agent_processing_task.cancel()
        
        # Gather all tasks to ensure they complete their cancellation/cleanup
        await asyncio.gather(
            heartbeat_task,
            read_task,
            *([agent_processing_task] if agent_processing_task else []), # Conditionally include agent_processing_task if it exists
            return_exceptions=True # Allow other tasks to complete even if one fails
        )
        logger.info("All agent WS tasks stopped.")


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
        }, indent=2, default=_json_default_serializer) # Use custom serializer
        await db.insert_interaction(
            user_id, session_id, role="tool",
            content=save_content,
            parent_id=parent_interaction_id,
            tool_name="memory_save",
            metadata=json.dumps({"brain": True, "slug": slug}, default=_json_default_serializer), # Use custom serializer
        )
        logger.debug("Saved chat to memory: %s", slug)
    except Exception as e:
        logger.warning("Failed to save chat to memory: %s", e)

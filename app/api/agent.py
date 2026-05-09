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

from app.agent.prompts import (
    build_system_prompt,
    format_attachments_for_prompt,
    CONTEXT_SECTION_TYPES,
)
from app.agent.loop import stream_agent_events
from app.agent.session_history import build_openai_history_from_session
from app.db import get_db
from app.api.chat import register_visualizer_listener, unregister_visualizer_listener

logger = logging.getLogger(__name__)

router = APIRouter()


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

    await websocket.accept()

    # Session-level state, initialized on first message received
    session_id: Optional[str] = None
    user_id: Optional[str] = None

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

                # ── Visualizer listener mode (HTTP chat events) ──
                if data.get("mode") == "http_chat":
                    vis_user = data.get("user_id", "").strip()
                    vis_session = data.get("session_id", "").strip()
                    if not vis_user or not vis_session:
                        await websocket.send_text(json.dumps({
                            "type": "error", "level": "agent",
                            "message": "http_chat mode requires user_id and session_id",
                        }, default=_json_default_serializer))
                        continue
                    register_visualizer_listener(vis_session, websocket)
                    logger.info(f"Visualizer listener registered for session {vis_session}")
                    # Stay connected, receiving events from chat endpoint
                    try:
                        while True:
                            raw = await websocket.receive_text()
                            try:
                                d = json.loads(raw)
                                if d.get("type") == "disconnect":
                                    break
                            except json.JSONDecodeError:
                                pass
                    except (WebSocketDisconnect, ConnectionClosedOK):
                        pass
                    finally:
                        unregister_visualizer_listener(vis_session, websocket)
                    return  # Exit reader task

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
                "message": "Invalid JSON received. Please send {\"message\": \"...\"}",
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
                        "title": f"{session_id[:12]}", # Default title
                    }).execute()
                    logger.info(f"Created session {session_id} for user {user_id}")
                except Exception as create_err:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": f"Failed to create session: {create_err}",
                    }, default=_json_default_serializer))
                    continue # Skip current message if session creation fails

            # ── Agent assignment (first chat → create agent) ──
            agent = await db.get_agent_for_user(user_id)
            if agent is None:
                agent = await db.create_agent_for_user(user_id)
                await websocket.send_text(json.dumps({
                    "type": "pipeline", "level": "pipeline",
                    "step": "agent_assigned",
                    "agent_id": agent["id"],
                    "max_turn_count": agent["max_turn_count"],
                }, default=_json_default_serializer))
            row = await db.get_agent_by_id(agent["id"])
            if row:
                agent = row
            max_turns = agent["max_turn_count"]

            # reset=True skips loading prior turns from DB for this message only (dev escape hatch)
            skip_db_history = bool(current_message_data.get("reset"))

            # ── Emit user message to pipeline ──
            await websocket.send_text(json.dumps({
                "type": "pipeline", "level": "user",
                "step": "user_message", "content": msg,
            }, default=_json_default_serializer))
            
            # Fetch context documents (agent, user, skills, tools, etc.)
            context_docs = await db.fetch_context_documents(
                agent["id"], CONTEXT_SECTION_TYPES,
            )
            if not context_docs:
                copied = await db.copy_defaults_to_agent(agent["id"])
                if copied > 0:
                    context_docs = await db.fetch_context_documents(
                        agent["id"], CONTEXT_SECTION_TYPES,
                    )

            # ── Pipeline: context loaded ──
            doc_types = list(set(
                (d.get("context_type") or d.get("doc_type") or "")
                for d in context_docs if d.get("context_type") or d.get("doc_type")
            ))
            await websocket.send_text(json.dumps({
                "type": "pipeline", "level": "pipeline",
                "step": "load_context", "count": len(context_docs),
                "types": doc_types,
            }, default=_json_default_serializer))

            # ── PHASE 1: Brain-first lookup (visible as tool interaction) ──
            # Perform a memory search based on the user's message
            await websocket.send_text(json.dumps({
                "type": "pipeline", "level": "pipeline",
                "step": "memory_search_start", "query": msg, "limit": 5,
            }, default=_json_default_serializer))

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

            # ── Pipeline: memory search results ──
            await websocket.send_text(json.dumps({
                "type": "pipeline", "level": "pipeline",
                "step": "memory_search_end", "results_count": len(brain_results),
                "results": [{"slug": r["slug"], "title": r.get("title", r["slug"]),
                             "score": round(r.get("rank", 0), 2)}
                            for r in (brain_results or [])],
            }, default=_json_default_serializer))

            # ── Resolve attachment references ──
            attachment_ids = current_message_data.get("attachment_ids")
            attachment_context = None
            if attachment_ids:
                attachment_docs = []
                for att_id in attachment_ids:
                    att = await db.get_attachment(att_id)
                    if att:
                        attachment_docs.append(att)
                if attachment_docs:
                    attachment_context = format_attachments_for_prompt(attachment_docs)
                    # Emit attachment event to frontend
                    await websocket.send_text(json.dumps({
                        "type": "attachment", "level": "agent",
                        "attachments": [
                            {"id": a["id"], "original_name": a["original_name"],
                             "mime_type": a["mime_type"], "size_bytes": a["size_bytes"],
                             "storage_path": a.get("storage_path", "")}
                            for a in attachment_docs
                        ],
                    }, default=_json_default_serializer))

            # Build the complete system prompt for the LLM
            system_prompt = await build_system_prompt(
                context_docs, brain_context, user_id,
                agent_system_prompt=agent.get("system_prompt"),
            )
            # Append attachment context after base prompt
            if attachment_context:
                system_prompt = system_prompt + "\n\n" + attachment_context

            # ── Pipeline: prompt built ──
            from app.tools.loader import load_tools as lt
            ws_tools = await lt(user_id)
            tool_count = len(ws_tools)
            await websocket.send_text(json.dumps({
                "type": "pipeline", "level": "pipeline",
                "step": "build_prompt",
                "brain_injected": bool(brain_context),
                "tool_count_in_prompt": tool_count,
            }, default=_json_default_serializer))

            # Save the user's interaction message to the database
            user_interaction_id = None
            try:
                user_interaction_id = await db.insert_interaction(
                    user_id, session_id, role="user", content=msg,
                    channel="webagent_ui",
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
                    channel="webagent_ui",
                    metadata=json.dumps({
                        "count": len(brain_results or []),
                        "brain": True,
                        "has_results": bool(brain_results),
                    }, default=_json_default_serializer),
                )
            except Exception as save_err:
                logger.warning(f"Failed to save memory search interaction: {save_err}")
                parent_id = user_interaction_id

            if skip_db_history:
                llm_history: List[Dict[str, Any]] = []
            else:
                excl: set[str] = set()
                if user_interaction_id:
                    excl.add(user_interaction_id)
                llm_history = await build_openai_history_from_session(
                    db, user_id, session_id, exclude_interaction_ids=excl,
                )

            # Clear the interrupt signal for the new agent task about to start
            interrupt_agent_event.clear()

            # Prepare the payload for the agent streaming task
            current_agent_prompt_payload = {
                "user_id": user_id,
                "session_id": session_id,
                "user_message": msg,
                "system_prompt": system_prompt,
                "parent_interaction_id": parent_id
            }

            async def run_agent_stream_wrapper(
                _user_id: str, _session_id: str, _user_message: str, 
                _system_prompt: str, _history: List[Dict[str, Any]], _parent_id: Optional[str],
                _max_turns: int,
            ):
                # nonlocal allows modifying agent_processing_task from the enclosing scope
                nonlocal agent_processing_task 

                try:
                    client_disconnected = False
                    async for event in stream_agent_events(
                        user_id=_user_id,
                        session_id=_session_id,
                        user_message=_user_message,
                        system_prompt=_system_prompt,
                        history=_history, 
                        parent_interaction_id=_parent_id,
                        interrupt_event=interrupt_agent_event,
                        max_turns=_max_turns,
                        channel="webagent_ui",
                    ):
                        # Exit the streaming loop if an interrupt signal was received during processing
                        if interrupt_agent_event.is_set():
                            logger.info(f"Agent WS [{_session_id}]: Stream interrupted, stopping processing of current message.")
                            if not client_disconnected:
                                try:
                                    await websocket.send_text(json.dumps({"type": "interrupt_ack", "message": "Agent processing stopped due to interruption."}, default=_json_default_serializer))
                                except Exception:
                                    pass
                            return # Exit the stream_agent_events loop and this wrapper function
                            
                        # Send events (stream, tool_call, tool_result, response, error) to the frontend
                        if not client_disconnected:
                            try:
                                await websocket.send_text(json.dumps(event, default=_json_default_serializer))
                            except (WebSocketDisconnect, ConnectionClosedOK, RuntimeError):
                                logger.info(f"Agent WS [{_session_id}]: Client disconnected during streaming events. Agent continues in background.")
                                client_disconnected = True

                        if event["type"] == "response":
                            # Pipeline: memory save
                            if not client_disconnected:
                                try:
                                    await websocket.send_text(json.dumps({
                                        "type": "pipeline", "level": "pipeline",
                                        "step": "memory_save_start",
                                        "slug": f"chat/{_session_id[:8]}",
                                    }, default=_json_default_serializer))
                                except Exception:
                                    pass
                            # Asynchronously save the completed chat turn to memory
                            asyncio.create_task(_save_chat_to_memory(
                                db, _user_id, _session_id, _user_message, event["content"], _parent_id,
                            ))
                        elif event["type"] == "stream":
                            pass  # Parts of a streaming response are being built, not a final message
                        elif event["type"] == "error":
                            pass

                except asyncio.CancelledError:
                    logger.info(f"Agent WS [{_session_id}]: Agent task was cancelled externally (new user message). Original message: \"{_user_message[:50]}...\"")
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
                    user_id,
                    session_id,
                    msg,
                    system_prompt,
                    llm_history,
                    parent_id,
                    max_turns,
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
        
        # We intentionally DO NOT cancel the agent_processing_task here.
        # This allows the agent to continue executing in the background 
        # even after the client disconnects.
        
        # Gather all tasks to ensure they complete their cancellation/cleanup
        await asyncio.gather(
            heartbeat_task,
            read_task,
            # We don't await agent_processing_task here so it runs detached
            return_exceptions=True 
        )
        logger.info("Agent WS socket tasks stopped (background agent may still be running).")


async def _save_chat_to_memory(
    db, user_id: str, session_id: str,
    user_message: str, assistant_reply: str,
    parent_interaction_id: Optional[str] = None,
) -> None:
    """Save chat conversation to memory as visible tool interaction."""
    try:
        from app.api.chat import _emit_to_visualizers
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
            channel="webagent_ui",
            metadata=json.dumps({"brain": True, "slug": slug}, default=_json_default_serializer), # Use custom serializer
        )
        # Emit visualizer events
        await _emit_to_visualizers(session_id, {
            "type": "pipeline", "level": "pipeline",
            "step": "memory_save_end", "slug": slug,
        })
        await _emit_to_visualizers(session_id, {
            "type": "db", "level": "db",
            "op": "memory_upsert", "slug": slug, "page_type": "meeting",
        })
        logger.debug("Saved chat to memory: %s", slug)
    except Exception as e:
        logger.warning("Failed to save chat to memory: %s", e)

"""Chat endpoint for webAgent."""

import asyncio
import json
import logging
import re
import uuid
from typing import List, Any, Dict, Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from app.models.schemas import ChatRequest, ChatResponse
from app.db import get_db
from app.agent.prompts import (
    build_system_prompt,
    format_attachments_for_prompt,
    CONTEXT_SECTION_TYPES,
)

from app.agent.loop import run_agent_loop_buffered, stream_agent_events
from app.agent.session_history import build_openai_history_from_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/chat", tags=["chat"])

# ── Memory skip gate ──
# Skip memory_search for trivial messages (greetings, affirmations, commands).
_SKIP_MEMORY_PATTERN = re.compile(
    r"^(hi|hello|hey|sup|yo|thanks|thank you|ok|okay|got it|cool|sure|"
    r"yes|no|go ahead|keep going|continue|next|and\??|more|elaborate|"
    r"good (morning|afternoon|evening)|what'?s up|how are you|how'?s it going|"
    r"check my email|list my files|show my messages|"
    r"read my email|get my mail|open my inbox)[\s!?.]*$",
    re.IGNORECASE,
)


def _should_skip_memory(message: str) -> bool:
    """Return True if message is trivial and doesn't need brain context."""
    stripped = (message or "").strip()
    return bool(not stripped or _SKIP_MEMORY_PATTERN.match(stripped))


async def _ensure_session(db, user_id: str, session_id: str) -> None:
    """Create the session row if it doesn't exist yet."""
    try:
        await db.assert_session_owned(user_id, session_id)
    except (PermissionError, Exception):
        try:
            raw = db.get_raw_client()
            raw.table("sessions").insert({
                "id": session_id,
                "user_id": user_id,
                "title": session_id[:12],
            }).execute()
            logger.info(f"Created session {session_id[:12]} for user {user_id[:12]}")
        except Exception as create_err:
            logger.warning(f"Session creation failed (may already exist): {create_err}")

class InterruptRequest(BaseModel):
    session_id: str

@router.post("/interrupt")
async def interrupt_chat(request: InterruptRequest):
    """Request an interruption for an ongoing chat session."""
    try:
        db = get_db()
        await db.set_interrupt(request.session_id)
        return {"status": "ok", "message": "Interrupt requested."}
    except Exception as e:
        logger.error(f"Error setting interrupt: {e}")
        raise HTTPException(status_code=500, detail=str(e))



@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    Process a chat message with the agent.

    Uses the simple agent loop with tool-calling support.
    """
    try:
        db = get_db()

        # Ensure the session exists before inserting interactions
        await _ensure_session(db, request.user_id, request.session_id)

        # ── Optimizer session: route to dedicated Planner/Finalizer agent ──
        if request.session_id.startswith('optimizer-'):
            conn = db._get_conn()
            try:
                meta_row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id=?", (request.session_id,)
                ).fetchone()
                metadata = json.loads(meta_row[0]) if meta_row and meta_row[0] else {}
                opt_role = metadata.get('opt_role', 'planner')
                opt_agent_user_id = f"opt_{opt_role}_{request.user_id}"
                agent = await db.get_agent_for_user(opt_agent_user_id)
                if agent is None:
                    prompter = 'opt_planner' if opt_role == 'planner' else 'opt_finalizer'
                    cur = conn.execute(
                        "SELECT * FROM agent_templates WHERE id=? LIMIT 1",
                        (prompter,)
                    )
                    tpl = cur.fetchone()
                    if tpl:
                        tpl_data = {
                            "system_prompt": tpl["system_prompt"],
                            "max_turn_count": tpl["max_turn_count"],
                            "model": tpl["model"],
                            "provider": tpl["provider"],
                            "temperature": tpl["temperature"],
                            "max_tokens": tpl["max_tokens"],
                            "metadata": tpl["metadata"],
                        }
                    else:
                        tpl_data = {
                            "system_prompt": f'You are the webAgent {opt_role.title()} agent.',
                            "max_turn_count": 1000,
                            "model": None,
                            "provider": None,
                            "temperature": 0.0,
                            "max_tokens": 4096,
                            "metadata": "{}",
                        }
                    agent_id = str(uuid.uuid4())
                    conn.execute(
                        """INSERT INTO agents
                           (id, user_id, system_prompt, max_turn_count, model, provider,
                            temperature, max_tokens, status, metadata, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, datetime('now'), datetime('now'))""",
                        (agent_id, opt_agent_user_id,
                         tpl_data["system_prompt"],
                         tpl_data["max_turn_count"],
                         tpl_data["model"],
                         tpl_data["provider"],
                         tpl_data["temperature"],
                         tpl_data["max_tokens"],
                         tpl_data["metadata"]),
                    )
                    conn.commit()
                    agent = await db.get_agent_for_user(opt_agent_user_id)
            finally:
                conn.close()
            if agent:
                # Skip normal agent assignment for optimizer sessions
                pass
            else:
                # Fall through to normal assignment
                pass

        # ── Assign agent first (context rows are keyed by agent_id) ──
        if not request.session_id.startswith('optimizer-') or agent is None:
            agent = await db.get_agent_for_user(request.user_id)
        if agent is None:
            agent = await db.create_agent_for_user(request.user_id)
            await _emit_to_visualizers(request.session_id, {
                "type": "pipeline", "level": "pipeline",
                "step": "agent_assigned",
                "agent_id": agent["id"],
                "max_turn_count": agent["max_turn_count"],
            })

        # Save user message and get its ID for parent linking
        # Optimizer sessions get source='optimizer' to distinguish from normal chats
        is_opt = request.session_id.startswith('optimizer-')
        user_interaction_id = await db.insert_interaction(
            request.user_id, request.session_id, role="user", content=request.message,
            channel="web_portal",
            metadata=json.dumps({"source": "optimizer" if is_opt else "web_portal_chat"}),
            sender_id=request.user_id,
            receiver_id=agent["id"],
            source="optimizer" if is_opt else None,
        )

        # ── Emit user message to visualizer listeners ──
        await _emit_to_visualizers(request.session_id, {
            "type": "user_message", "level": "user",
            "content": request.message, "id": user_interaction_id,
        })

        row = await db.get_agent_by_id(agent["id"])
        if row:
            agent = row

        # Fetch context documents; if empty, seed from templates for this agent
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
        await _emit_to_visualizers(request.session_id, {
            "type": "pipeline", "level": "pipeline",
            "step": "load_context", "count": len(context_docs),
            "types": doc_types,
        })

        # ── PHASE 1: Brain-first lookup (visible as tool interaction) ──
        if _should_skip_memory(request.message):
            await _emit_to_visualizers(request.session_id, {
                "type": "pipeline", "level": "pipeline",
                "step": "memory_search_skip", "reason": "greeting_or_cmd",
            })
            brain_results = []
            brain_context = None
            parent_id = await db.insert_interaction(
                request.user_id, request.session_id, role="tool",
                content=json.dumps({"skipped": True, "reason": "greeting_or_cmd"}),
                parent_id=user_interaction_id,
                tool_name="memory_search",
                channel="web_portal",
                metadata=json.dumps({"brain": True, "skipped": True}),
                sender_id=agent["id"],
                receiver_id=agent["id"],
            )
            await _emit_to_visualizers(request.session_id, {
                "type": "pipeline", "level": "pipeline",
                "step": "memory_search_end", "results_count": 0, "results": [],
                "skipped": True,
            })
        else:
            await _emit_to_visualizers(request.session_id, {
                "type": "pipeline", "level": "pipeline",
                "step": "memory_search_start", "query": request.message, "limit": 5,
            })

            brain_results = await db.memory_search(request.user_id, request.message, limit=5)
            brain_context = None

            # ── Pipeline: memory search results ──
            await _emit_to_visualizers(request.session_id, {
                "type": "pipeline", "level": "pipeline",
                "step": "memory_search_end", "results_count": len(brain_results),
                "results": [{"slug": r["slug"], "title": r.get("title", r["slug"]),
                             "score": round(r.get("rank", 0), 2)}
                            for r in (brain_results or [])],
            })

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

            # Save memory_search as tool interaction
            search_content = json.dumps({
                "query": request.message,
                "results": [
                    {"slug": r["slug"], "title": r.get("title",""),
                     "score": round(r.get("rank", 0), 2),
                     "snippet": r.get("compiled_truth", "")[:150]}
                    for r in (brain_results or [])
                ],
                "count": len(brain_results or []),
            }, indent=2)
            parent_id = await db.insert_interaction(
                request.user_id, request.session_id, role="tool",
                content=search_content,
                parent_id=user_interaction_id,
                tool_name="memory_search",
                channel="web_portal",
                metadata=json.dumps({
                    "count": len(brain_results or []),
                    "brain": True,
                    "has_results": bool(brain_results),
                }),
                sender_id=agent["id"],
                receiver_id=agent["id"],
            )

            # Emit memory_search as a tool result
            await _emit_to_visualizers(request.session_id, {
                "type": "tool_result", "level": "agent",
                "tool": "memory_search",
                "result": search_content[:2000],
                "duration_ms": 0,
                "error": False,
            })

        # ── Resolve attachment references ──
        attachment_context = None
        if request.attachment_ids:
            attachment_docs = []
            for att_id in request.attachment_ids:
                att = await db.get_attachment(att_id)
                if att:
                    attachment_docs.append(att)
            if attachment_docs:
                attachment_context = format_attachments_for_prompt(attachment_docs)
                await _emit_to_visualizers(request.session_id, {
                    "type": "attachment", "level": "agent",
                    "attachments": [
                        {"id": a["id"], "original_name": a["original_name"],
                         "mime_type": a["mime_type"], "size_bytes": a["size_bytes"],
                         "storage_path": a.get("storage_path", "")}
                        for a in attachment_docs
                    ],
                })

        # Build system prompt with brain context + dynamic tools
        system_prompt = await build_system_prompt(
            context_docs, brain_context, request.user_id,
            agent_system_prompt=agent.get("system_prompt"),
        )
        if attachment_context:
            system_prompt = system_prompt + "\n\n" + attachment_context

        # ── Pipeline: prompt built ──
        from app.tools.loader import load_tools
        tools = await load_tools(request.user_id)
        tool_count_for_prompt = len(tools)
        section_names = ["SYSTEM"]  # Simplified section count — actual sections are dynamic

        await _emit_to_visualizers(request.session_id, {
            "type": "pipeline", "level": "pipeline",
            "step": "build_prompt", "sections": section_names,
            "brain_injected": bool(brain_context),
            "tool_count_in_prompt": tool_count_for_prompt,
        })

        # DB-backed conversation history (same session survives browser refresh)
        exclude_ids = {user_interaction_id} if user_interaction_id else set()
        history = await build_openai_history_from_session(
            db, request.user_id, request.session_id,
            exclude_interaction_ids=exclude_ids,
        )

        # Create event callback that pushes to visualizer and user listeners
        async def event_callback(event: Dict[str, Any]):
            await _emit_to_visualizers(request.session_id, event, user_id=request.user_id)

        # Run the agent loop
        assistant_reply = await run_agent_loop_buffered(
            user_id=request.user_id,
            session_id=request.session_id,
            user_message=request.message,
            system_prompt=system_prompt,
            agent_id=agent["id"],
            history=history,
            parent_interaction_id=parent_id,
            event_callback=event_callback,
            max_turns=agent.get("max_turn_count", 10),
            channel="web_portal",
        )

        # ── PHASE 3: Background memory save (visible tool interaction) ──
        asyncio.create_task(_save_chat_to_memory(
            db, request.user_id, request.session_id,
            request.message, assistant_reply, agent["id"], parent_id,
        ))

        # ── Pipeline: memory save (async, fire-and-forget notification) ──
        await _emit_to_visualizers(request.session_id, {
            "type": "pipeline", "level": "pipeline",
            "step": "memory_save_start", "slug": f"chat/{request.session_id[:8]}",
        })

        return ChatResponse(
            reply=assistant_reply,
            response=assistant_reply,
            session_id=request.session_id,
        )

    except Exception as e:
        logger.error(f"Chat endpoint error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/stream")
async def chat_stream(request: ChatRequest, fastapi_request: Request):
    """
    Process a chat message using Server-Sent Events (SSE).
    """
    db = get_db()

    # Ensure the session exists before inserting interactions
    await _ensure_session(db, request.user_id, request.session_id)
    
    agent = await db.get_agent_for_user(request.user_id)
    if agent is None:
        agent = await db.create_agent_for_user(request.user_id)

    # Save user message and get its ID for parent linking
    user_interaction_id = await db.insert_interaction(
        request.user_id, request.session_id, role="user", content=request.message,
        channel="web_portal",
        metadata=json.dumps({"source": "web_portal_chat_sse"}),
        sender_id=request.user_id,
        receiver_id=agent["id"],
    )

    async def event_generator():
        nonlocal agent
        if "max_turn_count" not in agent:
            # Re-fetch agent if creation dict missed it somehow (fallback)
            row = await db.get_agent_by_id(agent["id"])
            if row:
                agent = row
        
        yield f"data: {json.dumps({'type': 'pipeline', 'level': 'pipeline', 'step': 'agent_assigned', 'agent_id': agent['id'], 'max_turn_count': agent.get('max_turn_count', 10)})}\n\n"

        context_docs = await db.fetch_context_documents(
            agent["id"], CONTEXT_SECTION_TYPES,
        )
        if not context_docs:
            copied = await db.copy_defaults_to_agent(agent["id"])
            if copied > 0:
                context_docs = await db.fetch_context_documents(
                    agent["id"], CONTEXT_SECTION_TYPES,
                )

        doc_types = list(set(
            (d.get("context_type") or d.get("doc_type") or "")
            for d in context_docs if d.get("context_type") or d.get("doc_type")
        ))
        yield f"data: {json.dumps({'type': 'pipeline', 'level': 'pipeline', 'step': 'load_context', 'count': len(context_docs), 'types': doc_types})}\n\n"

        # ── PHASE 1: Brain-first lookup ──
        if _should_skip_memory(request.message):
            yield f"data: {json.dumps({'type': 'pipeline', 'level': 'pipeline', 'step': 'memory_search_skip', 'reason': 'greeting_or_cmd'})}\n\n"
            brain_results = []
            brain_context = None
            parent_id = await db.insert_interaction(
                request.user_id, request.session_id, role="tool",
                content=json.dumps({"skipped": True, "reason": "greeting_or_cmd"}),
                parent_id=user_interaction_id,
                tool_name="memory_search",
                channel="web_portal",
                metadata=json.dumps({"brain": True, "skipped": True}),
                sender_id=agent["id"],
                receiver_id=agent["id"],
            )
            yield f"data: {json.dumps({'type': 'pipeline', 'level': 'pipeline', 'step': 'memory_search_end', 'results_count': 0, 'results': [], 'skipped': True})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'pipeline', 'level': 'pipeline', 'step': 'memory_search_start', 'query': request.message, 'limit': 5})}\n\n"

            brain_results = await db.memory_search(request.user_id, request.message, limit=5)
            brain_context = None

            yield f"data: {json.dumps({'type': 'pipeline', 'level': 'pipeline', 'step': 'memory_search_end', 'results_count': len(brain_results), 'results': [{'slug': r['slug'], 'title': r.get('title', r['slug']), 'score': round(r.get('rank', 0), 2)} for r in (brain_results or [])]})}\n\n"

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

            search_content = json.dumps({
                "query": request.message,
                "results": [
                    {"slug": r["slug"], "title": r.get("title",""), "score": round(r.get("rank", 0), 2), "snippet": r.get("compiled_truth", "")[:150]}
                    for r in (brain_results or [])
                ],
                "count": len(brain_results or []),
            }, indent=2)
            parent_id = await db.insert_interaction(
                request.user_id, request.session_id, role="tool",
                content=search_content,
                parent_id=user_interaction_id,
                tool_name="memory_search",
                channel="web_portal",
                metadata=json.dumps({
                    "count": len(brain_results or []),
                    "brain": True,
                    "has_results": bool(brain_results),
                }),
                sender_id=agent["id"],
                receiver_id=agent["id"],
            )

            yield f"data: {json.dumps({'type': 'tool_result', 'level': 'agent', 'tool': 'memory_search', 'result': search_content[:2000], 'duration_ms': 0, 'error': False})}\n\n"

        # ── Resolve attachment references (SSE) ──
        attachment_context = None
        if request.attachment_ids:
            attachment_docs = []
            for att_id in request.attachment_ids:
                att = await db.get_attachment(att_id)
                if att:
                    attachment_docs.append(att)
            if attachment_docs:
                attachment_context = format_attachments_for_prompt(attachment_docs)
                yield f"data: {json.dumps({'type': 'attachment', 'level': 'agent', 'attachments': [{'id': a['id'], 'original_name': a['original_name'], 'mime_type': a['mime_type'], 'size_bytes': a['size_bytes'], 'storage_path': a.get('storage_path', '')} for a in attachment_docs]})}\n\n"

        system_prompt = await build_system_prompt(
            context_docs, brain_context, request.user_id,
            agent_system_prompt=agent.get("system_prompt"),
        )
        if attachment_context:
            system_prompt = system_prompt + "\n\n" + attachment_context

        from app.tools.loader import load_tools
        tools = await load_tools(request.user_id)
        
        yield f"data: {json.dumps({'type': 'pipeline', 'level': 'pipeline', 'step': 'build_prompt', 'sections': ['SYSTEM'], 'brain_injected': bool(brain_context), 'tool_count_in_prompt': len(tools)})}\n\n"

        exclude_ids = {user_interaction_id} if user_interaction_id else set()
        history = await build_openai_history_from_session(
            db, request.user_id, request.session_id,
            exclude_interaction_ids=exclude_ids,
        )

        q = asyncio.Queue()

        async def run_agent_task():
            assistant_reply = ""
            try:
                async for event in stream_agent_events(
                    user_id=request.user_id,
                    session_id=request.session_id,
                    user_message=request.message,
                    system_prompt=system_prompt,
                    agent_id=agent["id"],
                    history=history,
                    parent_interaction_id=parent_id,
                    max_turns=agent.get("max_turn_count", 10),
                    channel="web_portal",
                ):
                    await q.put(event)
                    
                    if event["type"] == "response":
                        assistant_reply = event["content"]
                    elif event["type"] == "error" and not assistant_reply:
                        assistant_reply = f"I encountered an error: {event['message']}"
                    elif event["type"] == "interrupted" and not assistant_reply:
                        assistant_reply = f"I was interrupted: {event['message']}"

                asyncio.create_task(_save_chat_to_memory(
                    db, request.user_id, request.session_id,
                    request.message, assistant_reply, agent["id"], parent_id,
                ))

                await q.put({'type': 'pipeline', 'level': 'pipeline', 'step': 'memory_save_start', 'slug': f'chat/{request.session_id[:8]}'})
            finally:
                await q.put(None) # Signal end of stream
        
        # Start agent loop in the background!
        asyncio.create_task(run_agent_task())

        # Stream from the queue — also broadcast to session + user listeners
        while True:
            event = await q.get()
            if event is None:
                break
            # Broadcast to WebSocket listeners (session + user)
            await _emit_to_visualizers(request.session_id, event, user_id=request.user_id)
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


async def _save_chat_to_memory(
    db, user_id: str, session_id: str,
    user_message: str, assistant_reply: str, agent_id: str,
    parent_interaction_id: Optional[str] = None,
) -> None:
    """Save chat conversation to memory as visible tool interaction."""
    try:
        # Save chat as a memory page
        slug = f"chat/{session_id[:8]}"
        result = await db.memory_upsert(
            user_id, slug, "meeting",
            title=f"Session {session_id[:8]}",
            compiled_truth=assistant_reply[:500],
            timeline=user_message[:200],
        )

        # Save memory_save as visible tool interaction
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
            channel="web_portal",
            metadata=json.dumps({"brain": True, "slug": slug}),
            sender_id=agent_id,
            receiver_id=agent_id,
        )

        # Emit to visualizer and user listeners
        await _emit_to_visualizers(session_id, {
            "type": "pipeline", "level": "pipeline",
            "step": "memory_save_end", "slug": slug,
        }, user_id=user_id)
        await _emit_to_visualizers(session_id, {
            "type": "db", "level": "db",
            "op": "memory_upsert", "slug": slug, "page_type": "meeting",
        }, user_id=user_id)
        logger.debug("Saved chat to memory: %s", slug)
    except Exception as e:
        logger.warning("Failed to save chat to memory: %s", e)


# ── Visualizer listener registry ──
# WebSocket subscribers that receive pipeline events for HTTP chat sessions.
_visualizer_listeners: Dict[str, List[Any]] = {}  # session_id → list of WebSocket objects


def register_visualizer_listener(session_id: str, websocket: Any):
    """Register a WebSocket as a visualizer listener for a session."""
    if session_id not in _visualizer_listeners:
        _visualizer_listeners[session_id] = []
    _visualizer_listeners[session_id].append(websocket)


def unregister_visualizer_listener(session_id: str, websocket: Any):
    """Remove a WebSocket from the visualizer listeners."""
    if session_id in _visualizer_listeners:
        _visualizer_listeners[session_id] = [
            ws for ws in _visualizer_listeners[session_id] if ws is not websocket
        ]
        if not _visualizer_listeners[session_id]:
            del _visualizer_listeners[session_id]


async def _emit_to_visualizers(session_id: str, event: Dict[str, Any], user_id: Optional[str] = None):
    """Push an event to all visualizer listeners for a session, and optionally user listeners."""
    import json
    listeners = _visualizer_listeners.get(session_id, [])
    disconnected = []
    for ws in listeners:
        try:
            await ws.send_text(json.dumps(event))
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        unregister_visualizer_listener(session_id, ws)
    # Also broadcast to per-user listeners if user_id provided
    if user_id:
        await _emit_to_user_listeners(user_id, event)


# ── User listener registry (per-user) ──
_user_listeners: Dict[str, List[Any]] = {}  # user_id → list of WebSocket objects


def register_user_listener(user_id: str, websocket: Any):
    """Register a WebSocket that receives events for all of a user's sessions."""
    if user_id not in _user_listeners:
        _user_listeners[user_id] = []
    _user_listeners[user_id].append(websocket)


def unregister_user_listener(user_id: str, websocket: Any):
    """Remove a WebSocket from the per-user listeners."""
    if user_id in _user_listeners:
        _user_listeners[user_id] = [
            ws for ws in _user_listeners[user_id] if ws is not websocket
        ]
        if not _user_listeners[user_id]:
            del _user_listeners[user_id]


async def _emit_to_user_listeners(user_id: str, event: Dict[str, Any]):
    """Push an event to all per-user listeners."""
    import json
    listeners = _user_listeners.get(user_id, [])
    disconnected = []
    for ws in listeners:
        try:
            await ws.send_text(json.dumps(event))
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        unregister_user_listener(user_id, ws)

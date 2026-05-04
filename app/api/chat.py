"""Chat endpoint for webAgent."""

import json
import logging
from typing import List, Any, Dict
from fastapi import APIRouter, HTTPException
from app.models.schemas import ChatRequest, ChatResponse
from app.db import get_db
from app.agent.prompts import build_system_prompt

from app.agent.loop import run_agent_loop
import asyncio

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """
    Process a chat message with the agent.

    Uses the simple agent loop with tool-calling support.
    """
    try:
        db = get_db()

        # Save user message and get its ID for parent linking
        user_interaction_id = await db.insert_interaction(
            request.user_id, request.session_id, role="user", content=request.message,
            metadata=json.dumps({"source": "web_portal_chat"}),
        )

        # ── Emit user message to visualizer listeners ──
        await _emit_to_visualizers(request.session_id, {
            "type": "user_message", "level": "user",
            "content": request.message, "id": user_interaction_id,
        })

        # Fetch context documents; if empty, copy defaults for this user
        context_docs = await db.fetch_context_documents(
            request.user_id,
            ["agent", "user", "skills", "tools", "tasks", "memory", "project", "jobs"],
        )
        if not context_docs:
            copied = await db.copy_defaults_to_user(request.user_id)
            if copied > 0:
                context_docs = await db.fetch_context_documents(
                    request.user_id,
                    ["agent", "user", "skills", "tools", "tasks", "memory", "project", "jobs"],
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

        # Always save memory_search as tool interaction (even empty)
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
            metadata=json.dumps({
                "count": len(brain_results or []),
                "brain": True,
                "has_results": bool(brain_results),
            }),
        )

        # Emit memory_search as a tool result
        await _emit_to_visualizers(request.session_id, {
            "type": "tool_result", "level": "agent",
            "tool": "memory_search",
            "result": search_content[:2000],
            "duration_ms": 0,
            "error": False,
        })

        # Build system prompt with brain context + dynamic tools
        system_prompt = await build_system_prompt(
            context_docs, brain_context, request.user_id
        )

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

        # Convert history to the format expected by our agent
        history = []
        if request.history:
            for msg in request.history:
                history.append({"role": msg.role, "content": msg.content})

        # Create event callback that pushes to visualizer listeners
        async def event_callback(event: Dict[str, Any]):
            await _emit_to_visualizers(request.session_id, event)

        # Run the agent loop
        assistant_reply = await run_agent_loop(
            user_id=request.user_id,
            session_id=request.session_id,
            user_message=request.message,
            system_prompt=system_prompt,
            history=history,
            parent_interaction_id=parent_id,
            event_callback=event_callback,
        )

        # ── PHASE 3: Background memory save (visible tool interaction) ──
        asyncio.create_task(_save_chat_to_memory(
            db, request.user_id, request.session_id,
            request.message, assistant_reply, parent_id,
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


async def _save_chat_to_memory(
    db, user_id: str, session_id: str,
    user_message: str, assistant_reply: str,
    parent_interaction_id: str | None = None,
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
            metadata=json.dumps({"brain": True, "slug": slug}),
        )

        # Emit to visualizer
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


async def _emit_to_visualizers(session_id: str, event: Dict[str, Any]):
    """Push an event to all visualizer listeners for a session."""
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

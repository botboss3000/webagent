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

        # ── PHASE 1: Brain-first lookup (visible as tool interaction) ──
        brain_results = await db.memory_search(request.user_id, request.message, limit=5)
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

        # Build system prompt with brain context + dynamic tools
        system_prompt = await build_system_prompt(
            context_docs, brain_context, request.user_id
        )

        # Convert history to the format expected by our agent
        history = []
        if request.history:
            for msg in request.history:
                history.append({"role": msg.role, "content": msg.content})

        # Run the agent loop
        assistant_reply = await run_agent_loop(
            user_id=request.user_id,
            session_id=request.session_id,
            user_message=request.message,
            system_prompt=system_prompt,
            history=history,
            parent_interaction_id=parent_id,
        )

        # ── PHASE 3: Background memory save (visible tool interaction) ──
        asyncio.create_task(_save_chat_to_memory(
            db, request.user_id, request.session_id,
            request.message, assistant_reply, parent_id,
        ))

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
        logger.debug("Saved chat to memory: %s", slug)
    except Exception as e:
        logger.warning("Failed to save chat to memory: %s", e)

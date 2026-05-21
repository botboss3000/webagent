"""Chat endpoint for webAgent."""

import asyncio
import json
import logging
import os
import re
import sqlite3
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
from app.agent.loop_executor import LoopConfig
from app.agent.session_history import build_openai_history_from_session
from app.optimizer.runner import run_optimizer_async
from app.agent import trigger_index

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


async def _enforce_agent_access_policy(db, agent: dict, user_id: str) -> None:
    """Raise 403 if user is not allowed to chat with this agent under its user_mode policy."""
    mode = (agent or {}).get("user_mode") or "anonymous"
    if mode == "anonymous":
        return
    # Global admin always allowed
    try:
        if await db.is_user_admin(user_id):
            return
    except Exception:
        pass
    roles = await db.get_agent_roles(agent["id"])
    if user_id in (roles.get("admin_users") or []):
        return
    if mode == "register":
        # Look up the channel identity for this user_id. If anonymous tier, refuse.
        conn = db._get_conn()
        try:
            row = conn.execute(
                "SELECT user_tier FROM channel_identities WHERE user_id = ? ORDER BY updated_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        finally:
            conn.close()
        tier = (row["user_tier"] if row else None) or "anonymous"
        if tier == "anonymous":
            raise HTTPException(status_code=403, detail="This agent requires a registered account.")
        return
    if mode == "authorized":
        if user_id not in (roles.get("authorized_users") or []):
            raise HTTPException(status_code=403, detail="This agent requires admin authorization for new users.")
        return


async def _ensure_session(db, user_id: str, session_id: str, title: str = None) -> None:
    """Create the session row if it doesn't exist yet, and update its title on first real message."""
    conn = db._get_conn()
    try:
        row = conn.execute("SELECT title FROM sessions WHERE id=?", (session_id,)).fetchone()
    finally:
        conn.close()

    if row is None:
        # Session doesn't exist — create it
        try:
            raw = db.get_raw_client()
            raw.table("sessions").insert({
                "id": session_id,
                "user_id": user_id,
                "title": title or "New Session",
            }).execute()
            logger.info(f"Created session {session_id[:12]} for user {user_id[:12]}")
        except Exception as create_err:
            logger.warning(f"Session creation failed (may already exist): {create_err}")
    elif title and row[0] in (None, "New Session", session_id[:12]):
        # Session exists with placeholder title — update to first real message
        conn = db._get_conn()
        try:
            conn.execute("UPDATE sessions SET title=? WHERE id=?", (title, session_id))
            conn.commit()
        finally:
            conn.close()

class InterruptRequest(BaseModel):
    session_id: str

def _match_slash_command(message: str):
    """Match message against all slash_command triggers from the trigger index.

    Returns (trigger_key, arg, template_id) if matched, else None.
    trigger_key is e.g. '/optimize', arg is the text after the command.
    """
    stripped = (message or "").strip()
    if not stripped.startswith("/"):
        return None
    slash_cmds = trigger_index.get_slash_commands()
    for trigger_key, template_id in slash_cmds.items():
        pattern = re.compile(
            r"^" + re.escape(trigger_key) + r"\s*(.*)$",
            re.IGNORECASE | re.DOTALL,
        )
        m = pattern.match(stripped)
        if m:
            return trigger_key, m.group(1).strip(), template_id
    return None


async def _handle_generic_slash_command(
    template_id: str,
    trigger_key: str,
    arg: str,
    user_id: str,
    session_id: str,
    channel: str,
    db,
) -> str:
    """Generic handler for slash commands that don't have a custom runner.

    Creates a new session bound to the matched agent template and returns
    a user-facing confirmation message.
    """
    new_sid = f"slash-{uuid.uuid4().hex[:12]}"
    try:
        raw = db.get_raw_client()
        raw.table("sessions").insert({
            "id": new_sid,
            "user_id": user_id,
            "title": f"{trigger_key} session",
            "metadata": json.dumps({"trigger_key": trigger_key, "arg": arg}),
        }).execute()
    except Exception as e:
        logger.warning("Could not create session for %s: %s", trigger_key, e)
        return f"Could not start `{trigger_key}` — session creation failed."

    return (
        f"**{trigger_key}** session started.\n"
        f"Session ID: `{new_sid}`\n"
        + (f"Input: {arg}\n" if arg else "")
        + f"\nOpen the session to continue."
    )


async def _handle_optimize_command(
    user_id: str,
    session_id: str,
    feedback: str,
    channel: str,
    db,
) -> str:
    """Run the optimizer against the user's current session.
    feedback is the text after the slash command (may be empty).
    Returns a user-facing message."""

    # Find the user's most recent real session (not optimizer-*)
    import sqlite3
    try:
        conn = sqlite3.connect("app/db/local.db")
        row = conn.execute(
            "SELECT id FROM sessions WHERE user_id=? AND id NOT LIKE 'optimizer-%' AND id NOT LIKE 'worker-%' AND id NOT LIKE 'closer-%' ORDER BY created_at DESC LIMIT 1",
            (user_id,)
        ).fetchone()
        target_session = row[0] if row else ""
        conn.close()
    except Exception:
        target_session = ""

    if not target_session:
        return "No chat session found to optimize. Send a few messages first, then try /optimize."

    # Run optimizer inline (fast: no LLM calls, just prefilter + session setup)
    opt_sid = await run_optimizer_async(
        user_id=user_id,
        session_id=target_session,
        channel=channel,
        feedback=feedback,
        force=True,
    )

    msg = f"⚡ **Optimization session created!**\n"
    msg += f"• Target: `{target_session[:8]}`\n"
    msg += f"• Optimizer: `{opt_sid}`\n" if opt_sid else ""
    msg += f"• Feedback: {feedback}\n" if feedback else ""
    msg += f"\nOpen the optimizer session in the UI to review the analysis and discuss changes with the Planner."
    return msg


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

        # ── Temp DB resolution for optimizer/closer sessions ──
        _temp_db_path = None
        if request.session_id.startswith('optimizer-') or request.session_id.startswith('closer-'):
            _meta_conn = db._get_conn()
            try:
                _meta_row = _meta_conn.execute(
                    "SELECT metadata FROM sessions WHERE id=?", (request.session_id,)
                ).fetchone()
                if _meta_row and _meta_row[0]:
                    _meta = json.loads(_meta_row[0])
                    _temp_db_path = _meta.get('temp_db_path')
            finally:
                _meta_conn.close()
            if _temp_db_path:
                from app.db.local import LocalBackend as _OptBackend
                db = _OptBackend(db_path=_temp_db_path)
                logger.info("Using temp DB for %s session: %s", request.session_id[:12], _temp_db_path)

        # ── Handle slash commands ──
        _slash_match = _match_slash_command(request.message or "")
        if _slash_match:
            _slash_key, _slash_arg, _slash_tid = _slash_match
            if _slash_tid == "opt_planner":
                result = await _handle_optimize_command(
                    request.user_id, request.session_id,
                    _slash_arg, "web_portal", db,
                )
            else:
                result = await _handle_generic_slash_command(
                    _slash_tid, _slash_key, _slash_arg,
                    request.user_id, request.session_id, "web_portal", db,
                )
            return ChatResponse(reply=result, response=result, session_id=request.session_id)

        # Ensure the session exists before inserting interactions
        _session_title = (request.message or "").strip()[:60] or None
        await _ensure_session(db, request.user_id, request.session_id, title=_session_title)

        # ── Optimizer / Finalizer session: route to dedicated agent ──
        agent = None
        opt_role = None
        opt_template_id = None
        opt_metadata = {}
        if request.session_id.startswith('optimizer-') or request.session_id.startswith('closer-'):
            conn = db._get_conn()
            try:
                meta_row = conn.execute(
                    "SELECT metadata FROM sessions WHERE id=?", (request.session_id,)
                ).fetchone()
                opt_metadata = json.loads(meta_row[0]) if meta_row and meta_row[0] else {}
                if request.session_id.startswith('closer-'):
                    opt_role = 'closer'
                    opt_template_id = trigger_index.get('tool_call', 'handoff_to_closer') or 'opt_closer'
                else:
                    opt_role = opt_metadata.get('opt_role', 'planner')
                    opt_template_id = (
                        trigger_index.get('tool_call', 'run_optimizer') or 'opt_planner'
                        if opt_role == 'planner'
                        else trigger_index.get('tool_call', 'handoff_to_closer') or 'opt_closer'
                    )
            finally:
                conn.close()

            # Resolve agent in main local.db so it is accessible for UI edits,
            # then bind the session in temp DB if one is active.
            _agent_db = get_db() if _temp_db_path else db
            agent = await _agent_db.get_or_resolve_session_agent(
                session_id=request.session_id,
                user_id=request.user_id,
                template_id=opt_template_id,
            )
            if _temp_db_path and agent and agent.get("id"):
                try:
                    await db.bind_session_to_agent(request.session_id, agent["id"])
                except Exception:
                    pass
            if not agent:
                raise RuntimeError(
                    f"Failed to resolve optimizer agent (role={opt_role}) for user {request.user_id}. "
                    f"Check that agent template '{opt_template_id}' exists."
                )

        # ── Assign agent first (context rows are keyed by agent_id) ──
        if agent is None and getattr(request, 'agent_id', None):
            agent = await db.get_agent_by_id(request.agent_id)
        if agent is None:
            agent = await db.get_agent_for_user(request.user_id)
        if agent is None:
            agent = await db.create_agent_for_user(request.user_id)
            await _emit_to_visualizers(request.session_id, {
                "type": "pipeline", "level": "pipeline",
                "step": "agent_assigned",
                "agent_id": agent["id"],
                "max_turn_count": agent["max_turn_count"],
            })

        # ── Agent access policy enforcement ──
        await _enforce_agent_access_policy(db, agent, request.user_id)

        # ── Participants enforcement ──
        # Ensure the user and agent are registered as participants
        if not await db.is_session_participant(request.session_id, request.user_id, 'user'):
            await db.add_session_participant(request.session_id, request.user_id, 'user')
        if not await db.is_session_participant(request.session_id, agent["id"], 'agent'):
            await db.add_session_participant(request.session_id, agent["id"], 'agent')

        # Save user message and get its ID for parent linking
        # Optimizer/Finalizer sessions get source='optimizer' to distinguish from normal chats
        is_opt = request.session_id.startswith('optimizer-') or request.session_id.startswith('closer-')
        user_interaction_id = await db.insert_interaction(
            request.user_id, request.session_id, role="user", content=request.message,
            channel="web_portal",
            metadata=json.dumps({"source": "optimizer" if is_opt else "web_portal_chat"}),
            input_data=json.dumps(request.model_dump(), default=str),
            sender_id=request.user_id,
            receiver_id=agent["id"],
            source="optimizer" if is_opt else None,
        )

        # ── Emit user message to visualizer listeners ──
        await _emit_to_visualizers(request.session_id, {
            "type": "user_message", "level": "user",
            "content": request.message, "id": user_interaction_id,
        })

        # ── Build loop config for pre-loop gating ──
        loop_config = LoopConfig.from_agent(agent)

        # ── Agent context docs (already included by get_or_resolve_session_agent / get_agent_for_user) ──
        context_docs = agent.get("context_documents", [])

        # Non-optimizer agents: copy defaults if no context docs exist
        if not context_docs and not is_opt and loop_config.is_enabled("copy_defaults"):
            copied = await db.copy_defaults_to_agent(agent["id"], template_id='default')
            if copied > 0:
                agent = await db.fetch_agent_by_id_with_context(agent["id"], CONTEXT_SECTION_TYPES, user_id=request.user_id)
                context_docs = agent.get("context_documents", [])

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
        if not loop_config.is_enabled("memory_search") or _should_skip_memory(request.message):
            _skip_reason = "node_disabled" if not loop_config.is_enabled("memory_search") else "greeting_or_cmd"
            await _emit_to_visualizers(request.session_id, {
                "type": "pipeline", "level": "pipeline",
                "step": "memory_search_skip", "reason": _skip_reason,
            })
            brain_results = []
            brain_context = None
            parent_id = await db.insert_interaction(
                request.user_id, request.session_id, role="tool",
                content=json.dumps({"skipped": True, "reason": _skip_reason}),
                parent_id=user_interaction_id,
                tool_name="memory_search",
                channel="web_portal",
                metadata=json.dumps({"brain": True, "skipped": True, "reason": _skip_reason}),
                input_data=json.dumps({"query": request.message, "skipped": True}),
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
                input_data=json.dumps({"query": request.message}),
                output_data=search_content,
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
        # context_docs is already the resolved per-caller slot list.
        _agent_id_for_prompt = agent.get("id") if agent else None
        system_prompt = await build_system_prompt(
            context_docs, brain_context, request.user_id,
            agent_id=_agent_id_for_prompt,
        )
        if attachment_context:
            system_prompt = system_prompt + "\n\n" + attachment_context

        # ── Pipeline: prompt built ──
        from app.tools.loader import load_tools
        tools = await load_tools(request.user_id,
                                 agent_id=_agent_id_for_prompt or "",
                                 agent_template_id=agent.get("template_id") if agent else None,
                                 is_admin_agent=bool(agent.get("is_admin_agent")) if agent else False)
        tool_count_for_prompt = len(tools)
        section_names = ["SYSTEM"]  # Simplified section count — actual sections are dynamic

        await _emit_to_visualizers(request.session_id, {
            "type": "pipeline", "level": "pipeline",
            "step": "build_prompt", "sections": section_names,
            "brain_injected": bool(brain_context),
            "tool_count_in_prompt": tool_count_for_prompt,
        })

        # Emit data_src_load telemetry so the loop node lights up.
        try:
            if _agent_id_for_prompt:
                _ds_attached = await db.agent_data_source_list(_agent_id_for_prompt, enabled_only=True)
            else:
                _ds_attached = []
        except Exception:
            _ds_attached = []
        await _emit_to_visualizers(request.session_id, {
            "type": "pipeline", "level": "pipeline",
            "step": "data_src_loaded",
            "attached_count": len(_ds_attached),
            "sources": [
                {"name": a.get("name"), "type": a.get("type"), "tool_alias": a.get("tool_alias")}
                for a in _ds_attached
            ],
        })

        # DB-backed conversation history (same session survives browser refresh)
        exclude_ids = {user_interaction_id} if user_interaction_id else set()
        # Build conversation history from DB. For closer sessions, all context
        # (judging criteria, baseline, trial transcripts) is pre-injected as real
        # interaction rows in the temp DB by handoff_to_closer, so the standard
        # history builder works for both planner and closer sessions.
        history = await build_openai_history_from_session(
            db, request.user_id, request.session_id,
            exclude_interaction_ids=exclude_ids,
        )

        # Create event callback that pushes to visualizer and user listeners
        async def event_callback(event: Dict[str, Any]):
            await _emit_to_visualizers(request.session_id, event, user_id=request.user_id)

        # Run the agent loop (with 5-minute timeout)
        # Resolve allowed_tools from agent config (may be list or JSON string)
        _raw_allowed = agent.get("allowed_tools", [])
        if isinstance(_raw_allowed, str):
            import json as _json
            try:
                _raw_allowed = _json.loads(_raw_allowed)
            except Exception:
                _raw_allowed = []
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
            timeout_seconds=300,
            db=db,
            agent_template_id=agent.get("template_id"),
            allowed_tools=_raw_allowed or None,
        )

        # ── PHASE 3: Background memory save (visible tool interaction) ──
        # Skip if agent has disabled memory_save via allowed_tools or loop_logic
        if 'memory_save' not in set(_raw_allowed or []) and loop_config.is_enabled("memory_save"):
            asyncio.create_task(_save_chat_to_memory(
                db, request.user_id, request.session_id,
                request.message, assistant_reply, agent["id"], parent_id,
            ))
            # ── Pipeline: memory save notification ──
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

    # ── Temp DB resolution for optimizer/closer sessions ──
    _temp_db_path = None
    if request.session_id.startswith('optimizer-') or request.session_id.startswith('closer-'):
        _meta_conn = db._get_conn()
        try:
            _meta_row = _meta_conn.execute(
                "SELECT metadata FROM sessions WHERE id=?", (request.session_id,)
            ).fetchone()
            if _meta_row and _meta_row[0]:
                _meta = json.loads(_meta_row[0])
                _temp_db_path = _meta.get('temp_db_path')
        finally:
            _meta_conn.close()
        if _temp_db_path:
            from app.db.local import LocalBackend as _OptBackend
            db = _OptBackend(db_path=_temp_db_path)
            logger.info("Using temp DB for %s session: %s", request.session_id[:12], _temp_db_path)

    # ── Handle slash commands (streaming) ──
    _slash_match = _match_slash_command(request.message or "")
    if _slash_match:
        _slash_key, _slash_arg, _slash_tid = _slash_match
        if _slash_tid == "opt_planner":
            result = await _handle_optimize_command(
                request.user_id, request.session_id,
                request.message, "web_portal", db,
            )
        else:
            result = await _handle_generic_slash_command(
                _slash_tid, _slash_key, _slash_arg,
                request.user_id, request.session_id, "web_portal", db,
            )
        async def _slash_events():
            yield f"data: {json.dumps({'type': 'stream', 'level': 'agent', 'content': result})}\n\n"
            yield f"data: {json.dumps({'type': 'response', 'level': 'agent', 'content': result})}\n\n"
        return StreamingResponse(_slash_events(), media_type="text/event-stream")

    # Ensure the session exists before inserting interactions
    _session_title = (request.message or "").strip()[:60] or None
    await _ensure_session(db, request.user_id, request.session_id, title=_session_title)

    # ── Optimizer / Finalizer session: route to dedicated agent ──
    opt_template_id = None
    opt_metadata = {}
    if request.session_id.startswith('optimizer-') or request.session_id.startswith('closer-'):
        conn = db._get_conn()
        try:
            meta_row = conn.execute(
                "SELECT metadata FROM sessions WHERE id=?", (request.session_id,)
            ).fetchone()
            opt_metadata = json.loads(meta_row[0]) if meta_row and meta_row[0] else {}
            if request.session_id.startswith('closer-'):
                opt_role = 'closer'
                opt_template_id = trigger_index.get('tool_call', 'handoff_to_closer') or 'opt_closer'
            else:
                opt_role = opt_metadata.get('opt_role', 'planner')
                opt_template_id = (
                    trigger_index.get('tool_call', 'run_optimizer') or 'opt_planner'
                    if opt_role == 'planner'
                    else trigger_index.get('tool_call', 'handoff_to_closer') or 'opt_closer'
                )
        finally:
            conn.close()

        # Resolve agent in main local.db so it is accessible for UI edits,
        # then bind the session in temp DB if one is active.
        _agent_db = get_db() if _temp_db_path else db
        agent = await _agent_db.get_or_resolve_session_agent(
            session_id=request.session_id,
            user_id=request.user_id,
            template_id=opt_template_id,
        )
        if _temp_db_path and agent and agent.get("id"):
            try:
                await db.bind_session_to_agent(request.session_id, agent["id"])
            except Exception:
                pass
        if not agent:
            raise RuntimeError(
                f"Failed to resolve optimizer agent (role={opt_role}) for user {request.user_id}. "
                f"Check that agent template '{opt_template_id}' exists."
            )
    else:
        req_agent_id = getattr(request, 'agent_id', None)
        req_template = getattr(request, 'agent_template_id', None)
        if req_template == 'admin-agent':
            if not await db.is_user_admin(request.user_id):
                raise HTTPException(status_code=403, detail="Admin agent is only available to admin users.")
            agent = await db.get_or_resolve_session_agent(
                session_id=request.session_id,
                user_id=request.user_id,
                template_id='admin-agent',
            )
        elif req_agent_id:
            agent = await db.get_agent_by_id(req_agent_id)
            if agent:
                admin_users = agent.get("admin_users") or []
                if isinstance(admin_users, str):
                    import json as _json
                    try:
                        admin_users = _json.loads(admin_users)
                    except Exception:
                        admin_users = []
                if request.user_id not in admin_users:
                    await db.add_agent_member(req_agent_id, request.user_id)
        else:
            agent = await db.get_agent_for_user(request.user_id)
        if agent is None:
            agent = await db.create_agent_for_user(request.user_id)

    # ── Agent access policy enforcement ──
    await _enforce_agent_access_policy(db, agent, request.user_id)

    # -- Bind session to agent --
    existing_agent_id = await db.get_session_agent_id(request.session_id)
    if existing_agent_id is None:
        await db.bind_session_to_agent(request.session_id, agent["id"])
    elif existing_agent_id != agent["id"]:
        raise RuntimeError(
            f"Session {request.session_id[:8]} bound to agent {existing_agent_id[:8]}, "
            f"but resolved agent is {agent['id'][:8]}. Cannot respond."
        )

    # ── Participants enforcement ──
    if not await db.is_session_participant(request.session_id, request.user_id, 'user'):
        await db.add_session_participant(request.session_id, request.user_id, 'user')
    if not await db.is_session_participant(request.session_id, agent["id"], 'agent'):
        await db.add_session_participant(request.session_id, agent["id"], 'agent')

    # Save user message and get its ID for parent linking
    user_interaction_id = await db.insert_interaction(
        request.user_id, request.session_id, role="user", content=request.message,
        channel="web_portal",
        metadata=json.dumps({"source": "web_portal_chat_sse"}),
        input_data=json.dumps(request.model_dump(), default=str),
        sender_id=request.user_id,
        receiver_id=agent["id"],
    )

    async def event_generator():
        nonlocal agent
        # Re-fetch to include context_documents (for non-optimizer agents that
        # went through get_agent_for_user rather than get_or_resolve_session_agent).
        # For optimizer agents, get_or_resolve_session_agent already includes them.
        if not agent.get("context_documents"):
            _fetched = await db.fetch_agent_by_id_with_context(agent["id"], CONTEXT_SECTION_TYPES, user_id=request.user_id)
            if _fetched is not None:
                agent = _fetched

        # Build loop config for pre-loop gating
        loop_config = LoopConfig.from_agent(agent)

        yield f"data: {json.dumps({'type': 'pipeline', 'level': 'pipeline', 'step': 'agent_assigned', 'agent_id': agent['id'], 'max_turn_count': agent.get('max_turn_count', 10)})}\n\n"

        if not agent.get("context_documents") and loop_config.is_enabled("copy_defaults"):
            copied = await db.copy_defaults_to_agent(agent["id"], template_id='default')
            if copied > 0:
                agent = await db.fetch_agent_by_id_with_context(agent["id"], CONTEXT_SECTION_TYPES, user_id=request.user_id)

        context_docs = agent.get("context_documents", [])

        doc_types = list(set(
            (d.get("context_type") or d.get("doc_type") or "")
            for d in context_docs if d.get("context_type") or d.get("doc_type")
        ))
        yield f"data: {json.dumps({'type': 'pipeline', 'level': 'pipeline', 'step': 'load_context', 'count': len(context_docs), 'types': doc_types})}\n\n"

        # ── PHASE 1: Brain-first lookup ──
        if not loop_config.is_enabled("memory_search") or _should_skip_memory(request.message):
            _skip_reason = "node_disabled" if not loop_config.is_enabled("memory_search") else "greeting_or_cmd"
            yield f"data: {json.dumps({'type': 'pipeline', 'level': 'pipeline', 'step': 'memory_search_skip', 'reason': _skip_reason})}\n\n"
            brain_results = []
            brain_context = None
            parent_id = await db.insert_interaction(
                request.user_id, request.session_id, role="tool",
                content=json.dumps({"skipped": True, "reason": _skip_reason}),
                parent_id=user_interaction_id,
                tool_name="memory_search",
                channel="web_portal",
                metadata=json.dumps({"brain": True, "skipped": True, "reason": _skip_reason}),
                input_data=json.dumps({"query": request.message, "skipped": True}),
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
                input_data=json.dumps({"query": request.message}),
                output_data=search_content,
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

        _agent_id_for_prompt_sse = agent.get("id") if agent else None
        system_prompt = await build_system_prompt(
            context_docs, brain_context, request.user_id,
            agent_id=_agent_id_for_prompt_sse,
        )
        if attachment_context:
            system_prompt = system_prompt + "\n\n" + attachment_context

        from app.tools.loader import load_tools
        tools = await load_tools(request.user_id,
                                 agent_id=_agent_id_for_prompt_sse or "",
                                 agent_template_id=agent.get("template_id") if agent else None,
                                 is_admin_agent=bool(agent.get("is_admin_agent")) if agent else False)

        yield f"data: {json.dumps({'type': 'pipeline', 'level': 'pipeline', 'step': 'build_prompt', 'sections': ['SYSTEM'], 'brain_injected': bool(brain_context), 'tool_count_in_prompt': len(tools), 'system_prompt': system_prompt[:8000]})}\n\n"

        exclude_ids = {user_interaction_id} if user_interaction_id else set()
        # All optimizer/closer context is pre-injected as real interaction rows
        # in the session's temp DB by handoff_to_closer, so the standard history
        # builder works for all session types.
        history = await build_openai_history_from_session(
            db, request.user_id, request.session_id,
            exclude_interaction_ids=exclude_ids,
        )

        q = asyncio.Queue()

        async def run_agent_task():
            assistant_reply = ""
            TIMEOUT_SEC = 300  # 5 min total timeout for the agent loop
            # Resolve allowed_tools from agent config (may be list or JSON string)
            _raw_at = agent.get("allowed_tools", [])
            if isinstance(_raw_at, str):
                import json as _json2
                try:
                    _raw_at = _json2.loads(_raw_at)
                except Exception:
                    _raw_at = []
            try:
                async def _run():
                    nonlocal assistant_reply
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
                        db=db,
                        agent_template_id=agent.get("template_id"),
                        allowed_tools=_raw_at or None,
                    ):
                        await q.put(event)

                        if event["type"] == "response":
                            assistant_reply = event["content"]
                        elif event["type"] == "error" and not assistant_reply:
                            assistant_reply = f"I encountered an error: {event['message']}"
                        elif event["type"] == "interrupted" and not assistant_reply:
                            assistant_reply = f"I was interrupted: {event['message']}"

                # Wrap the agent loop with a timeout
                await asyncio.wait_for(_run(), timeout=TIMEOUT_SEC)

                # Skip memory save if agent disabled it via allowed_tools or loop_logic
                if 'memory_save' not in set(_raw_at or []) and loop_config.is_enabled("memory_save"):
                    asyncio.create_task(_save_chat_to_memory(
                        db, request.user_id, request.session_id,
                        request.message, assistant_reply, agent["id"], parent_id,
                    ))
                    await q.put({'type': 'pipeline', 'level': 'pipeline', 'step': 'memory_save_start', 'slug': f'chat/{request.session_id[:8]}'})
            except asyncio.TimeoutError:
                logger.warning("SSE agent task timed out for session %s", request.session_id)
                await q.put({
                    "type": "error", "level": "agent",
                    "message": f"The request timed out after {TIMEOUT_SEC} seconds. Please try again or simplify your request.",
                })
            except Exception as _task_err:
                import traceback as _tb
                logger.error("run_agent_task error: %s\n%s", _task_err, _tb.format_exc())
                await q.put({
                    "type": "error", "level": "agent",
                    "message": str(_task_err),
                })
            finally:
                await q.put(None)  # Signal end of stream
        
        # Start agent loop in the background!
        asyncio.create_task(run_agent_task())

        # Stream from the queue — also broadcast to session + user listeners
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=20)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if event is None:
                break
            # Broadcast to WebSocket listeners (session + user)
            await _emit_to_visualizers(request.session_id, event, user_id=request.user_id)
            yield f"data: {json.dumps(event)}\n\n"

    async def safe_event_generator():
        try:
            async for chunk in event_generator():
                yield chunk
        except Exception as e:
            logger.error("event_generator unhandled error: %s", e, exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'level': 'agent', 'message': str(e)})}\n\n"

    return StreamingResponse(safe_event_generator(), media_type="text/event-stream")


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
            input_data=json.dumps({"user_message": user_message[:200], "assistant_reply": assistant_reply[:200]}),
            output_data=save_content,
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

"""Chat endpoint for webAgent."""

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from typing import List, Any, Dict, Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from app.models.schemas import ChatRequest, ChatResponse
from app.db import get_db
from app.agent.prompts import (
    build_system_prompt,
    build_user_message_content,
    format_attachments_for_prompt,
    CONTEXT_SECTION_TYPES,
)

from app.agent.loop import run_agent_loop_buffered, stream_agent_events
from app.agent.loop_executor import LoopConfig
from app.agent.session_history import build_openai_history_from_session
from app.agent.run_buffer import get_registry as get_run_buffer_registry
from app.agent.run_manager import get_run_manager
from app.optimizer.runner import run_optimizer_async
from app.agent import trigger_index
from app.billing.enforcement import check_access as billing_check_access

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


def _session_title_from_message(message: str, max_words: int = 6) -> str:
    """Extract a concise 3–6 word session title from the user's first message."""
    words = (message or "").strip().split()
    if not words:
        return "New Session"
    title = " ".join(words[:max_words]).rstrip(".,!?;: ")
    return title[:60] or "New Session"


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


async def _enforce_billing_access(db, agent: dict, user_id: str) -> None:
    """Gate chat on billing state — credits, subscription, or trial.

    Agents with no billing config (or strategy='free', or where the user is
    exempt) pass through. Otherwise we raise HTTP 402 with a structured
    reason so the frontend can show the right paywall."""
    try:
        decision = await billing_check_access(agent, user_id, db)
    except Exception as e:
        logger.debug("billing access check failed (allowing): %s", e)
        return
    if decision.allow:
        return
    raise HTTPException(status_code=402, detail=decision.to_dict())


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


class ResumeRequest(BaseModel):
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
    try:
        from app.db import get_db as _get_db
        conn = _get_db()._get_conn()
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
    """Request a graceful interruption for an ongoing chat session.

    Interrupt is the ONLY thing (besides finishing or a server restart) that
    stops a supervised run. Sets the DB flag the agent loop polls; the loop
    finalizes its partial answer as 'interrupted' and flips run-state."""
    try:
        db = get_db()
        was_running = await get_run_manager().interrupt(request.session_id, db)
        return {"status": "ok", "message": "Interrupt requested.", "was_running": was_running}
    except Exception as e:
        logger.error(f"Error setting interrupt: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/resume")
async def resume_chat(request: ResumeRequest):
    """Manually re-ignite a stopped run (the one-click path for a run held as
    'needs_manual_resume' by the auto-resume opt-out, or any interrupted/failed
    turn the user wants to continue). Backend-driven — works even with no live
    WebSocket. The resumed turn streams into the chat via the normal event path."""
    try:
        from app.agent.runner import manual_resume
        ok = await manual_resume(request.session_id)
        return {"status": "ok" if ok else "noop",
                "resumed": ok,
                "message": "Resuming." if ok else "Nothing to resume (already running or not resumable)."}
    except Exception as e:
        logger.error("Error resuming run %s: %s", request.session_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/self-heal/status")
async def self_heal_status():
    """Observability: the liveness watchdog's status + counters, plus the list of
    runs currently awaiting a manual one-click resume."""
    try:
        from app.agent.watchdog import get_watchdog
        wd = await get_watchdog().get_status()
    except Exception as e:
        wd = {"error": str(e)}
    manual: List[Dict[str, Any]] = []
    try:
        db = get_db()
        conn = db._get_conn()
        try:
            rows = conn.execute(
                "SELECT session_id, user_id, origin, resume_attempts, error, updated_at "
                "FROM session_runs WHERE stop_cause='needs_manual_resume' "
                "ORDER BY updated_at DESC LIMIT 100"
            ).fetchall()
            manual = [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        manual = []
    return {"watchdog": wd, "awaiting_manual_resume": manual}



@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest, fastapi_request: Request):
    """
    Process a chat message with the agent.

    Uses the simple agent loop with tool-calling support.
    """
    try:
        # Tenant isolation: the JWT subject must match the user_id the
        # client says it's chatting as. Every tool wrapper down the call
        # graph closes over this user_id, so getting it wrong here lets one
        # authenticated user impersonate another for the whole session.
        from app.auth.identity import assert_caller_is
        request.user_id = await assert_caller_is(fastapi_request, request.user_id)
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
        _session_title = _session_title_from_message(request.message) if (request.message or "").strip() else None
        await _ensure_session(db, request.user_id, request.session_id, title=_session_title)

        # ── Optimizer / Closer session: route to dedicated agent ──
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
            raise HTTPException(
                status_code=400,
                detail="No agent assigned. Create an agent before chatting.",
            )

        # ── Agent access policy enforcement ──
        await _enforce_agent_access_policy(db, agent, request.user_id)
        await _enforce_billing_access(db, agent, request.user_id)

        # ── Participants enforcement ──
        # Ensure the user and agent are registered as participants
        if not await db.is_session_participant(request.session_id, request.user_id, 'user'):
            await db.add_session_participant(request.session_id, request.user_id, 'user')
        if not await db.is_session_participant(request.session_id, agent["id"], 'agent'):
            await db.add_session_participant(request.session_id, agent["id"], 'agent')

        # Save user message and get its ID for parent linking
        # Optimizer/Closer sessions get source='optimizer' to distinguish from normal chats
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

        # ── Start a run buffer for this turn ──
        _run_buffer = await get_run_buffer_registry().start_turn(
            session_id=request.session_id,
            user_id=request.user_id,
            turn_id=user_interaction_id,
            db=db,
        )
        try:
            _user_ss, _user_ts = _run_buffer.next_seq()
            _conn = db._get_conn()
            try:
                _conn.execute(
                    "UPDATE interactions SET session_seq=?, turn_id=?, turn_seq=? WHERE id=?",
                    (_user_ss, user_interaction_id, _user_ts, user_interaction_id),
                )
                _conn.commit()
            finally:
                _conn.close()
        except Exception as _seqerr:
            logger.debug("Failed to backfill seq on user row (buffered): %s", _seqerr)

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
        attachment_docs: List[Dict[str, Any]] = []
        if request.attachment_ids:
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
        _desc_out = {}
        async for _dev in _maybe_describe_images(
            db, request.user_id, request.message, user_interaction_id,
            loop_config, attachment_docs, _desc_out,
        ):
            await _emit_to_visualizers(request.session_id, _dev)
        user_message_content = await build_user_message_content(
            _desc_out.get("message_text", request.message),
            _desc_out.get("inline_docs", attachment_docs),
        )

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
                                 agent_template_id=agent.get("template_id") if agent else None)
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
            user_message=user_message_content,
            system_prompt=system_prompt,
            agent_id=agent["id"],
            history=history,
            parent_interaction_id=parent_id,
            event_callback=event_callback,
            max_turns=agent.get("max_turn_count", 0),
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

        # End the run buffer for this turn — starts the retention countdown.
        try:
            await get_run_buffer_registry().end_turn(request.session_id)
        except Exception as _eb:
            logger.debug("end_turn failed (buffered) for session %s: %s", request.session_id, _eb)

        return ChatResponse(
            reply=assistant_reply,
            response=assistant_reply,
            session_id=request.session_id,
        )

    except Exception as e:
        # Make sure we mark the run buffer ended even on error path.
        try:
            await get_run_buffer_registry().end_turn(request.session_id)
        except Exception:
            pass
        logger.error(f"Chat endpoint error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def _prepare_send(request: ChatRequest, fastapi_request: Request) -> Dict[str, Any]:
    """Synchronous prep shared by /send and /stream.

    Does everything that must happen *before* we hand the turn to the Run
    Manager: auth, agent resolution + access/billing gating, session + participant
    setup, persisting the user message, and emitting the user_message event so it
    shows on every device instantly. Returns a dict the background turn executor
    needs, or ``{"slash_result": "..."}`` when the message was a slash command.

    NOTE: the RunBuffer + run-state are started inside the turn coroutine (not
    here), so that when a new message INTERRUPTS an active run, the prior run's
    finalize and the new run's begin can't race on the single session_runs row.
    A new message never refuses — it interrupts the current run (see
    RunManager.start_or_replace).
    """
    from app.auth.identity import assert_caller_is
    request.user_id = await assert_caller_is(fastapi_request, request.user_id)
    db = get_db()
    channel = "web_portal"

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
                request.user_id, request.session_id, request.message, channel, db,
            )
        else:
            result = await _handle_generic_slash_command(
                _slash_tid, _slash_key, _slash_arg,
                request.user_id, request.session_id, channel, db,
            )
        return {"slash_result": result}

    # Ensure the session exists before inserting interactions
    _session_title = _session_title_from_message(request.message) if (request.message or "").strip() else None
    await _ensure_session(db, request.user_id, request.session_id, title=_session_title)

    # ── Optimizer / Closer session: route to dedicated agent ──
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
        if req_template in ('admin-agent', 'integration-admin-agent'):
            if not await db.is_user_admin(request.user_id):
                raise HTTPException(status_code=403, detail="This agent is only available to admin users.")
            agent = await db.get_or_resolve_session_agent(
                session_id=request.session_id,
                user_id=request.user_id,
                template_id=req_template,
            )
        elif req_agent_id:
            agent = await db.get_agent_by_id(req_agent_id)
            if agent:
                admin_users = agent.get("admin_users") or []
                if isinstance(admin_users, str):
                    try:
                        admin_users = json.loads(admin_users)
                    except Exception:
                        admin_users = []
                if request.user_id not in admin_users:
                    await db.add_agent_member(req_agent_id, request.user_id)
        else:
            agent = await db.get_agent_for_user(request.user_id)
        if agent is None:
            raise HTTPException(
                status_code=400,
                detail="No agent assigned. Create an agent before chatting.",
            )

    # ── Access policy + billing enforcement ──
    await _enforce_agent_access_policy(db, agent, request.user_id)
    await _enforce_billing_access(db, agent, request.user_id)

    # ── Bind session to agent ──
    existing_agent_id = await db.get_session_agent_id(request.session_id)
    if existing_agent_id is None:
        await db.bind_session_to_agent(request.session_id, agent["id"])
    elif existing_agent_id != agent["id"]:
        raise RuntimeError(
            f"Session {request.session_id[:8]} bound to agent {existing_agent_id[:8]}, "
            f"but resolved agent is {agent['id'][:8]}. Cannot respond."
        )

    # ── Participants ──
    if not await db.is_session_participant(request.session_id, request.user_id, 'user'):
        await db.add_session_participant(request.session_id, request.user_id, 'user')
    if not await db.is_session_participant(request.session_id, agent["id"], 'agent'):
        await db.add_session_participant(request.session_id, agent["id"], 'agent')

    # ── Persist the user message ──
    user_interaction_id = await db.insert_interaction(
        request.user_id, request.session_id, role="user", content=request.message,
        channel=channel,
        metadata=json.dumps({"source": "web_portal_chat"}),
        input_data=json.dumps(request.model_dump(), default=str),
        sender_id=request.user_id,
        receiver_id=agent["id"],
    )

    # ── Emit the user message so all subscribed devices render it instantly ──
    # (The RunBuffer + run-state for the new turn are started inside the turn
    # coroutine. If an old run is still active, its buffer stamps this event;
    # seq stays monotonic across the interrupt.)
    await _emit_to_visualizers(request.session_id, {
        "type": "user_message", "level": "user",
        "content": request.message, "id": user_interaction_id,
    }, user_id=request.user_id)

    return {
        "db": db,
        "agent": agent,
        "user_interaction_id": user_interaction_id,
        "channel": channel,
    }


async def _run_turn_background(
    db, request: ChatRequest, agent: Dict[str, Any],
    user_interaction_id: str, channel: str = "web_portal",
    replaced: bool = False,
) -> None:
    """Execute one agent turn to completion, fully decoupled from any client
    connection. Owned by the Run Manager — survives the sender leaving, closing
    the browser, switching sessions/devices. Every event is emitted via
    ``_emit_to_visualizers`` (→ RunBuffer stamp + per-user WS broadcast), and the
    agent loop streams its answer into the DB. On finish, run-state is flipped to
    its terminal status and the RunBuffer retention countdown begins.

    ``replaced`` is True when this turn is replacing a run the user just
    interrupted by sending a new message; the agent is told so it can read the
    new message as a course-correction / stop / addition relative to its
    interrupted partial answer."""
    session_id = request.session_id
    user_id = request.user_id
    final_status = "complete"
    _last_seq_persist = 0.0

    # Start the RunBuffer + durable run-state for THIS turn. Done here (not in
    # _prepare_send) so a replaced run's begin happens strictly after the prior
    # run's finalize — no race on the single session_runs row.
    _run_buffer = await get_run_buffer_registry().start_turn(
        session_id=session_id, user_id=user_id, turn_id=user_interaction_id, db=db,
    )
    try:
        # origin='web' + a relaunch recipe so the self-healing layer can re-ignite
        # this turn headlessly (boot recovery / watchdog) if it dies involuntarily.
        _web_relaunch_ctx = json.dumps({
            "origin": "web", "session_id": session_id, "user_id": user_id,
            "agent_id": agent.get("id"), "channel": channel,
            "turn_id": user_interaction_id,
        })
        await db.run_state_begin(
            session_id, user_id, agent.get("id"), user_interaction_id,
            origin="web", relaunch_ctx=_web_relaunch_ctx,
        )
    except Exception as _rse:
        logger.debug("run_state_begin failed: %s", _rse)
    # Backfill seq on the already-saved user row from the buffer's first slot.
    try:
        _user_ss, _user_ts = _run_buffer.next_seq()
        _conn = db._get_conn()
        try:
            _conn.execute(
                "UPDATE interactions SET session_seq=?, turn_id=?, turn_seq=? WHERE id=?",
                (_user_ss, user_interaction_id, _user_ts, user_interaction_id),
            )
            _conn.commit()
        finally:
            _conn.close()
    except Exception as _seqerr:
        logger.debug("Failed to backfill seq on user row: %s", _seqerr)

    async def event_callback(event: Dict[str, Any]):
        nonlocal final_status, _last_seq_persist
        await _emit_to_visualizers(session_id, event, user_id=user_id)
        et = event.get("type")
        if et == "interrupted":
            final_status = "interrupted"
        elif et == "error":
            final_status = "error"
        # Throttled advance of the durable latest_session_seq (drives WS resume
        # for cold devices). The RunBuffer holds the real events; this is just a
        # cheap pointer so a fresh device knows where the live stream is up to.
        ss = event.get("session_seq")
        if ss is not None:
            now = time.monotonic()
            if now - _last_seq_persist > 1.0:
                _last_seq_persist = now
                try:
                    await db.run_state_update_seq(session_id, int(ss))
                except Exception:
                    pass

    try:
        # Re-fetch agent with context documents if missing.
        nonlocal_agent = agent
        if not nonlocal_agent.get("context_documents"):
            _fetched = await db.fetch_agent_by_id_with_context(
                nonlocal_agent["id"], CONTEXT_SECTION_TYPES, user_id=user_id)
            if _fetched is not None:
                nonlocal_agent = _fetched
        agent = nonlocal_agent

        loop_config = LoopConfig.from_agent(agent)

        await event_callback({
            "type": "pipeline", "level": "pipeline", "step": "agent_assigned",
            "agent_id": agent["id"], "max_turn_count": agent.get("max_turn_count", 0),
        })

        if not agent.get("context_documents") and loop_config.is_enabled("copy_defaults"):
            copied = await db.copy_defaults_to_agent(agent["id"], template_id='default')
            if copied > 0:
                agent = await db.fetch_agent_by_id_with_context(
                    agent["id"], CONTEXT_SECTION_TYPES, user_id=user_id)

        context_docs = agent.get("context_documents", [])
        doc_types = list(set(
            (d.get("context_type") or d.get("doc_type") or "")
            for d in context_docs if d.get("context_type") or d.get("doc_type")
        ))
        await event_callback({
            "type": "pipeline", "level": "pipeline", "step": "load_context",
            "count": len(context_docs), "types": doc_types,
        })

        # ── PHASE 1: Brain-first lookup ──
        if not loop_config.is_enabled("memory_search") or _should_skip_memory(request.message):
            _skip_reason = "node_disabled" if not loop_config.is_enabled("memory_search") else "greeting_or_cmd"
            await event_callback({"type": "pipeline", "level": "pipeline",
                                  "step": "memory_search_skip", "reason": _skip_reason})
            brain_context = None
            parent_id = await db.insert_interaction(
                user_id, session_id, role="tool",
                content=json.dumps({"skipped": True, "reason": _skip_reason}),
                parent_id=user_interaction_id, tool_name="memory_search", channel=channel,
                metadata=json.dumps({"brain": True, "skipped": True, "reason": _skip_reason}),
                input_data=json.dumps({"query": request.message, "skipped": True}),
                sender_id=agent["id"], receiver_id=agent["id"],
            )
            await event_callback({"type": "pipeline", "level": "pipeline",
                                  "step": "memory_search_end", "results_count": 0,
                                  "results": [], "skipped": True})
        else:
            await event_callback({"type": "pipeline", "level": "pipeline",
                                  "step": "memory_search_start", "query": request.message, "limit": 5})
            brain_results = await db.memory_search(request.user_id, request.message, limit=5)
            brain_context = None
            await event_callback({
                "type": "pipeline", "level": "pipeline", "step": "memory_search_end",
                "results_count": len(brain_results),
                "results": [{"slug": r["slug"], "title": r.get("title", r["slug"]),
                             "score": round(r.get("rank", 0), 2)} for r in (brain_results or [])],
            })
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
                    {"slug": r["slug"], "title": r.get("title", ""), "score": round(r.get("rank", 0), 2),
                     "snippet": r.get("compiled_truth", "")[:150]}
                    for r in (brain_results or [])
                ],
                "count": len(brain_results or []),
            }, indent=2)
            parent_id = await db.insert_interaction(
                user_id, session_id, role="tool", content=search_content,
                parent_id=user_interaction_id, tool_name="memory_search", channel=channel,
                metadata=json.dumps({"count": len(brain_results or []), "brain": True,
                                     "has_results": bool(brain_results)}),
                input_data=json.dumps({"query": request.message}), output_data=search_content,
                sender_id=agent["id"], receiver_id=agent["id"],
            )
            await event_callback({"type": "tool_result", "level": "agent", "tool": "memory_search",
                                  "result": search_content[:2000], "duration_ms": 0, "error": False})

        # ── Resolve attachments + vision fallback ──
        attachment_context = None
        attachment_docs: List[Dict[str, Any]] = []
        if request.attachment_ids:
            for att_id in request.attachment_ids:
                att = await db.get_attachment(att_id)
                if att:
                    attachment_docs.append(att)
            if attachment_docs:
                attachment_context = format_attachments_for_prompt(attachment_docs)
                await event_callback({
                    "type": "attachment", "level": "agent",
                    "attachments": [
                        {"id": a["id"], "original_name": a["original_name"],
                         "mime_type": a["mime_type"], "size_bytes": a["size_bytes"],
                         "storage_path": a.get("storage_path", "")}
                        for a in attachment_docs
                    ],
                })
        _desc_out = {}
        async for _dev in _maybe_describe_images(
            db, request.user_id, request.message, user_interaction_id,
            loop_config, attachment_docs, _desc_out,
        ):
            await event_callback(_dev)
        user_message_content = await build_user_message_content(
            _desc_out.get("message_text", request.message),
            _desc_out.get("inline_docs", attachment_docs),
        )

        _agent_id_for_prompt = agent.get("id") if agent else None
        system_prompt = await build_system_prompt(
            context_docs, brain_context, request.user_id, agent_id=_agent_id_for_prompt)
        if attachment_context:
            system_prompt = system_prompt + "\n\n" + attachment_context

        from app.tools.loader import load_tools
        tools = await load_tools(request.user_id, agent_id=_agent_id_for_prompt or "",
                                 agent_template_id=agent.get("template_id") if agent else None)
        await event_callback({
            "type": "pipeline", "level": "pipeline", "step": "build_prompt", "sections": ["SYSTEM"],
            "brain_injected": bool(brain_context), "tool_count_in_prompt": len(tools),
            "system_prompt": system_prompt[:8000],
        })

        try:
            _ds_attached = await db.agent_data_source_list(_agent_id_for_prompt, enabled_only=True) if _agent_id_for_prompt else []
        except Exception:
            _ds_attached = []
        await event_callback({
            "type": "pipeline", "level": "pipeline", "step": "data_src_loaded",
            "attached_count": len(_ds_attached),
            "sources": [{"name": a.get("name"), "type": a.get("type"), "tool_alias": a.get("tool_alias")}
                        for a in _ds_attached],
        })

        history = await build_openai_history_from_session(
            db, request.user_id, request.session_id,
            exclude_interaction_ids={user_interaction_id} if user_interaction_id else set(),
        )

        # If this turn replaced one the user interrupted, tell the agent so it
        # reads the new message as a course-correction / stop / addition relative
        # to the partial answer it had started. The agent decides what to do.
        if replaced:
            history.append({
                "role": "system",
                "content": (
                    "The user sent a new message while you were still responding, so your "
                    "previous answer was interrupted (you can see your partial reply above). "
                    "Read their new message carefully and respond to it: they may be telling "
                    "you to STOP (acknowledge briefly and stop), steering you in a different "
                    "direction (adjust accordingly), or adding information (incorporate it). "
                    "Do not simply repeat your interrupted answer."
                ),
            })

        _raw_at = agent.get("allowed_tools", [])
        if isinstance(_raw_at, str):
            try:
                _raw_at = json.loads(_raw_at)
            except Exception:
                _raw_at = []

        assistant_reply = ""
        _exec_mode = getattr(request, 'execution_mode', 'ask') or 'ask'
        async for event in stream_agent_events(
            user_id=request.user_id, session_id=request.session_id,
            user_message=user_message_content, system_prompt=system_prompt,
            agent_id=agent["id"], history=history, parent_interaction_id=parent_id,
            max_turns=agent.get("max_turn_count", 0), channel=channel, db=db,
            agent_template_id=agent.get("template_id"), allowed_tools=_raw_at or None,
            execution_mode=_exec_mode,
        ):
            await event_callback(event)
            if event["type"] == "response":
                assistant_reply = event["content"]
            elif event["type"] == "error" and not assistant_reply:
                assistant_reply = f"I encountered an error: {event['message']}"
            elif event["type"] == "interrupted" and not assistant_reply:
                assistant_reply = f"I was interrupted: {event['message']}"

        # ── Background memory save ──
        if 'memory_save' not in set(_raw_at or []) and loop_config.is_enabled("memory_save"):
            asyncio.create_task(_save_chat_to_memory(
                db, request.user_id, request.session_id,
                request.message, assistant_reply, agent["id"], parent_id,
            ))
            await event_callback({"type": "pipeline", "level": "pipeline",
                                  "step": "memory_save_start", "slug": f"chat/{request.session_id[:8]}"})
    except asyncio.CancelledError:
        # Hard-cancelled (replace grace timeout, or watchdog frozen-cancel). Mark
        # interrupted so it isn't wrongly recorded 'complete'; the stop_cause was
        # already tagged by whoever requested the stop (replaced / frozen).
        final_status = "interrupted"
        raise
    except Exception as e:
        final_status = "error"
        logger.error("Background turn failed for session %s: %s", session_id, e, exc_info=True)
        try:
            await _emit_to_visualizers(session_id, {
                "type": "error", "level": "agent", "message": str(e),
            }, user_id=user_id)
        except Exception:
            pass
    finally:
        # Derive the machine cause from the terminal status. A voluntary cause
        # (user_stop / replaced) already on the row is preserved by run_state_finish.
        _web_cause = ("complete" if final_status == "complete"
                      else "crash" if final_status == "error" else None)
        try:
            await db.run_state_finish(session_id, status=final_status, stop_cause=_web_cause)
        except Exception as _rsf:
            logger.debug("run_state_finish failed for %s: %s", session_id, _rsf)
        try:
            await get_run_buffer_registry().end_turn(session_id)
        except Exception as _eb:
            logger.debug("end_turn failed for session %s: %s", session_id, _eb)


async def _resume_web_turn(rc: Dict[str, Any], replaced: bool):
    """Self-healing resume builder for the 'web' origin. Re-ignites an
    involuntarily-stopped interactive turn from durable history, emitting through
    the SAME RunBuffer + WebSocket path a live turn uses, so an attached chat sees
    the continuation stream in. Run-state begin/finish are owned by the runner;
    this only executes the turn and returns the outcome."""
    from app.agent.runner import RunOutcome, RESUME_NUDGE
    db = get_db()
    session_id = rc.get("session_id")
    user_id = rc.get("user_id")
    # Use the session's CURRENT agent — a mid-session delegation may have rebound it.
    agent_id = await db.get_session_agent_id(session_id) or rc.get("agent_id")
    agent = None
    if agent_id:
        agent = await db.fetch_agent_by_id_with_context(
            agent_id, CONTEXT_SECTION_TYPES, user_id=user_id)
        if agent is None:
            agent = await db.get_agent_by_id(agent_id)
    if not agent:
        return RunOutcome(status="error", stop_cause="failed",
                          error="agent not found for web resume")

    final_status = "complete"
    reply = ""
    _run_buffer = await get_run_buffer_registry().start_turn(
        session_id=session_id, user_id=user_id, turn_id=rc.get("turn_id"), db=db,
    )

    async def event_callback(event: Dict[str, Any]):
        nonlocal final_status
        await _emit_to_visualizers(session_id, event, user_id=user_id)
        et = event.get("type")
        if et == "interrupted":
            final_status = "interrupted"
        elif et == "error":
            final_status = "error"
        ss = event.get("session_seq")
        if ss is not None:
            try:
                await db.run_state_update_seq(session_id, int(ss))
            except Exception:
                pass

    try:
        loop_config = LoopConfig.from_agent(agent)
        context_docs = agent.get("context_documents", [])
        system_prompt = await build_system_prompt(
            context_docs, None, user_id, agent_id=agent_id)
        history = await build_openai_history_from_session(db, user_id, session_id)
        _raw_at = agent.get("allowed_tools", [])
        if isinstance(_raw_at, str):
            try:
                _raw_at = json.loads(_raw_at)
            except Exception:
                _raw_at = []
        await event_callback({
            "type": "resumed", "level": "agent",
            "reason": rc.get("resume_reason", "server_restart"),
        })
        async for event in stream_agent_events(
            user_id=user_id, session_id=session_id, user_message=RESUME_NUDGE,
            system_prompt=system_prompt, agent_id=agent_id, history=history,
            max_turns=agent.get("max_turn_count", 0), channel=rc.get("channel"), db=db,
            agent_template_id=agent.get("template_id"), allowed_tools=_raw_at or None,
        ):
            await event_callback(event)
            if event["type"] == "response":
                reply = event["content"]
    except asyncio.CancelledError:
        final_status = "interrupted"
        raise
    except Exception as e:
        final_status = "error"
        logger.error("web resume failed for session %s: %s", session_id, e, exc_info=True)
    finally:
        try:
            await get_run_buffer_registry().end_turn(session_id)
        except Exception:
            pass

    from app.agent.runner import RunOutcome as _RO
    cause = ("complete" if final_status == "complete"
             else "crash" if final_status == "error" else None)
    return _RO(status=final_status, stop_cause=cause, reply=reply)


# Register the web-origin resume builder so the self-healing layer (boot recovery
# + liveness watchdog) can re-ignite interactive turns with UI streaming intact.
try:
    from app.agent.runner import register_resume_builder as _rrb
    _rrb("web", _resume_web_turn)
except Exception as _rrb_err:  # pragma: no cover
    logger.debug("Could not register web resume builder: %s", _rrb_err)


async def _sse_tail_run(session_id: str):
    """SSE fallback: tail the RunBuffer for a session and yield events as they
    appear, then stop when the run completes. Fully decoupled from the run — if
    this client disconnects, the supervised run keeps going. The WebSocket
    subscriber is the primary live path; this exists so the old streaming
    endpoint keeps working."""
    reg = get_run_buffer_registry()
    rm = get_run_manager()
    last = 0
    idle = 0.0
    while True:
        buf = reg.get(session_id)
        if buf is not None:
            missed = buf.replay_after(last)
            for ev in missed:
                last = ev.get("session_seq", last)
                yield f"data: {json.dumps(ev)}\n\n"
            if buf.completed_at is not None and not buf.replay_after(last):
                break
        else:
            if not rm.is_running(session_id):
                break
        if not rm.is_running(session_id):
            # run ended; drain any final buffered events then stop
            buf = reg.get(session_id)
            if buf is None or not buf.replay_after(last):
                break
        await asyncio.sleep(0.08)
        idle += 0.08
        if idle >= 20:
            idle = 0.0
            yield ": ping\n\n"


@router.post("/send")
async def chat_send(request: ChatRequest, fastapi_request: Request):
    """Fire-and-forget send. Saves the user message, starts the agent turn as a
    supervised background run, and returns immediately. All output (including for
    the sending device) is rendered from the DB + the per-user WebSocket — see
    /api/v1/agent/ws and /api/v1/db/session-messages. Leaving, closing the
    browser, switching sessions/devices does NOT interrupt the run.

    A new message sent while the agent is still working **interrupts** the
    current run and starts a fresh one that includes the interrupted partial +
    the new message; the agent then decides whether to stop, steer, or continue.
    """
    prep = await _prepare_send(request, fastapi_request)
    if "slash_result" in prep:
        return {"status": "ok", "session_id": request.session_id, "reply": prep["slash_result"]}

    status = await get_run_manager().start_or_replace(
        session_id=request.session_id,
        user_id=request.user_id,
        turn_id=prep["user_interaction_id"],
        db=prep["db"],
        run_factory=lambda replaced: _run_turn_background(
            prep["db"], request, prep["agent"], prep["user_interaction_id"],
            prep["channel"], replaced=replaced),
    )
    return {
        "status": status,  # "running" or "replacing"
        "session_id": request.session_id,
        "turn_id": prep["user_interaction_id"],
    }


@router.post("/stream")
async def chat_stream(request: ChatRequest, fastapi_request: Request):
    """
    SSE fallback for sending a message. Behaves like /send (saves the message and
    starts a supervised, connection-independent run) but also tails the run's
    events back over Server-Sent Events for this client. A disconnect here never
    interrupts the run — it keeps going server-side and is viewable from any
    device via the DB + WebSocket. Prefer /send + the WebSocket for new clients.
    """
    prep = await _prepare_send(request, fastapi_request)
    if "slash_result" in prep:
        result = prep["slash_result"]
        async def _slash_events():
            yield f"data: {json.dumps({'type': 'stream', 'level': 'agent', 'content': result})}\n\n"
            yield f"data: {json.dumps({'type': 'response', 'level': 'agent', 'content': result})}\n\n"
        return StreamingResponse(_slash_events(), media_type="text/event-stream")

    await get_run_manager().start_or_replace(
        session_id=request.session_id,
        user_id=request.user_id,
        turn_id=prep["user_interaction_id"],
        db=prep["db"],
        run_factory=lambda replaced: _run_turn_background(
            prep["db"], request, prep["agent"], prep["user_interaction_id"],
            prep["channel"], replaced=replaced),
    )

    async def safe_event_generator():
        try:
            async for chunk in _sse_tail_run(request.session_id):
                yield chunk
        except Exception as e:
            logger.error("SSE tail unhandled error: %s", e, exc_info=True)
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


async def _maybe_describe_images(db, user_id, message, user_interaction_id,
                                 loop_config, attachment_docs, out):
    """Attachment Description step (async generator).

    When an image is attached and the model(s) that will handle this turn cannot
    see images, describe each image once via a separately-configured vision model
    and fold the description into the user message as text — and persist it into
    the user row so later turns retain it (the image itself is never stored).

    Yields pipeline event dicts for the caller to emit on its own transport
    (``await _emit_to_visualizers`` on the buffered path, ``yield`` on SSE) and
    writes results into ``out``:
      out["message_text"]  → text to send as the user message
      out["inline_docs"]   → attachments to pass to build_user_message_content
                             (images removed when described, so nothing re-inlines)
    """
    from app.agent.prompts import _VISION_INLINE_MIMES, describe_image_attachment

    out["message_text"] = message
    out["inline_docs"] = attachment_docs

    image_atts = [a for a in (attachment_docs or [])
                  if (a.get("mime_type") or "").lower() in _VISION_INLINE_MIMES]
    if not image_atts or not loop_config.is_enabled("attachment_describe"):
        return

    from app.admin.settings import (
        load_llm_capabilities_for_user, turn_models_image_capable, pick_describer,
    )
    try:
        caps = await load_llm_capabilities_for_user(user_id)
    except Exception as e:
        logger.warning("attachment_describe: capability read failed: %s", e)
        return
    if turn_models_image_capable(caps):
        return  # a turn model can see the image natively → leave it inlined

    # Images are removed from native inlining; non-image attachments pass through.
    out["inline_docs"] = [a for a in (attachment_docs or [])
                          if (a.get("mime_type") or "").lower() not in _VISION_INLINE_MIMES]

    describer = pick_describer(caps)
    parts = [message] if message else []

    if not describer:
        for a in image_atts:
            parts.append(
                f"\n\n[Attached image — '{a.get('original_name', 'image')}']:\n"
                "(An image was attached but no vision-capable model is configured to describe it.)"
            )
        out["message_text"] = "".join(parts).strip() or (message or "")
        try:
            await db.update_interaction_content(user_interaction_id, out["message_text"])
        except Exception as e:
            logger.debug("attachment_describe: persist (no_describer) failed: %s", e)
        yield {"type": "pipeline", "level": "pipeline", "step": "attachment_describe_end",
               "image_count": len(image_atts), "status": "no_describer"}
        return

    import time as _t
    from datetime import datetime, timezone
    yield {"type": "pipeline", "level": "pipeline", "step": "attachment_describe_start",
           "image_count": len(image_atts), "vision_model": describer.get("model", "")}
    _start = _t.time()
    described = 0
    cached = 0
    for a in image_atts:
        name = a.get("original_name", "image")
        meta = a.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        desc = None
        if isinstance(meta, dict) and meta.get("vision_description") \
                and meta.get("vision_describer_model") == describer.get("model"):
            desc = meta.get("vision_description")
            cached += 1
        if not desc:
            desc = await describe_image_attachment(a, describer, user_text_hint=message)
            if desc:
                described += 1
                try:
                    await db.update_attachment_metadata(a.get("id"), {
                        "vision_description": desc,
                        "vision_describer_model": describer.get("model", ""),
                        "vision_described_at": datetime.now(timezone.utc).isoformat(),
                    })
                except Exception as e:
                    logger.debug("attachment_describe: metadata cache failed: %s", e)
        if desc:
            parts.append(f"\n\n[Attached image — '{name}']:\n{desc}")
        else:
            parts.append(f"\n\n[Attached image — '{name}']:\n(Image could not be described.)")

    out["message_text"] = "".join(parts).strip() or (message or "")
    try:
        await db.update_interaction_content(user_interaction_id, out["message_text"])
    except Exception as e:
        logger.debug("attachment_describe: persist failed: %s", e)

    yield {"type": "pipeline", "level": "pipeline", "step": "attachment_describe_end",
           "image_count": len(image_atts), "vision_model": describer.get("model", ""),
           "described": described, "cached": cached,
           "duration_ms": int((_t.time() - _start) * 1000),
           "status": "ok" if (described or cached) else "partial"}


async def _emit_to_visualizers(session_id: str, event: Dict[str, Any], user_id: Optional[str] = None):
    """Push an event to all visualizer listeners for a session, and optionally user listeners.

    Side effect: if a RunBuffer is active for this session, the event is
    stamped with session_seq / turn_id / turn_seq / emit_time before broadcast,
    so reconnecting clients can replay events newer than their last seen seq.
    """
    import json
    # Tag the event with its originating session so per-USER WebSocket
    # subscribers (which receive events for ALL of the user's sessions) can
    # route it to the right session and NOT render it into whatever session
    # happens to be active. Without this, live events arrive untagged and the
    # frontend's session filter fails open. Set before stamp_event so the copy
    # stored in the RunBuffer (used for replay) carries it too.
    event["session_id"] = session_id
    # Stamp via the in-memory run buffer (if a turn is active for this session).
    # This mutates `event` to add session_seq / turn_id / turn_seq / emit_time.
    try:
        _reg = get_run_buffer_registry()
        _buf = _reg.get(session_id)
        if _buf is not None:
            _buf.stamp_event(event)
    except Exception as _be:
        logger.debug("RunBuffer stamp failed for session %s: %s", session_id, _be)

    # Flight-recorder tap: keep interesting loop/tool events (pipeline problems,
    # tool errors) for post-hoc diagnosis. Cheap + swallows its own errors.
    try:
        from app.agent.diagnostics import tap_loop_event
        tap_loop_event(session_id, event)
    except Exception:
        pass

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

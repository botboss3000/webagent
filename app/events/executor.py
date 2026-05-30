"""Fire one event subscription: build prompt, run agent loop, deliver reply.

Mirrors ``app.scheduler.executor`` but the input is a ``NormalizedEvent``
plus a subscription row (not a cron tick). The event payload is injected
into the prompt, and the agent is told which delivery channels are
available so it can ask the user (per design decision: no default channel —
the agent talks to the user about where to send the message).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.events.types import NormalizedEvent

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _create_session(db, user_id: str, agent_id: str, label: str, source: str) -> str:
    session_id = f"evt-{uuid.uuid4().hex[:12]}"
    title = (label or "Event trigger")[:60]
    try:
        raw = db.get_raw_client()
        raw.table("sessions").insert({
            "id": session_id,
            "user_id": user_id,
            "title": title,
            "agent_id": agent_id,
            "metadata": json.dumps({"source": f"event:{source}"}),
        }).execute()
    except Exception as e:
        logger.warning("Event session insert via raw client failed (%s); falling back", e)
        try:
            conn = db._get_conn()
            try:
                conn.execute(
                    "INSERT INTO sessions (id, user_id, title, agent_id, metadata) VALUES (?, ?, ?, ?, ?)",
                    (session_id, user_id, title, agent_id, json.dumps({"source": f"event:{source}"})),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e2:
            logger.error("Event session insert failed: %s", e2)
            raise
    return session_id


async def _available_channels(db, user_id: str, agent_id: str) -> List[Dict[str, Any]]:
    """Thin wrapper kept for backwards compat. Real logic lives in
    `app.events.channels.list_available_channels` so the chat-level
    `list_delivery_channels` tool can reuse it without importing the executor."""
    from app.events.channels import list_available_channels
    return await list_available_channels(db, user_id=user_id, agent_id=agent_id)


def _format_event_block(event: NormalizedEvent) -> str:
    """Format the event payload for prompt injection."""
    lines = [
        "[EVENT TRIGGER]",
        f"Source: {event.source}",
        f"Type: {event.event_type}",
        f"Occurred at: {event.occurred_at}",
        f"External id: {event.external_id}",
        "",
        "Payload:",
    ]
    try:
        lines.append(json.dumps(event.payload, indent=2, ensure_ascii=False))
    except Exception:
        lines.append(str(event.payload))
    if event.raw_ref:
        lines.append("")
        lines.append("Provider refs (use with the appropriate tool to fetch full content):")
        try:
            lines.append(json.dumps(event.raw_ref, indent=2, ensure_ascii=False))
        except Exception:
            lines.append(str(event.raw_ref))
    return "\n".join(lines)


def _format_channels_overlay(channels: List[Dict[str, Any]], preferred: Optional[str]) -> str:
    lines = [
        "[DELIVERY CHANNELS]",
        ("This agent run was triggered by an external event. When you have "
         "something to tell the user about it, choose one of the channels "
         "below to deliver it on."),
        "",
    ]
    if preferred:
        lines.append(f"User-preferred channel for this trigger: **{preferred}**.")
        lines.append("If that channel is enabled below, use it. Otherwise, ask the user where to send the result.")
        lines.append("")
    else:
        lines.append("The user did not specify a delivery channel for this trigger.")
        lines.append("Ask them which channel to use (list the options below) before sending anything proactive.")
        lines.append("")
    lines.append("Available channels:")
    for c in channels:
        lines.append(f"  - {c['channel']}: {c['label']} ({c['recipient_hint']})")
    return "\n".join(lines)


async def execute_event_subscription(
    sub: Dict[str, Any],
    event: NormalizedEvent,
) -> Dict[str, Any]:
    """Run one event-triggered fire. Returns {ok, session_id, error}."""
    from app.db import get_db
    from app.agent.loop import run_agent_loop_buffered
    from app.agent.prompts import build_system_prompt
    from app.admin.integrations import is_ability_enabled_for_agent

    db = get_db()
    agent_id = sub["agent_id"]
    user_id = sub["owner_user_id"]
    prompt_text = sub.get("prompt") or ""
    preferred_channel = sub.get("channel")
    label = sub.get("task_label") or f"{sub.get('source','event')}:{sub.get('event_type','?')}"

    if not await is_ability_enabled_for_agent(agent_id, "automation"):
        logger.info(
            "Skipping event subscription %s for agent %s — automation ability is disabled",
            sub.get("id"), agent_id,
        )
        return {"ok": False, "error": "automation ability disabled"}

    try:
        agent = await db.get_agent_by_id(agent_id)
        if not agent:
            return {"ok": False, "error": f"agent {agent_id} not found"}

        # When the user subscribed with channel=webchat + channel_recipient=<session_id>,
        # honor it: post the event-triggered reply back into the user's own chat
        # session instead of spawning a fresh evt-* session that only shows up
        # in the sidebar. Validates that the named session still exists and
        # belongs to this user before reusing — otherwise falls back to evt-*.
        session_id = None
        target_recipient = (sub.get("channel_recipient") or "").strip()
        if preferred_channel == "webchat" and target_recipient:
            try:
                existing = await db.get_session(target_recipient) if hasattr(db, "get_session") else None
                if existing is None:
                    # Some backends don't have get_session — fall back to a
                    # raw lookup that any backend with raw_client supports.
                    try:
                        raw = db.get_raw_client()
                        rows = raw.table("sessions").select("id,user_id").eq("id", target_recipient).execute()
                        existing = (rows.data or [None])[0]
                    except Exception:
                        existing = None
                if existing and (existing.get("user_id") == user_id):
                    session_id = target_recipient
                    logger.info(
                        "Event sub %s reusing user session %s (channel=webchat)",
                        sub.get("id"), session_id,
                    )
            except Exception as e:
                logger.debug("Session reuse lookup failed for %s: %s", target_recipient, e)

        if not session_id:
            session_id = await _create_session(db, user_id, agent_id, label, event.source)

        resolved_slots = await db.resolve_prompts(agent_id, user_id=user_id)
        context_docs = [
            {"id": s["slot_name"], "context_type": s["slot_name"],
             "title": s["slot_name"], "content": s["content"], "tags": []}
            for s in resolved_slots
            if (s.get("content") or "").strip() and s.get("slot_name") != "automation"
        ]

        # Prepend the event block + channel overlay as synthetic context.
        channels = await _available_channels(db, user_id, agent_id)
        overlay_docs = [
            {"id": "_event_trigger", "content": _format_event_block(event)},
            {"id": "_event_channels", "content": _format_channels_overlay(channels, preferred_channel)},
        ]
        system_prompt = await build_system_prompt(
            overlay_docs + context_docs,
            brain_context=None,
            user_id=user_id,
            agent_id=agent_id,
        )

        # Build the synthetic user message: the parsed prompt plus a pointer to the event block.
        user_message = prompt_text
        if not user_message.strip():
            user_message = (
                f"A {event.source} {event.event_type} event just fired. "
                f"See [EVENT TRIGGER] in your system prompt for details and decide what to do."
            )

        user_interaction_id = None
        try:
            user_interaction_id = await db.insert_interaction(
                user_id, session_id, role="user", content=user_message,
                channel=f"event:{event.source}",
                metadata=json.dumps({
                    "source": "event_trigger",
                    "subscription_id": sub["id"],
                    "event_type": event.event_type,
                    "external_id": event.external_id,
                }),
                sender_id=user_id, receiver_id=agent_id, source="event",
            )
        except Exception as e:
            logger.debug("Could not insert synthetic user interaction: %s", e)

        # Push the synthetic user message into the user's WebSocket so the
        # chat tab shows it as the trigger before the assistant reply
        # streams in. Mirrors what `app/api/chat.py` emits for normal chats.
        try:
            from app.api.chat import _emit_to_visualizers
            await _emit_to_visualizers(session_id, {
                "type": "user_message",
                "level": "user",
                "content": user_message,
                "id": user_interaction_id,
                "source": "event_trigger",
                "event_type": event.event_type,
            }, user_id=user_id)
        except Exception as e:
            logger.debug("Could not broadcast synthetic user message: %s", e)

        raw_allowed = agent.get("allowed_tools", [])
        if isinstance(raw_allowed, str):
            try:
                raw_allowed = json.loads(raw_allowed)
            except Exception:
                raw_allowed = []

        # Broadcast loop events to the user's WebSocket so the chat tab
        # updates live when an event-triggered run posts a reply (without
        # this the chat needs a manual refresh — DB has the row, UI doesn't
        # know). Mirrors the `event_callback` the regular chat path uses.
        async def _event_loop_callback(evt: Dict[str, Any]):
            try:
                from app.api.chat import _emit_to_visualizers
                await _emit_to_visualizers(session_id, evt, user_id=user_id)
            except Exception as e:
                logger.debug("Event-run broadcast failed: %s", e)

        # Run supervised + recorded so a restart/freeze is detected and re-ignited
        # by the self-healing layer instead of silently lost.
        from app.agent.runner import run_supervised_turn, RunOutcome
        _relaunch_ctx = {
            "origin": "event", "session_id": session_id, "user_id": user_id,
            "agent_id": agent_id, "channel": "event", "timeout_seconds": 600,
            "delivery": {
                "channel": preferred_channel,
                "recipient": sub.get("channel_recipient"),
                "silent": bool(sub.get("silent")),
            },
        }

        async def _build_event_turn(replaced: bool) -> RunOutcome:
            _reply = await run_agent_loop_buffered(
                user_id=user_id,
                session_id=session_id,
                user_message=user_message,
                system_prompt=system_prompt,
                agent_id=agent_id,
                history=None,
                channel="event",
                timeout_seconds=600,
                db=db,
                agent_template_id=agent.get("template_id"),
                allowed_tools=raw_allowed or None,
                max_turns=agent.get("max_turn_count", 10),
                event_callback=_event_loop_callback,
            )
            return RunOutcome(status="complete", stop_cause="complete", reply=_reply)

        _outcome = await run_supervised_turn(
            session_id=session_id, user_id=user_id, agent_id=agent_id,
            origin="event", channel="event", relaunch_ctx=_relaunch_ctx,
            build_turn=_build_event_turn, await_result=True, result_timeout=620,
        )
        reply = (_outcome.reply if _outcome else "") or ""

        # The agent itself drives delivery via tool calls (per design: no
        # implicit channel send). If the user gave a preferred channel AND
        # the subscription is marked silent=False AND it's not webchat, the
        # executor still does a fallback send so something visible happens.
        delivery_err = None
        if (
            preferred_channel
            and preferred_channel not in ("webchat", "silent")
            and not bool(sub.get("silent"))
        ):
            delivery_err = await _fallback_deliver(
                preferred_channel,
                sub.get("channel_recipient"),
                reply or "",
            )

        return {
            "ok": True,
            "session_id": session_id,
            "error": delivery_err,
        }

    except Exception as e:
        logger.exception("Event subscription %s failed: %s", sub.get("id"), e)
        return {"ok": False, "error": str(e)[:500]}


async def _fallback_deliver(channel: str, recipient: Optional[str], text: str) -> Optional[str]:
    """Send the agent's final reply to the preferred channel as a fallback."""
    try:
        from app.communications.manager import get_plugin_manager
        pm = get_plugin_manager()
        plugin = pm.get_plugin(channel)
        if not plugin or not plugin.enabled:
            return f"channel {channel} not enabled"
        if not recipient:
            return f"channel {channel} has no recipient (agent should use send_* tool)"
        await plugin.send_message(recipient, text)
        return None
    except Exception as e:
        logger.warning("Fallback delivery failed (%s → %s): %s", channel, recipient, e)
        return str(e)[:300]

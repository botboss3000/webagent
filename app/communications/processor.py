"""
Shared message processing for communication channels.

Used by both the webhook handler (incoming from Telegram/WhatsApp) and
the polling loop (plugin pulls messages from Telegram API).
Avoids duplicating the agent loop logic.
"""

import json
import logging

from app.communications.auth import (
    get_or_create_identity,
    get_registration_system_prompt,
    get_anonymous_limit_prompt,
    upgrade_to_verified,
    verify_code,
    ChannelIdentity,
)
from app.db import get_db
from app.agent.loop import run_agent_loop_buffered
from app.agent.prompts import build_system_prompt, CONTEXT_SECTION_TYPES
from app.agent.session_history import build_openai_history_from_session

logger = logging.getLogger(__name__)


async def process_channel_message(
    channel: str,
    external_id: str,
    message_text: str,
    plugin,
) -> str:
    """
    Process an incoming message from a communication channel.

    This is the shared entry point for both webhook and polling paths.
    It handles identity resolution, session creation, and running
    the agent loop (registration or normal).

    Args:
        channel: Plugin name, e.g. 'telegram'
        external_id: Channel-specific user ID, e.g. chat_id
        message_text: Raw message text
        plugin: The CommunicationPlugin instance (for sending replies)

    Returns:
        The reply text that was sent to the user
    """
    # 1. Get or create identity
    identity = await get_or_create_identity(channel, external_id)
    user_id = identity.user_id

    # 2. Ensure a session exists
    db = get_db()
    session_id = user_id
    try:
        await db.assert_session_owned(user_id, session_id)
    except PermissionError:
        raw = db.get_raw_client()
        raw.table("sessions").insert({
            "id": session_id,
            "user_id": user_id,
            "title": f"{channel}:{external_id[-8:]}",
        }).execute()
        logger.info("Created session %s for channel user %s", session_id, user_id)

    # 3. Check for verification code reply
    if identity.user_tier == "anonymous":
        if message_text.strip().isdigit() and len(message_text.strip()) == 6:
            is_valid = await verify_code(channel, external_id, message_text.strip())
            if is_valid:
                await upgrade_to_verified(identity)
                reply = "✅ You're verified! You now have full access. How can I help you?"
                await plugin.send_message(external_id, reply)
                return reply

    # 4. Route based on user tier
    if identity.user_tier == "anonymous":
        reply = await _run_registration_agent(plugin, identity, user_id, message_text, channel)
    else:
        reply = await _run_agent_loop(plugin, identity, user_id, message_text, channel)

    # 5. Send reply
    await plugin.send_message(external_id, reply)
    return reply


async def _run_registration_agent(
    plugin, identity: ChannelIdentity, user_id: str, message_text: str, channel: str,
) -> str:
    """Run the agent loop with a registration-focused system prompt."""
    try:
        db = get_db()
        session_id = user_id

        await db.insert_interaction(
            user_id, session_id, role="user", content=message_text,
            channel=channel,
            metadata=json.dumps({"source": f"{channel}/registration"}),
        )

        agent = await db.get_agent_for_user(user_id)
        if agent is None:
            agent = await db.create_agent_for_user(user_id)

        context_docs = await db.fetch_context_documents(
            agent["id"], CONTEXT_SECTION_TYPES,
        )
        if not context_docs:
            copied = await db.copy_defaults_to_agent(agent["id"])
            if copied > 0:
                context_docs = await db.fetch_context_documents(
                    agent["id"], CONTEXT_SECTION_TYPES,
                )

        row = await db.get_agent_by_id(agent["id"])
        if row:
            agent = row

        registration_prompt = get_registration_system_prompt(identity)
        system_prompt = await build_system_prompt(
            context_docs, brain_context=None, user_id=user_id,
            agent_system_prompt=registration_prompt,
        )

        history = await build_openai_history_from_session(
            db, user_id, session_id, exclude_interaction_ids=None,
        )

        reply = await run_agent_loop_buffered(
            user_id=user_id,
            session_id=session_id,
            user_message=message_text,
            system_prompt=system_prompt,
            history=history,
            max_turns=5,
            channel=channel,
        )

        return reply
    except Exception as e:
        logger.error("Registration agent error: %s", e, exc_info=True)
        return "⚠️ Sorry, something went wrong. Please try again."


async def _run_agent_loop(
    plugin, identity: ChannelIdentity, user_id: str, message_text: str, channel: str,
) -> str:
    """Run the normal agent loop for a registered user."""
    try:
        db = get_db()
        session_id = user_id

        await db.insert_interaction(
            user_id, session_id, role="user", content=message_text,
            channel=channel,
            metadata=json.dumps({"source": f"{channel}/message"}),
        )

        agent = await db.get_agent_for_user(user_id)
        if agent is None:
            agent = await db.create_agent_for_user(user_id)

        row = await db.get_agent_by_id(agent["id"])
        if row:
            agent = row

        context_docs = await db.fetch_context_documents(
            agent["id"], CONTEXT_SECTION_TYPES,
        )
        if not context_docs:
            copied = await db.copy_defaults_to_agent(agent["id"])
            if copied > 0:
                context_docs = await db.fetch_context_documents(
                    agent["id"], CONTEXT_SECTION_TYPES,
                )

        brain_results = await db.memory_search(user_id, message_text, limit=5)
        brain_context = None
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

        agent_system_prompt = agent.get("system_prompt", "")
        if identity.user_tier == "anonymous":
            agent_system_prompt += "\n\n" + get_anonymous_limit_prompt()

        system_prompt = await build_system_prompt(
            context_docs, brain_context, user_id,
            agent_system_prompt=agent_system_prompt,
        )

        history = await build_openai_history_from_session(
            db, user_id, session_id, exclude_interaction_ids=None,
        )

        reply = await run_agent_loop_buffered(
            user_id=user_id,
            session_id=session_id,
            user_message=message_text,
            system_prompt=system_prompt,
            history=history,
            max_turns=agent.get("max_turn_count", 10),
            channel=channel,
        )

        return reply
    except Exception as e:
        logger.error("Agent loop error for %s: %s", user_id, e, exc_info=True)
        return "⚠️ Sorry, I encountered an error. Please try again."
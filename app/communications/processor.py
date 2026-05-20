"""
Shared message processing for communication channels.

Used by both the webhook handler (incoming from Telegram/WhatsApp) and
the polling loop (plugin pulls messages from Telegram API).
Avoids duplicating the agent loop logic.
"""

import json
import logging

# Display names for each channel -- shown as the session title in the UI
CHANNEL_DISPLAY_NAMES: dict[str, str] = {
    "telegram": "Telegram",
    "whatsapp": "WhatsApp",
    "sms": "SMS",
}


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
    agent_id: str | None = None,
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
        agent_id: Optional agent ID if the channel connection is per-agent

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
        try:
            raw = db.get_raw_client()
            session_row = {
                "id": session_id,
                "user_id": user_id,
                "title": CHANNEL_DISPLAY_NAMES.get(channel, channel.capitalize()),
            }
            # Attach the owning agent when known so the session is bound from the start.
            if agent_id:
                session_row["agent_id"] = agent_id
            raw.table("sessions").insert(session_row).execute()
            logger.info("Created session %s for channel user %s (agent %s)",
                        session_id, user_id, agent_id)
        except Exception as sess_err:
            # Another concurrent request may have created the session already;
            # verify ownership rather than hard-failing.
            logger.warning("Session insert failed (%s) -- checking if it already exists", sess_err)
            await db.assert_session_owned(user_id, session_id)

    # 3. Route based on agent connection config.
    if agent_id:
        await db.add_agent_member(agent_id, user_id)

        user_mode = "anonymous"
        try:
            agent_row = await db.get_agent_by_id(agent_id)
            if agent_row:
                user_mode = agent_row.get("user_mode", "anonymous")
        except Exception as cfg_err:
            logger.warning("Could not read user_mode for agent %s: %s", agent_id, cfg_err)

        if user_mode == "register" and identity.user_tier == "anonymous":
            if message_text.strip().isdigit() and len(message_text.strip()) == 6:
                is_valid = await verify_code(channel, external_id, message_text.strip())
                if is_valid:
                    await upgrade_to_verified(identity)
                    reply = "You're verified! You now have full access. How can I help you?"
                    await plugin.send_message(external_id, reply)
                    return reply
            reply = await _run_registration_agent(plugin, identity, user_id, message_text, channel, agent_id=agent_id)
        else:
            reply = await _run_agent_loop(plugin, identity, user_id, message_text, channel, agent_id=agent_id)
    else:
        # Legacy path: no specific agent known -- fall back to tier-based routing.
        if identity.user_tier == "anonymous":
            if message_text.strip().isdigit() and len(message_text.strip()) == 6:
                is_valid = await verify_code(channel, external_id, message_text.strip())
                if is_valid:
                    await upgrade_to_verified(identity)
                    reply = "You're verified! You now have full access. How can I help you?"
                    await plugin.send_message(external_id, reply)
                    return reply
            reply = await _run_registration_agent(plugin, identity, user_id, message_text, channel)
        else:
            reply = await _run_agent_loop(plugin, identity, user_id, message_text, channel)

    # 4. Send reply
    await plugin.send_message(external_id, reply)
    return reply


async def _run_registration_agent(
    plugin, identity: ChannelIdentity, user_id: str, message_text: str, channel: str,
    agent_id: str | None = None,
) -> str:
    """Run the agent loop with a registration-focused system prompt."""
    try:
        db = get_db()
        session_id = user_id

        await db.insert_interaction(
            user_id, session_id, role="user", content=message_text,
            channel=channel,
            metadata=json.dumps({"source": f"{channel}/registration"}),
            sender_id=user_id,
            receiver_id=agent_id,
        )

        # If we know which agent owns this channel, load it directly.
        # Otherwise fall back to looking up (or creating) an agent for this user.
        if agent_id:
            agent = await db.get_agent_by_id(agent_id)
            agent_with_ctx = await db.fetch_agent_by_id_with_context(agent_id, CONTEXT_SECTION_TYPES)
            if agent_with_ctx:
                agent = agent_with_ctx
        else:
            agent = await db.get_agent_for_user(user_id)
            if agent is None:
                agent = await db.create_agent_for_user(user_id)
            agent_with_ctx = await db.fetch_agent_with_context(user_id, CONTEXT_SECTION_TYPES)
            if agent_with_ctx:
                agent = agent_with_ctx

        if not agent.get("context_documents"):
            copied = await db.copy_defaults_to_agent(agent["id"])
            if copied > 0:
                if agent_id:
                    agent = await db.fetch_agent_by_id_with_context(agent_id, CONTEXT_SECTION_TYPES)
                else:
                    agent = await db.fetch_agent_with_context(user_id, CONTEXT_SECTION_TYPES)

        context_docs = agent.get("context_documents", [])

        registration_prompt = get_registration_system_prompt(identity)

        # Include channel context so the agent knows how to reach this user.
        if channel and identity.external_id:
            tool_name = f"send_{channel}_message"
            registration_prompt += (
                f"\n\n[Channel context: This conversation is happening over {channel}. "
                f"The user's {channel} ID is {identity.external_id}. "
                f"To send them a message use `{tool_name}` with chat_id={identity.external_id}.]"
            )

        system_prompt = await build_system_prompt(
            context_docs, brain_context=None, user_id=user_id,
            agent_system_prompt=registration_prompt,
            bootstrap_tools=agent.get("bootstrap_tools", "") if isinstance(agent, dict) else "",
        )

        history = await build_openai_history_from_session(
            db, user_id, session_id, exclude_interaction_ids=None,
        )

        reply = await run_agent_loop_buffered(
            user_id=user_id,
            session_id=session_id,
            user_message=message_text,
            system_prompt=system_prompt,
            agent_id=agent.get("id", ""),
            history=history,
            max_turns=5,
            channel=channel,
        )

        return reply
    except Exception as e:
        logger.error("Registration agent error for %s: %s", user_id, e, exc_info=True)
        return "Sorry, something went wrong. Please try again."


async def _run_agent_loop(
    plugin, identity: ChannelIdentity, user_id: str, message_text: str, channel: str,
    agent_id: str | None = None,
) -> str:
    """Run the normal agent loop for a registered user."""
    try:
        db = get_db()
        session_id = user_id

        await db.insert_interaction(
            user_id, session_id, role="user", content=message_text,
            channel=channel,
            metadata=json.dumps({"source": f"{channel}/message"}),
            sender_id=user_id,
            receiver_id=agent_id,
        )

        # If we know which agent owns this channel, load it directly.
        # Otherwise fall back to looking up (or creating) an agent for this user.
        if agent_id:
            agent = await db.get_agent_by_id(agent_id)
            agent_with_ctx = await db.fetch_agent_by_id_with_context(agent_id, CONTEXT_SECTION_TYPES)
            if agent_with_ctx:
                agent = agent_with_ctx
        else:
            agent = await db.get_agent_for_user(user_id)
            if agent is None:
                agent = await db.create_agent_for_user(user_id)
            agent_with_ctx = await db.fetch_agent_with_context(user_id, CONTEXT_SECTION_TYPES)
            if agent_with_ctx:
                agent = agent_with_ctx

        if not agent.get("context_documents"):
            copied = await db.copy_defaults_to_agent(agent["id"])
            if copied > 0:
                if agent_id:
                    agent = await db.fetch_agent_by_id_with_context(agent_id, CONTEXT_SECTION_TYPES)
                else:
                    agent = await db.fetch_agent_with_context(user_id, CONTEXT_SECTION_TYPES)

        context_docs = agent.get("context_documents", [])

        brain_results = await db.memory_search(user_id, message_text, limit=5)
        brain_context = None
        if brain_results:
            lines = []
            for r in brain_results:
                slug = r.get("slug", "?")
                title = r.get("title", slug)
                ct = r.get("compiled_truth", "")[:300]
                rank = r.get("rank", 0)
                lines.append(f"[{slug}] {title} (score: {rank:.2f})")
                if ct:
                    lines.append(ct)
                lines.append("")
            brain_context = "\n".join(lines)

        agent_system_prompt = agent.get("system_prompt", "")
        if identity.user_tier == "anonymous":
            agent_system_prompt += "\n\n" + get_anonymous_limit_prompt()

        # Tell the agent which channel it's on and how to reach the user proactively.
        # This lets it call send_telegram_message (or equivalent) with the correct ID.
        if channel and identity.external_id:
            tool_name = f"send_{channel}_message"
            agent_system_prompt += (
                f"\n\n[Channel context: This conversation is happening over {channel}. "
                f"The user's {channel} ID is {identity.external_id}. "
                f"To proactively send them a message use the `{tool_name}` tool "
                f"with chat_id={identity.external_id}.]"
            )

        system_prompt = await build_system_prompt(
            context_docs, brain_context, user_id,
            agent_system_prompt=agent_system_prompt,
            bootstrap_tools=agent.get("bootstrap_tools", "") if isinstance(agent, dict) else "",
        )

        history = await build_openai_history_from_session(
            db, user_id, session_id, exclude_interaction_ids=None,
        )

        reply = await run_agent_loop_buffered(
            user_id=user_id,
            session_id=session_id,
            user_message=message_text,
            system_prompt=system_prompt,
            agent_id=agent.get("id", ""),
            history=history,
            max_turns=agent.get("max_turn_count", 10),
            channel=channel,
        )

        return reply
    except Exception as e:
        logger.error("Agent loop error for %s: %s", user_id, e, exc_info=True)
        return "Sorry, I encountered an error. Please try again."

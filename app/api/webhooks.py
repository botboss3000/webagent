"""
Webhook router for inbound messages from communication channels.

Delegates to plugins for parsing, then routes to auth or agent loop.
"""

import json
import logging

from fastapi import APIRouter, Request, Response

from app.communications.auth import (
    get_or_create_identity,
    get_registration_system_prompt,
    get_anonymous_limit_prompt,
    upgrade_to_verified,
    verify_code,
    start_verification,
    ChannelIdentity,
)
from app.communications.manager import get_plugin_manager
from app.db import get_db
from app.agent.loop import run_agent_loop_buffered
from app.agent.prompts import build_system_prompt
from app.agent.session_history import build_openai_history_from_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


@router.post("/{plugin_name}")
async def webhook_handler(plugin_name: str, request: Request):
    """
    Generic webhook handler. Routes to the right plugin,
    checks auth, then runs the agent loop.
    """
    pm = get_plugin_manager()
    plugin = pm.get_plugin(plugin_name)

    if not plugin:
        logger.warning("Webhook for unknown plugin: %s", plugin_name)
        return Response(content='{"error":"unknown plugin"}', status_code=404)

    if not plugin.enabled:
        logger.warning("Webhook for disabled plugin: %s", plugin_name)
        return Response(content='{"error":"plugin disabled"}', status_code=503)

    # 1. Verify request authenticity
    if not await plugin.verify_request(request):
        return Response(content='{"error":"invalid request"}', status_code=403)

    # 2. Extract identity
    external_id = plugin.extract_external_id(request)
    if not external_id:
        return Response(content='{"ok":true}', status_code=200)  # non-message update

    message_text = plugin.extract_text(request)
    if not message_text:
        return Response(content='{"ok":true}', status_code=200)  # empty message

    # 3. Check auth — get or create identity
    identity = await get_or_create_identity(plugin.name, external_id)
    user_id = identity.user_id

    # 4. Route based on user tier
    if identity.user_tier == "anonymous":
        # Check if this looks like a verification code reply
        if message_text.strip().isdigit() and len(message_text.strip()) == 6:
            # Might be a verification code
            is_valid = await verify_code(plugin.name, external_id, message_text.strip())
            if is_valid:
                await upgrade_to_verified(identity)
                reply = "✅ You're verified! You now have full access. How can I help you?"
                await plugin.send_message(external_id, reply)
                return Response(content='{"ok":true}', status_code=200)

        # First contact or anonymous → run agent with registration prompt
        reply = await _run_registration_agent(
            plugin, identity, user_id, message_text
        )
        await plugin.send_message(external_id, reply)
        return Response(content='{"ok":true}', status_code=200)

    # 5. Registered user → run normal agent loop
    reply = await _run_agent_loop(
        plugin, identity, user_id, message_text
    )
    await plugin.send_message(external_id, reply)
    return Response(content='{"ok":true}', status_code=200)


async def _run_registration_agent(
    plugin, identity: ChannelIdentity, user_id: str, message_text: str,
) -> str:
    """
    Run the agent loop with a registration-focused system prompt.
    The agent has tools to register the user.
    """
    try:
        db = get_db()
        session_id = user_id  # one session per identity

        # Save the incoming message
        await db.insert_interaction(
            user_id, session_id, role="user", content=message_text,
            metadata=json.dumps({"source": f"webhook/{plugin.name}"}),
        )

        # Build context docs (same as normal chat)
        context_docs = await db.fetch_context_documents(
            user_id,
            ["agent", "user", "skills", "tools", "tasks", "memory", "project", "jobs"],
        )
        if not context_docs:
            copied = await db.copy_defaults_to_user(user_id)
            if copied > 0:
                context_docs = await db.fetch_context_documents(
                    user_id,
                    ["agent", "user", "skills", "tools", "tasks", "memory", "project", "jobs"],
                )

        # Load agent config
        agent = await db.get_agent_for_user(user_id)
        if agent is None:
            agent = await db.create_agent_for_user(user_id)

        # Build registration system prompt
        registration_prompt = get_registration_system_prompt(identity)
        system_prompt = await build_system_prompt(
            context_docs, brain_context=None, user_id=user_id,
            agent_system_prompt=registration_prompt,
        )

        # Build history (excluding current message which we already saved)
        history = await build_openai_history_from_session(
            db, user_id, session_id, exclude_interaction_ids=None,
        )

        reply = await run_agent_loop_buffered(
            user_id=user_id,
            session_id=session_id,
            user_message=message_text,
            system_prompt=system_prompt,
            history=history,
            max_turns=5,  # registration is quick
        )

        return reply
    except Exception as e:
        logger.error("Registration agent error: %s", e, exc_info=True)
        return "⚠️ Sorry, something went wrong. Please try again."


async def _run_agent_loop(
    plugin, identity: ChannelIdentity, user_id: str, message_text: str,
) -> str:
    """
    Run the normal agent loop for a registered user.
    """
    try:
        db = get_db()
        session_id = user_id

        # Save the incoming message
        await db.insert_interaction(
            user_id, session_id, role="user", content=message_text,
            metadata=json.dumps({"source": f"webhook/{plugin.name}"}),
        )

        # Build context
        context_docs = await db.fetch_context_documents(
            user_id,
            ["agent", "user", "skills", "tools", "tasks", "memory", "project", "jobs"],
        )
        if not context_docs:
            copied = await db.copy_defaults_to_user(user_id)
            if copied > 0:
                context_docs = await db.fetch_context_documents(
                    user_id,
                    ["agent", "user", "skills", "tools", "tasks", "memory", "project", "jobs"],
                )

        # Brain search
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

        # Agent config
        agent = await db.get_agent_for_user(user_id)
        if agent is None:
            agent = await db.create_agent_for_user(user_id)

        row = await db.get_agent_by_id(agent["id"])
        if row:
            agent = row

        # Build system prompt
        # Append anonymous limit if still anonymous
        agent_system_prompt = agent.get("system_prompt", "")
        if identity.user_tier == "anonymous":
            agent_system_prompt += "\n\n" + get_anonymous_limit_prompt()

        system_prompt = await build_system_prompt(
            context_docs, brain_context, user_id,
            agent_system_prompt=agent_system_prompt,
        )

        # History
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
        )

        return reply
    except Exception as e:
        logger.error("Agent loop error for %s: %s", user_id, e, exc_info=True)
        return "⚠️ Sorry, I encountered an error. Please try again."

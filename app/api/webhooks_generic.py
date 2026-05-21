"""
Generic inbound webhook endpoint.

External services POST to:
  POST /api/v1/webhooks/generic/{webhook_id}

The webhook_id maps to a registration in the database.
The payload is forwarded to the agent loop as a user message,
and the agent's response is returned as the HTTP response.
"""

import json
import logging
import time

from fastapi import APIRouter, Request, Response

from app.db import get_db
from app.agent.loop import run_agent_loop_buffered
from app.agent.prompts import build_system_prompt, CONTEXT_SECTION_TYPES
from app.agent.session_history import build_openai_history_from_session

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


router = APIRouter(prefix="/api/v1/webhooks/generic", tags=["webhooks"])


@router.post("/{webhook_id}")
async def generic_webhook_handler(webhook_id: str, request: Request):
    """
    Receive an inbound webhook from any external service.

    Looks up the webhook registration, parses the payload,
    creates an interaction, runs the agent loop with the
    registration's instructions, and returns the agent's response.
    """
    db = get_db()

    # 1. Look up registration
    registration = await db.get_webhook(webhook_id)
    if not registration:
        logger.warning("Webhook %s not found", webhook_id)
        return Response(content='{"error":"webhook not found"}', status_code=404)

    if not registration.get("active", True):
        logger.warning("Webhook %s is disabled", webhook_id)
        return Response(content='{"error":"webhook disabled"}', status_code=410)

    user_id = registration["user_id"]
    instructions = registration.get("instructions", "")

    # 2. Read request
    method = request.method
    in_headers = dict(request.headers)
    try:
        body_bytes = await request.body()
        body_str = body_bytes.decode("utf-8", errors="replace")
    except Exception:
        body_str = ""

    # 3. Build the user message from the webhook data
    #    Format: concise summary + payload for the agent
    content_type = in_headers.get("content-type", "")
    if content_type.startswith("application/json") and body_str:
        try:
            parsed = json.loads(body_str)
            user_message = f"[WEBHOOK] {registration['name']}\n\nPayload:\n```json\n{json.dumps(parsed, indent=2)[:8000]}\n```"
        except json.JSONDecodeError:
            user_message = f"[WEBHOOK] {registration['name']}\n\nRaw body:\n{body_str[:8000]}"
    else:
        user_message = f"[WEBHOOK] {registration['name']}\n\nRaw body:\n{body_str[:8000]}"

    start = time.time()

    try:
        # 4. Ensure session exists, then save incoming interaction
        session_id = user_id  # one session per user for webhooks
        try:
            await db.assert_session_owned(user_id, session_id)
        except PermissionError:
            # Auto-create the session via raw client
            now = _now_iso()
            client = db.get_raw_client()
            client.table("sessions").insert({
                "id": session_id,
                "user_id": user_id,
                "title": "Webhook Session",
                "created_at": now,
                "updated_at": now,
            }).execute()
        await db.insert_interaction(
            user_id, session_id, role="user", content=user_message,
            channel="webhook",
            metadata=json.dumps({
                "source": f"webhook/generic/{webhook_id}",
                "webhook_name": registration["name"],
                "method": method,
            }),
        )

        # 5. Get or create agent
        agent = await db.get_agent_for_user(user_id)
        if agent is None:
            agent = await db.create_agent_for_user(user_id)

        # 6. Fetch agent + context docs in one query
        agent_with_ctx = await db.fetch_agent_with_context(user_id, CONTEXT_SECTION_TYPES)
        if agent_with_ctx:
            agent = agent_with_ctx

        if not agent.get("context_documents"):
            copied = await db.copy_defaults_to_agent(agent["id"])
            if copied > 0:
                agent = await db.fetch_agent_with_context(user_id, CONTEXT_SECTION_TYPES)

        context_docs = agent.get("context_documents", [])

        # 7. Build system prompt — inject webhook instructions as overlay
        if instructions:
            webhook_block = (
                "## [WEBHOOK INSTRUCTIONS]\n"
                "This message arrived via webhook. Follow these instructions:\n"
                f"{instructions}\n"
            )
            context_docs = [{"id": "_webhook_overlay", "content": webhook_block}] + context_docs

        system_prompt = await build_system_prompt(
            context_docs, brain_context=None, user_id=user_id,
        )

        # 8. History (exclude irrelevant prior webhook turns — keep last 20)
        history = await build_openai_history_from_session(
            db, user_id, session_id, exclude_interaction_ids=None,
        )
        # Trim history to last 20 to keep context manageable
        if len(history) > 20:
            history = history[-20:]

        # 9. Run agent loop
        reply = await run_agent_loop_buffered(
            user_id=user_id,
            session_id=session_id,
            user_message=user_message,
            system_prompt=system_prompt,
            history=history,
            max_turns=agent.get("max_turn_count", 10),
            channel="webhook",
        )

        duration_ms = int((time.time() - start) * 1000)

        # 10. Log the event
        try:
            await db.log_webhook_event(
                webhook_id=webhook_id,
                method=method,
                headers=json.dumps({k: v for k, v in in_headers.items()
                                   if k.lower() not in ("authorization", "cookie", "x-api-key")}),
                payload=body_str[:5000],
                response_status=200,
                response_body=reply[:5000],
                duration_ms=duration_ms,
            )
        except Exception as e:
            logger.warning("Failed to log webhook event: %s", e)

        # 11. Return response
        return Response(
            content=json.dumps({
                "status": "ok",
                "reply": reply,
                "webhook_id": webhook_id,
                "duration_ms": duration_ms,
            }),
            media_type="application/json",
            status_code=200,
        )

    except Exception as e:
        logger.error("Webhook handler error for %s: %s", webhook_id, e, exc_info=True)
        return Response(
            content=json.dumps({"status": "error", "message": str(e)}),
            media_type="application/json",
            status_code=500,
        )

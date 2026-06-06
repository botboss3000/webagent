"""Automation tools — event-subscription handlers for the ``automation`` ability.

Moved here from app/tools/loader.py as part of making the ``automation`` ability
a self-contained drop-in (plugins/abilities/automation.py wraps this factory).
These tools let a chat agent create the same kind of real-time trigger the
Automation tab creates: a row in ``agent_event_subscriptions`` plus a
provider-side watch (Gmail Pub/Sub, Graph subscription, etc.).

The factory ``build_automation_tools`` returns {tool_name: handler} with each
handler closed over the caller's user_id / agent_id / session_id.
``AUTOMATION_TOOL_SCHEMAS`` holds the per-tool input schemas (single source of
truth). Nothing here is marked destructive at the ToolInfo level, matching the
prior loader behavior (writes are guarded by ownership checks inside).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


AUTOMATION_TOOL_SCHEMAS = {
    "list_event_sources": {"type": "object", "properties": {}, "required": []},
    "list_delivery_channels": {"type": "object", "properties": {}, "required": []},
    "event_subscribe": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Event source name. Call list_event_sources to see what's enabled (e.g. 'gmail', 'slack', 'outlook_mail', 'google_drive', 'telegram').",
            },
            "event_type": {
                "type": "string",
                "description": "Event type on that source (e.g. 'message_received', 'file_added', 'mention'). See list_event_sources.",
            },
            "prompt": {
                "type": "string",
                "description": "Instructions the agent will run when the event fires. Reference the event payload naturally — e.g. 'Summarize the new email and send it to me on Telegram.'",
            },
            "filter": {
                "type": "object",
                "description": "Source-specific filter. For Gmail: {'query': 'from:airlines.com'}. For Drive: {'parent_id': '<folder-id>'}. Empty = match all.",
                "default": {},
            },
            "task_label": {
                "type": "string",
                "description": "Short human label for this trigger (shown in the Automation tab). Optional.",
                "default": "",
            },
            "trigger_natural": {
                "type": "string",
                "description": "The original English the user wrote (for the Automation tab to display). Optional.",
                "default": "",
            },
            "channel": {
                "type": "string",
                "description": (
                    "Where to deliver when the event fires. **DEFAULT: omit this argument** — "
                    "the tool will set channel='webchat' targeted at the user's current "
                    "session, so the agent's reply lands directly in the chat they're "
                    "sitting in. Only pass a non-webchat value (telegram, slack, sms, "
                    "discord, etc.) if you have FIRST called `list_delivery_channels` "
                    "AND that exact name appears in the returned `channels` array. "
                    "Never invent or assume a channel is available."
                ),
            },
            "channel_recipient": {
                "type": "string",
                "description": (
                    "Channel-specific recipient. For webchat (default), this is the "
                    "session_id where the event reply should land — the tool auto-fills "
                    "it from the current session, so usually omit. For other channels, "
                    "this is the chat_id / phone / address."
                ),
            },
            "silent": {
                "type": "boolean",
                "description": "If true, the agent processes the event without notifying the user.",
                "default": False,
            },
        },
        "required": ["source", "event_type", "prompt"],
    },
    "list_event_subscriptions": {"type": "object", "properties": {}, "required": []},
    "event_unsubscribe": {
        "type": "object",
        "properties": {
            "subscription_id": {"type": "string", "description": "id of the row to remove (from list_event_subscriptions)"},
        },
        "required": ["subscription_id"],
    },
}

AUTOMATION_DESTRUCTIVE: set = set()


def build_automation_tools(user_id: str, agent_id: str, session_id: str) -> dict:
    """Return {tool_name: handler} for the event-subscription tools, each closed
    over the caller's user_id / agent_id / session_id."""

    async def list_event_sources():
        """List the event sources discovered on this server and which event types each supports."""
        try:
            from app.events import get_manager
            mgr = get_manager()
            return json.dumps({
                "status": "ok",
                "sources": mgr.manifest(),
            })
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})

    async def list_delivery_channels():
        """List the delivery channels available for this agent's automations (only these may be used)."""
        try:
            from app.events.channels import list_available_channels
            from app.db import get_db as _gd
            chans = await list_available_channels(
                _gd(), user_id=user_id, agent_id=agent_id or "",
                current_session_id=session_id or None,
            )
            return json.dumps({
                "status": "ok",
                "channels": chans,
                "note": (
                    "ONLY suggest channels in this list. Channels NOT listed (Telegram, "
                    "SMS, Slack, Discord, email-out, etc. when absent) require admin "
                    "comms plugin enablement + agent connection setup + per-user recipient "
                    "config — none of which exist yet for those channels. Do not invent "
                    "channels or claim a channel is 'available' that isn't here."
                ),
            })
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})

    async def event_subscribe(
        source: str,
        event_type: str,
        prompt: str,
        filter: Optional[Dict[str, Any]] = None,
        task_label: str = "",
        trigger_natural: str = "",
        channel: Optional[str] = None,
        channel_recipient: Optional[str] = None,
        silent: bool = False,
    ):
        """Subscribe this agent to a real-time event: when source/event_type fires, run the prompt."""
        if not agent_id:
            return json.dumps({
                "status": "error",
                "message": "event_subscribe requires an agent context (no agent_id on this session)",
            })
        from app.db import get_db
        from app.events import get_manager
        from app.automation.parser import ParsedEventSubscription
        from app.automation.sync import _event_sub_hash, _register_event_sub

        # Pre-flight: refuse to insert a row that the provider can't honor.
        # Avoids stale error rows and gives the agent a clear, actionable hint.
        mgr = get_manager()
        src_obj = mgr.get(source)
        if src_obj is None:
            return json.dumps({
                "status": "error",
                "code": "unknown_source",
                "message": (
                    f"Event source '{source}' is not registered. "
                    "Call list_event_sources to see what's available."
                ),
            })
        if not src_obj.enabled:
            return json.dumps({
                "status": "error",
                "code": "source_not_enabled",
                "source": source,
                "message": (
                    f"Event source '{source}' is discovered but not enabled on this server "
                    "(provider-side config missing — e.g. for Gmail, EVENTS_GMAIL_PUBSUB_TOPIC "
                    "is not set, so push notifications can't be wired up). "
                    "Tell the user real-time push isn't available for this source; do not "
                    "create a subscription. Other options: poll-style sources (see "
                    "list_event_sources for which sources have supports_poll=true), or the "
                    "user can ask the admin to configure the provider env vars."
                ),
            })
        if event_type not in (src_obj.event_types or []):
            return json.dumps({
                "status": "error",
                "code": "unknown_event_type",
                "source": source,
                "supported_event_types": list(src_obj.event_types or []),
                "message": (
                    f"Event type '{event_type}' is not supported by source '{source}'. "
                    f"Supported: {', '.join(src_obj.event_types or []) or '(none)'}."
                ),
            })

        # ── Scope pre-flight ─────────────────────────────────────
        # Avoid inserting a doomed subscription when the user's OAuth
        # token lacks the scope the provider requires (most common
        # cause: user unticked a sensitive-scope box on consent).
        required_provider = getattr(src_obj, "required_provider", None)
        required_groups = src_obj.required_scopes(event_type, filter or {}) if hasattr(src_obj, "required_scopes") else []
        if required_provider and required_groups:
            from app.integrations.oauth_helper import oauth_label as _oauth_label
            from app.db import get_db as _get_db
            _db = _get_db()
            elem = await _db.auth_element_get(user_id, required_provider, _oauth_label(agent_id))
            if not elem:
                return json.dumps({
                    "status": "error",
                    "code": "provider_not_connected",
                    "provider": required_provider,
                    "message": (
                        f"Source '{source}' needs an active '{required_provider}' OAuth "
                        "connection on this agent. Ask the user to connect via the chat's "
                        "OAuth link before subscribing."
                    ),
                })
            try:
                cfg = json.loads(elem.get("config") or "{}") if isinstance(elem.get("config"), str) else (elem.get("config") or {})
            except Exception:
                cfg = {}
            granted = set(cfg.get("scopes") or [])
            missing = [g for g in required_groups if not any(s in granted for s in g)]
            if missing:
                return json.dumps({
                    "status": "error",
                    "code": "scope_missing",
                    "provider": required_provider,
                    "source": source,
                    "mode": "push" if src_obj.supports_push else "poll",
                    "missing_groups": missing,
                    "granted_scopes": sorted(granted),
                    "message": (
                        f"The user's '{required_provider}' token does not have the scope(s) "
                        f"required for source '{source}' "
                        f"({'push' if src_obj.supports_push else 'poll'} mode). "
                        "Tell the user to RECONNECT the integration and TICK every checkbox "
                        "for this provider on the Google/Microsoft consent screen — they "
                        "almost certainly unchecked one previously. Do not insert this "
                        "subscription. "
                        f"Need at least one scope from each group: {missing}. "
                        f"Currently granted: {sorted(granted)}."
                    ),
                })

        # Default delivery: the chat session the user is currently in.
        # `channel="webchat"` + `channel_recipient=<session_id>` tells
        # the event executor to post the agent's reply BACK into the
        # user's current chat instead of spawning an evt-* session
        # that lives in the sidebar but never lights up the chat
        # they're sitting in. Caller can override either field to
        # subscribe in a different session, fresh evt-* session, or
        # a non-webchat channel.
        effective_channel = channel
        effective_recipient = channel_recipient
        if effective_channel is None:
            effective_channel = "webchat"
        if effective_channel == "webchat" and not effective_recipient and session_id:
            effective_recipient = session_id

        parsed = ParsedEventSubscription(
            task_label=task_label or f"{source}/{event_type}",
            prompt=prompt,
            source=source,
            event_type=event_type,
            filter_dict=filter or {},
            trigger_natural=trigger_natural,
            channel=effective_channel,
            channel_recipient=effective_recipient,
            silent=silent,
        )
        db = get_db()
        try:
            row = await db.upsert_event_subscription(
                agent_id=agent_id,
                owner_user_id=user_id,
                source_hash=_event_sub_hash(parsed),
                source=parsed.source,
                event_type=parsed.event_type,
                filter_dict=parsed.filter_dict,
                task_label=parsed.task_label,
                prompt=parsed.prompt,
                trigger_natural=parsed.trigger_natural,
                channel=parsed.channel,
                channel_recipient=parsed.channel_recipient,
                silent=parsed.silent,
                enabled=True,
            )
            await _register_event_sub(db, row, parsed)
            row_after = (await db.list_event_subscriptions(
                agent_id=agent_id, owner_user_id=user_id,
            ))
            fresh = next((r for r in row_after if r.get("id") == row.get("id")), row)
            # Notify the user's open tabs so the Automation tab re-fetches
            # without a manual refresh.
            try:
                from app.api.chat import _emit_to_user_listeners
                await _emit_to_user_listeners(user_id, {
                    "type": "automation_updated",
                    "agent_id": agent_id,
                    "action": "subscribed",
                    "kind": "event_subscription",
                    "subscription_id": fresh.get("id"),
                })
            except Exception:
                pass
            return json.dumps({
                "status": "ok",
                "subscription": fresh,
                "message": (
                    f"Subscribed: when {source}/{event_type} fires, run agent."
                    + (f" Provider-side: {fresh.get('last_status') or 'pending'}." if fresh.get('last_status') else "")
                    + (f" Error: {fresh.get('last_error')}" if fresh.get('last_error') else "")
                ),
            })
        except Exception as e:
            logger.exception("event_subscribe failed")
            return json.dumps({"status": "error", "message": str(e)})

    async def list_event_subscriptions():
        """List this agent's existing event subscriptions."""
        from app.db import get_db
        db = get_db()
        rows = await db.list_event_subscriptions(
            agent_id=agent_id or None,
            owner_user_id=user_id,
        )
        return json.dumps({"status": "ok", "subscriptions": rows, "count": len(rows)})

    async def event_unsubscribe(subscription_id: str):
        """Remove one of this agent's event subscriptions by id."""
        from app.db import get_db
        from app.automation.sync import _unregister_event_sub
        db = get_db()
        rows = await db.list_event_subscriptions(
            agent_id=agent_id or None,
            owner_user_id=user_id,
        )
        target = next((r for r in rows if r.get("id") == subscription_id), None)
        if not target:
            return json.dumps({"status": "error", "message": f"Subscription {subscription_id} not found or not owned by user"})
        await _unregister_event_sub(target)
        ok = await db.delete_event_subscription(subscription_id)
        if ok:
            try:
                from app.api.chat import _emit_to_user_listeners
                await _emit_to_user_listeners(user_id, {
                    "type": "automation_updated",
                    "agent_id": agent_id,
                    "action": "unsubscribed",
                    "kind": "event_subscription",
                    "subscription_id": subscription_id,
                })
            except Exception:
                pass
        return json.dumps({
            "status": "ok" if ok else "error",
            "message": f"Subscription {subscription_id} {'removed' if ok else 'delete failed'}",
        })

    return {
        "list_event_sources": list_event_sources,
        "list_delivery_channels": list_delivery_channels,
        "event_subscribe": event_subscribe,
        "list_event_subscriptions": list_event_subscriptions,
        "event_unsubscribe": event_unsubscribe,
    }

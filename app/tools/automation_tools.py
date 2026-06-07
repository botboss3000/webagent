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

# Shared bits of the delivery/run-mode schema, reused by schedule_task + event_subscribe.
_DELIVERY_PROP = {
    "type": "array",
    "description": (
        "Where to send the result. A list of targets — each either a channel "
        "name string, or an object {mode,...}. Modes: "
        "{mode:'here'} (reply in the current chat), {mode:'new_session'} (a fresh "
        "sidebar session), {mode:'headless'} (do the work, surface nothing), "
        "{mode:'channel',channel:'telegram',recipient?:'...'} (a comms channel — "
        "call list_delivery_channels first), {mode:'webhook',url:'https://...'}, "
        "{mode:'file',filename:'out.md'}. Omit to default to the current chat. "
        "Multiple targets fan out (e.g. act silently AND ping me on Telegram)."
    ),
    "items": {"type": "object"},
    "default": [],
}
_RUN_MODE_PROP = {
    "type": "string",
    "enum": ["inline", "fresh_clone", "dedicated_clone", "headless"],
    "description": (
        "Who runs it. 'inline' = this agent (default). 'fresh_clone' = a new "
        "throwaway clone each run (reaped after). 'dedicated_clone' = one long-lived "
        "clone owns it. Clone modes need the Agent Orchestration ability enabled — "
        "otherwise they fall back to inline."
    ),
    "default": "inline",
}
_GUARDRAIL_PROPS = {
    "max_per_day": {"type": "integer", "description": "Guardrail: max fires per day (omit = unlimited)."},
    "disable_after_failures": {"type": "integer", "description": "Auto-disable after this many consecutive failures (omit = never)."},
    "expires_at": {"type": "string", "description": "ISO timestamp after which the automation stops and disables (omit = never)."},
    "retry_max": {"type": "integer", "description": "Retry a failed run up to this many times (with backoff) before giving up the occurrence.", "default": 0},
    "retry_backoff_seconds": {"type": "integer", "description": "Base seconds between retries (grows per attempt).", "default": 0},
    "clone_abilities": {"type": "array", "items": {"type": "string"}, "description": "Abilities to grant a clone runner (clamped to what this agent already has).", "default": []},
}

AUTOMATION_TOOL_SCHEMAS.update({
    "schedule_task": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "Instructions to run when the task fires."},
            "task_label": {"type": "string", "description": "Short human label (shown in the Automation tab).", "default": ""},
            "schedule_cron": {"type": "string", "description": "Cron expression for a RECURRING task, e.g. '0 9 * * 1-5' (weekdays 9am). Leave empty for a one-shot — then set in_minutes or at.", "default": ""},
            "in_minutes": {"type": "number", "description": "One-shot: fire this many minutes from now. Ignored if schedule_cron is set.", "default": 0},
            "at": {"type": "string", "description": "One-shot: ISO timestamp to fire at (e.g. '2026-06-08T15:00:00'). Ignored if schedule_cron is set.", "default": ""},
            "timezone": {"type": "string", "description": "IANA timezone for the cron expression.", "default": "UTC"},
            "delivery": _DELIVERY_PROP,
            "run_mode": _RUN_MODE_PROP,
            **_GUARDRAIL_PROPS,
        },
        "required": ["prompt"],
    },
    "remind_me": {
        "type": "object",
        "properties": {
            "note": {"type": "string", "description": "What to remind the user about."},
            "in_minutes": {"type": "number", "description": "Fire this many minutes from now.", "default": 0},
            "at": {"type": "string", "description": "Or an ISO timestamp to fire at.", "default": ""},
        },
        "required": ["note"],
    },
    "list_automations": {"type": "object", "properties": {}, "required": []},
    "update_automation": {
        "type": "object",
        "properties": {
            "automation_id": {"type": "string", "description": "id from list_automations."},
            "prompt": {"type": "string"},
            "task_label": {"type": "string"},
            "schedule_cron": {"type": "string"},
            "timezone": {"type": "string"},
            "enabled": {"type": "boolean"},
            "delivery": _DELIVERY_PROP,
            "run_mode": _RUN_MODE_PROP,
            **_GUARDRAIL_PROPS,
        },
        "required": ["automation_id"],
    },
    "pause_automation": {"type": "object", "properties": {"automation_id": {"type": "string"}}, "required": ["automation_id"]},
    "resume_automation": {"type": "object", "properties": {"automation_id": {"type": "string"}}, "required": ["automation_id"]},
    "cancel_automation": {"type": "object", "properties": {"automation_id": {"type": "string"}}, "required": ["automation_id"]},
    "run_automation_now": {"type": "object", "properties": {"automation_id": {"type": "string"}}, "required": ["automation_id"]},
    "remember_automation_state": {
        "type": "object",
        "properties": {
            "state": {"description": "The state to persist for THIS automation (an object or string). Replaces the prior memory."},
        },
        "required": ["state"],
    },
})

# Add the feature-rich knobs to event_subscribe too.
AUTOMATION_TOOL_SCHEMAS["event_subscribe"]["properties"].update({
    "delivery": _DELIVERY_PROP,
    "run_mode": _RUN_MODE_PROP,
    **_GUARDRAIL_PROPS,
})

# Mutating tools confirm-gate at the ToolInfo level.
AUTOMATION_DESTRUCTIVE.update({"cancel_automation"})


def _build_delivery_spec(*, delivery=None, channel=None, recipient=None,
                         silent: bool = False, default_session_id=None) -> dict:
    """Build a unified delivery spec from a `delivery` target list, or fall back
    to simple channel/recipient/silent inputs (legacy-friendly)."""
    targets = []
    for t in (delivery or []):
        if isinstance(t, str) and t.strip():
            targets.append({"mode": "channel", "channel": t.strip()})
        elif isinstance(t, dict) and t.get("mode"):
            targets.append(t)
    if targets:
        return {"silent": bool(silent), "targets": targets}
    from app.automation.delivery import spec_from_legacy
    return spec_from_legacy(channel, recipient or default_session_id, silent)


def _validate_run_mode(run_mode, enabled_providers):
    rm = (run_mode or "inline").strip()
    if rm in ("fresh_clone", "dedicated_clone") and "agent_orchestration" not in (enabled_providers or set()):
        return "inline", ("Clone run modes need the Agent Orchestration ability enabled on "
                          "this agent — used 'inline' instead.")
    return rm, None


def _parse_at(at: str):
    """Parse an ISO timestamp into a UTC ISO string (naive = assumed UTC)."""
    from datetime import datetime, timezone as _tz
    try:
        dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return dt.astimezone(_tz.utc).isoformat()
    except Exception:
        return None


def build_automation_tools(user_id: str, agent_id: str, session_id: str,
                           enabled_providers=None) -> dict:
    """Return {tool_name: handler} for the automation tools, each closed over the
    caller's user_id / agent_id / session_id. ``enabled_providers`` gates clone
    run modes (which need the agent_orchestration ability)."""

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
        delivery: Optional[list] = None,
        run_mode: str = "inline",
        clone_abilities: Optional[list] = None,
        max_per_day: Optional[int] = None,
        disable_after_failures: Optional[int] = None,
        expires_at: Optional[str] = None,
        retry_max: int = 0,
        retry_backoff_seconds: int = 0,
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
        # Run mode (clone gating) + unified delivery spec.
        rm, rm_warn = _validate_run_mode(run_mode, enabled_providers)
        spec = _build_delivery_spec(
            delivery=delivery, channel=effective_channel,
            recipient=effective_recipient, silent=silent,
            default_session_id=session_id)

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
                delivery_json=json.dumps(spec),
                run_mode=rm,
                clone_abilities=json.dumps(clone_abilities or []),
                max_per_day=max_per_day,
                disable_after_failures=disable_after_failures,
                expires_at=expires_at or None,
                retry_max=int(retry_max or 0),
                retry_backoff_seconds=int(retry_backoff_seconds or 0),
                origin="tool",
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
                "warning": rm_warn,
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

    # ── Scheduled tasks / one-shot timers ────────────────────────────────

    async def _emit_automation_updated(action: str, automation_id: str):
        try:
            from app.api.chat import _emit_to_user_listeners
            await _emit_to_user_listeners(user_id, {
                "type": "automation_updated", "agent_id": agent_id,
                "action": action, "kind": "automation",
                "automation_id": automation_id,
            })
        except Exception:
            pass

    async def _owned_automation(db, automation_id: str):
        row = await db.get_automation(automation_id)
        if not row or row.get("owner_user_id") != user_id:
            return None
        return row

    async def schedule_task(
        prompt: str, task_label: str = "", schedule_cron: str = "",
        in_minutes: float = 0, at: str = "", timezone: str = "UTC",
        delivery: Optional[list] = None, channel: Optional[str] = None,
        channel_recipient: Optional[str] = None, silent: bool = False,
        run_mode: str = "inline", clone_abilities: Optional[list] = None,
        max_per_day: Optional[int] = None, disable_after_failures: Optional[int] = None,
        expires_at: Optional[str] = None, retry_max: int = 0,
        retry_backoff_seconds: int = 0,
    ):
        """Create a recurring (cron) or one-shot scheduled task that runs the prompt."""
        if not agent_id:
            return json.dumps({"status": "error", "message": "schedule_task requires an agent context"})
        from app.db import get_db
        from app.automation.runner import _compute_next_run
        db = get_db()

        schedule_kind = "cron"
        next_run = None
        if schedule_cron:
            next_run = _compute_next_run(schedule_cron, timezone)
            if not next_run:
                return json.dumps({"status": "error", "message": f"invalid cron expression: {schedule_cron}"})
        else:
            schedule_kind = "once"
            if in_minutes and float(in_minutes) > 0:
                from datetime import datetime, timezone as _tz, timedelta
                next_run = (datetime.now(_tz.utc) + timedelta(minutes=float(in_minutes))).isoformat()
            elif at:
                next_run = _parse_at(at)
            if not next_run:
                return json.dumps({"status": "error", "message": "provide schedule_cron, in_minutes, or at"})

        rm, rm_warn = _validate_run_mode(run_mode, enabled_providers)
        spec = _build_delivery_spec(delivery=delivery, channel=channel,
                                    recipient=channel_recipient, silent=silent,
                                    default_session_id=session_id)
        try:
            row = await db.create_automation(
                agent_id=agent_id, owner_user_id=user_id, prompt=prompt,
                task_label=task_label or ("Reminder" if schedule_kind == "once" else "Scheduled task"),
                schedule_cron=schedule_cron, schedule_kind=schedule_kind,
                timezone=timezone, next_run_at=next_run,
                delivery_json=json.dumps(spec), run_mode=rm,
                clone_abilities=json.dumps(clone_abilities or []),
                max_per_day=max_per_day, disable_after_failures=disable_after_failures,
                expires_at=expires_at or None, retry_max=int(retry_max or 0),
                retry_backoff_seconds=int(retry_backoff_seconds or 0), origin="tool",
            )
            await _emit_automation_updated("created", row.get("id"))
            return json.dumps({"status": "ok", "automation": row, "warning": rm_warn,
                               "message": f"Scheduled ({schedule_kind}); next run {next_run}."})
        except Exception as e:
            logger.exception("schedule_task failed")
            return json.dumps({"status": "error", "message": str(e)})

    async def remind_me(note: str, in_minutes: float = 0, at: str = ""):
        """Set a one-shot reminder that fires in the current chat (e.g. 'in 1 hour')."""
        return await schedule_task(
            prompt=f"Reminder for the user: {note}\n\nDeliver this reminder to them now.",
            task_label=f"Reminder: {note[:40]}", in_minutes=in_minutes, at=at,
            run_mode="inline", channel="webchat", channel_recipient=session_id,
        )

    async def list_automations():
        """List this agent's scheduled tasks / timers for the current user."""
        from app.db import get_db
        db = get_db()
        rows = await db.list_automations(agent_id=agent_id or None, owner_user_id=user_id)
        return json.dumps({"status": "ok", "automations": rows, "count": len(rows)})

    async def update_automation(automation_id: str, prompt: Optional[str] = None,
                                task_label: Optional[str] = None, schedule_cron: Optional[str] = None,
                                timezone: Optional[str] = None, enabled: Optional[bool] = None,
                                delivery: Optional[list] = None, run_mode: Optional[str] = None,
                                clone_abilities: Optional[list] = None, max_per_day: Optional[int] = None,
                                disable_after_failures: Optional[int] = None, expires_at: Optional[str] = None,
                                retry_max: Optional[int] = None, retry_backoff_seconds: Optional[int] = None):
        """Edit one of this agent's scheduled tasks."""
        from app.db import get_db
        from app.automation.runner import _compute_next_run
        db = get_db()
        row = await _owned_automation(db, automation_id)
        if not row:
            return json.dumps({"status": "error", "message": "automation not found"})
        fields: Dict[str, Any] = {}
        if prompt is not None: fields["prompt"] = prompt
        if task_label is not None: fields["task_label"] = task_label
        if timezone is not None: fields["timezone"] = timezone
        if enabled is not None: fields["enabled"] = enabled
        if schedule_cron is not None:
            fields["schedule_cron"] = schedule_cron
            fields["next_run_at"] = _compute_next_run(schedule_cron, timezone or row.get("timezone") or "UTC")
        if run_mode is not None:
            fields["run_mode"], _ = _validate_run_mode(run_mode, enabled_providers)
        if delivery is not None:
            fields["delivery_json"] = json.dumps(_build_delivery_spec(delivery=delivery, default_session_id=session_id))
        if clone_abilities is not None: fields["clone_abilities"] = json.dumps(clone_abilities)
        if max_per_day is not None: fields["max_per_day"] = max_per_day
        if disable_after_failures is not None: fields["disable_after_failures"] = disable_after_failures
        if expires_at is not None: fields["expires_at"] = expires_at or None
        if retry_max is not None: fields["retry_max"] = int(retry_max)
        if retry_backoff_seconds is not None: fields["retry_backoff_seconds"] = int(retry_backoff_seconds)
        updated = await db.update_automation(automation_id, **fields)
        await _emit_automation_updated("updated", automation_id)
        return json.dumps({"status": "ok", "automation": updated})

    async def pause_automation(automation_id: str):
        """Pause (disable) a scheduled task without deleting it."""
        from app.db import get_db
        db = get_db()
        if not await _owned_automation(db, automation_id):
            return json.dumps({"status": "error", "message": "automation not found"})
        await db.update_automation(automation_id, enabled=False)
        await _emit_automation_updated("paused", automation_id)
        return json.dumps({"status": "ok", "message": "paused"})

    async def resume_automation(automation_id: str):
        """Resume (re-enable) a paused scheduled task."""
        from app.db import get_db
        from app.automation.runner import _compute_next_run
        db = get_db()
        row = await _owned_automation(db, automation_id)
        if not row:
            return json.dumps({"status": "error", "message": "automation not found"})
        fields: Dict[str, Any] = {"enabled": True}
        if (row.get("schedule_kind") or "cron") != "once" and not row.get("next_run_at"):
            fields["next_run_at"] = _compute_next_run(row.get("schedule_cron") or "", row.get("timezone") or "UTC")
        await db.update_automation(automation_id, **fields)
        await _emit_automation_updated("resumed", automation_id)
        return json.dumps({"status": "ok", "message": "resumed"})

    async def cancel_automation(automation_id: str):
        """Delete a scheduled task (and reap its dedicated clone runner if any)."""
        from app.db import get_db
        db = get_db()
        row = await _owned_automation(db, automation_id)
        if not row:
            return json.dumps({"status": "error", "message": "automation not found"})
        runner_id = (row.get("runner_agent_id") or "").strip()
        ok = await db.delete_automation(automation_id)
        if runner_id and row.get("run_mode") == "dedicated_clone":
            try:
                await db.delete_clone_agent(runner_id)
            except Exception:
                pass
        await _emit_automation_updated("cancelled", automation_id)
        return json.dumps({"status": "ok" if ok else "error",
                           "message": "cancelled" if ok else "delete failed"})

    async def run_automation_now(automation_id: str):
        """Fire a scheduled task immediately (in addition to its schedule)."""
        from app.db import get_db
        db = get_db()
        if not await _owned_automation(db, automation_id):
            return json.dumps({"status": "error", "message": "automation not found"})
        try:
            from app.scheduler import get_scheduler
            res = await get_scheduler().run_now(automation_id)
            return json.dumps({"status": "ok", "result": res})
        except Exception as e:
            return json.dumps({"status": "error", "message": str(e)})

    async def remember_automation_state(state):
        """Persist state for the automation whose run is currently in progress."""
        from app.automation.runner import set_automation_memory, current_automation_id
        if not current_automation_id():
            return json.dumps({"status": "error", "message": "no automation run in progress"})
        ok = await set_automation_memory(state)
        return json.dumps({"status": "ok" if ok else "error"})

    return {
        "list_event_sources": list_event_sources,
        "list_delivery_channels": list_delivery_channels,
        "event_subscribe": event_subscribe,
        "list_event_subscriptions": list_event_subscriptions,
        "event_unsubscribe": event_unsubscribe,
        "schedule_task": schedule_task,
        "remind_me": remind_me,
        "list_automations": list_automations,
        "update_automation": update_automation,
        "pause_automation": pause_automation,
        "resume_automation": resume_automation,
        "cancel_automation": cancel_automation,
        "run_automation_now": run_automation_now,
        "remember_automation_state": remember_automation_state,
    }

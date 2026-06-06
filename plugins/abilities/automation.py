"""Automation ability — drop-in. See app/abilities/__init__.py for the contract."""

FEATURE = {
    "id": "automation",
    "display_name": "Automation",
    "category": "ability",
    "status": "beta",
    "summary": "scheduled tasks + event subscriptions.",
    "tools": ["list_event_sources", "list_delivery_channels", "event_subscribe"],
    "group": "basic",
    "icon": "clock",
    "color": "#e0af68",
    "description": "Scheduled tasks and event-triggered jobs that can call integrations.",
    "simple": False,
}

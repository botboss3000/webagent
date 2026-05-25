"""
sent.dm communication plugin (stub).

Multi-channel provider supporting SMS, WhatsApp, and RCS.
Credentials (api_key, from_number) live in registry under `plugins.sent_dm`.
Env-var fallbacks: SENT_DM_API_KEY, SENT_DM_FROM_NUMBER.

This is a scaffold — outbound `send_message` and inbound webhook parsing are
not yet wired to the live sent.dm API.
"""

import logging
import os

from fastapi import Request, Response

from app.communications.base import CommunicationPlugin

logger = logging.getLogger(__name__)


class SentDmPlugin(CommunicationPlugin):
    """sent.dm channel plugin — SMS / WhatsApp / RCS via one API."""

    def __init__(self, registry: dict):
        self._registry = registry

    def _cfg(self) -> dict:
        return self._registry.get("plugins", {}).get("sent_dm", {})

    def _cred(self, key: str, env_var: str) -> str:
        return self._cfg().get(key) or os.environ.get(env_var, "")

    @property
    def name(self) -> str:
        return "sent_dm"

    @property
    def enabled(self) -> bool:
        if not self._cfg().get("enabled", False):
            return False
        return bool(self._cred("api_key", "SENT_DM_API_KEY"))

    @property
    def user_id_prefix(self) -> str:
        return "sent_dm:"

    @property
    def webhook_path(self) -> str:
        return "sent_dm"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "send_sent_dm_message",
                "description": (
                    "Send a message via sent.dm. Routes to SMS, WhatsApp, or RCS "
                    "based on the `channel` argument. Args: to (E.164 phone number), "
                    "text (message body), channel (one of 'sms', 'whatsapp', 'rcs')."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to":      {"type": "string", "description": "Recipient phone number in E.164 format (e.g. +15551234567)"},
                        "text":    {"type": "string", "description": "Message body"},
                        "channel": {"type": "string", "enum": ["sms", "whatsapp", "rcs"], "description": "Delivery channel"},
                    },
                    "required": ["to", "text", "channel"],
                },
            }
        ]

    def extract_external_id(self, request: Request) -> str:
        return ""

    def extract_text(self, request: Request) -> str:
        return ""

    async def handle_webhook(self, request: Request) -> Response:
        return Response(content='{"ok":true}', status_code=200)

    async def send_message(self, recipient_id: str, text: str) -> str:
        raise NotImplementedError("sent.dm send_message is not yet implemented")


plugin_cls = SentDmPlugin

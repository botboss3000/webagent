"""
Vonage (formerly Nexmo) communication plugin (stub).

Supports SMS and WhatsApp (via Vonage Messages API).
Credentials (api_key, api_secret, application_id, from_number) live in
registry under `plugins.vonage`. Env-var fallbacks:
VONAGE_API_KEY, VONAGE_API_SECRET, VONAGE_APPLICATION_ID, VONAGE_FROM_NUMBER.
"""

import logging
import os

from fastapi import Request, Response

from app.communications.base import CommunicationPlugin

logger = logging.getLogger(__name__)


class VonagePlugin(CommunicationPlugin):
    """Vonage channel plugin — SMS / WhatsApp."""

    def __init__(self, registry: dict):
        self._registry = registry

    def _cfg(self) -> dict:
        return self._registry.get("plugins", {}).get("vonage", {})

    def _cred(self, key: str, env_var: str) -> str:
        return self._cfg().get(key) or os.environ.get(env_var, "")

    @property
    def name(self) -> str:
        return "vonage"

    @property
    def enabled(self) -> bool:
        if not self._cfg().get("enabled", False):
            return False
        return bool(self._cred("api_key", "VONAGE_API_KEY") and
                    self._cred("api_secret", "VONAGE_API_SECRET"))

    @property
    def user_id_prefix(self) -> str:
        return "vonage:"

    @property
    def webhook_path(self) -> str:
        return "vonage"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "send_vonage_message",
                "description": (
                    "Send a message via Vonage. Args: to (E.164 phone number), "
                    "text (message body), channel (one of 'sms', 'whatsapp')."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to":      {"type": "string", "description": "Recipient phone number in E.164 format"},
                        "text":    {"type": "string", "description": "Message body"},
                        "channel": {"type": "string", "enum": ["sms", "whatsapp"], "description": "Delivery channel"},
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
        raise NotImplementedError("Vonage send_message is not yet implemented")


plugin_cls = VonagePlugin

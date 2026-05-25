"""
Telnyx communication plugin (stub).

Supports SMS, WhatsApp (via Telnyx WhatsApp Business), and RCS.
Credentials (api_key, messaging_profile_id, from_number) live in registry
under `plugins.telnyx`. Env-var fallbacks:
TELNYX_API_KEY, TELNYX_MESSAGING_PROFILE_ID, TELNYX_FROM_NUMBER.
"""

import logging
import os

from fastapi import Request, Response

from app.communications.base import CommunicationPlugin

logger = logging.getLogger(__name__)


class TelnyxPlugin(CommunicationPlugin):
    """Telnyx channel plugin — SMS / WhatsApp / RCS."""

    def __init__(self, registry: dict):
        self._registry = registry

    def _cfg(self) -> dict:
        return self._registry.get("plugins", {}).get("telnyx", {})

    def _cred(self, key: str, env_var: str) -> str:
        return self._cfg().get(key) or os.environ.get(env_var, "")

    @property
    def name(self) -> str:
        return "telnyx"

    @property
    def enabled(self) -> bool:
        if not self._cfg().get("enabled", False):
            return False
        return bool(self._cred("api_key", "TELNYX_API_KEY"))

    @property
    def user_id_prefix(self) -> str:
        return "telnyx:"

    @property
    def webhook_path(self) -> str:
        return "telnyx"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "send_telnyx_message",
                "description": (
                    "Send a message via Telnyx. Args: to (E.164 phone number), "
                    "text (message body), channel (one of 'sms', 'whatsapp', 'rcs')."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to":      {"type": "string", "description": "Recipient phone number in E.164 format"},
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
        raise NotImplementedError("Telnyx send_message is not yet implemented")


plugin_cls = TelnyxPlugin

"""
Plivo communication plugin (stub).

SMS via Plivo Messages API.
Credentials (auth_id, auth_token, from_number) live in registry under
`plugins.plivo`. Env-var fallbacks:
PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN, PLIVO_FROM_NUMBER.
"""

import logging
import os

from fastapi import Request, Response

from app.communications.base import CommunicationPlugin

logger = logging.getLogger(__name__)


class PlivoPlugin(CommunicationPlugin):
    """Plivo channel plugin — SMS."""

    def __init__(self, registry: dict):
        self._registry = registry

    def _cfg(self) -> dict:
        return self._registry.get("plugins", {}).get("plivo", {})

    def _cred(self, key: str, env_var: str) -> str:
        return self._cfg().get(key) or os.environ.get(env_var, "")

    @property
    def name(self) -> str:
        return "plivo"

    @property
    def enabled(self) -> bool:
        if not self._cfg().get("enabled", False):
            return False
        return bool(self._cred("auth_id", "PLIVO_AUTH_ID") and
                    self._cred("auth_token", "PLIVO_AUTH_TOKEN"))

    @property
    def user_id_prefix(self) -> str:
        return "plivo:"

    @property
    def webhook_path(self) -> str:
        return "plivo"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "send_plivo_sms",
                "description": "Send an SMS via Plivo. Args: to (E.164 phone number), text (message body).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to":   {"type": "string", "description": "Recipient phone number in E.164 format"},
                        "text": {"type": "string", "description": "Message body"},
                    },
                    "required": ["to", "text"],
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
        raise NotImplementedError("Plivo send_message is not yet implemented")


plugin_cls = PlivoPlugin

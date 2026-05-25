"""
Bandwidth communication plugin (stub).

Supports SMS and RCS via Bandwidth Messaging API.
Credentials (account_id, api_token, api_secret, application_id, from_number)
live in registry under `plugins.bandwidth`. Env-var fallbacks:
BANDWIDTH_ACCOUNT_ID, BANDWIDTH_API_TOKEN, BANDWIDTH_API_SECRET,
BANDWIDTH_APPLICATION_ID, BANDWIDTH_FROM_NUMBER.
"""

import logging
import os

from fastapi import Request, Response

from app.communications.base import CommunicationPlugin

logger = logging.getLogger(__name__)


class BandwidthPlugin(CommunicationPlugin):
    """Bandwidth channel plugin — SMS / RCS."""

    def __init__(self, registry: dict):
        self._registry = registry

    def _cfg(self) -> dict:
        return self._registry.get("plugins", {}).get("bandwidth", {})

    def _cred(self, key: str, env_var: str) -> str:
        return self._cfg().get(key) or os.environ.get(env_var, "")

    @property
    def name(self) -> str:
        return "bandwidth"

    @property
    def enabled(self) -> bool:
        if not self._cfg().get("enabled", False):
            return False
        return bool(self._cred("api_token", "BANDWIDTH_API_TOKEN") and
                    self._cred("api_secret", "BANDWIDTH_API_SECRET"))

    @property
    def user_id_prefix(self) -> str:
        return "bandwidth:"

    @property
    def webhook_path(self) -> str:
        return "bandwidth"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "send_bandwidth_message",
                "description": (
                    "Send a message via Bandwidth. Args: to (E.164 phone number), "
                    "text (message body), channel (one of 'sms', 'rcs')."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to":      {"type": "string", "description": "Recipient phone number in E.164 format"},
                        "text":    {"type": "string", "description": "Message body"},
                        "channel": {"type": "string", "enum": ["sms", "rcs"], "description": "Delivery channel"},
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
        raise NotImplementedError("Bandwidth send_message is not yet implemented")


plugin_cls = BandwidthPlugin

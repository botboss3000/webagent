"""
WhatsApp via Twilio communication plugin.

Uses the same Twilio REST API as twilio_sms but with whatsapp: number prefixes.
Receives inbound WhatsApp messages via Twilio webhook.
Credentials: account_sid, auth_token, from_number (whatsapp:+14155238886 format).
Env-var fallbacks: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM.

Plugin discovery: set `plugin_cls = TwilioWhatsAppPlugin` at module level.
"""

import hashlib
import hmac
import logging
import os
import urllib.parse
from typing import Optional

import httpx
from fastapi import Request, Response

from app.communications.base import CommunicationPlugin

logger = logging.getLogger(__name__)

TWILIO_API = "https://api.twilio.com/2010-04-01"


async def _send_whatsapp(account_sid: str, auth_token: str, from_num: str, to_num: str, body: str) -> dict:
    """Send a WhatsApp message via Twilio REST API."""
    url = f"{TWILIO_API}/Accounts/{account_sid}/Messages.json"
    # Ensure whatsapp: prefix
    if not from_num.startswith("whatsapp:"):
        from_num = f"whatsapp:{from_num}"
    if not to_num.startswith("whatsapp:"):
        to_num = f"whatsapp:{to_num}"
    payload = {"From": from_num, "To": to_num, "Body": body}
    try:
        async with httpx.AsyncClient(timeout=15, auth=(account_sid, auth_token)) as client:
            resp = await client.post(url, data=payload)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("Twilio WhatsApp send failed: %s", e)
        return {"error": str(e)}


class TwilioWhatsAppPlugin(CommunicationPlugin):
    """WhatsApp via Twilio plugin — same REST API as SMS, whatsapp: prefixed numbers."""

    def __init__(self, registry: dict):
        self._registry = registry

    def _cfg(self) -> dict:
        return self._registry.get("plugins", {}).get("twilio_whatsapp", {})

    def _cred(self, key: str, env_var: str) -> str:
        return self._cfg().get(key) or os.environ.get(env_var, "")

    @property
    def name(self) -> str:
        return "twilio_whatsapp"

    @property
    def enabled(self) -> bool:
        if not self._cfg().get("enabled", False):
            return False
        return bool(self._cred("account_sid", "TWILIO_ACCOUNT_SID") and
                    self._cred("auth_token", "TWILIO_AUTH_TOKEN"))

    @property
    def user_id_prefix(self) -> str:
        return "whatsapp:"

    @property
    def webhook_path(self) -> str:
        return "twilio_whatsapp"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "send_whatsapp_message",
                "description": "Send a WhatsApp message via Twilio to a phone number. Args: to (E.164 phone number, e.g. +15551234567), text (str).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Recipient phone number in E.164 format (e.g. +15551234567, whatsapp: prefix optional)"},
                        "text": {"type": "string", "description": "Message body"},
                    },
                    "required": ["to", "text"],
                },
            }
        ]

    async def verify_request(self, request: Request) -> bool:
        """Parse Twilio form body; signature check optional in dev."""
        body_bytes = await request.body()
        params = dict(urllib.parse.parse_qsl(body_bytes.decode("utf-8")))
        request.state._twilio_wa_params = params
        return True

    def extract_external_id(self, request: Request) -> str:
        params = getattr(request.state, "_twilio_wa_params", {})
        # Strip whatsapp: prefix for storage, re-add on send
        raw = params.get("From", "")
        return raw.replace("whatsapp:", "")

    def extract_text(self, request: Request) -> str:
        params = getattr(request.state, "_twilio_wa_params", {})
        return params.get("Body", "")

    async def handle_webhook(self, request: Request) -> Response:
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml",
            status_code=200,
        )

    async def send_message(self, recipient_id: str, text: str) -> str:
        account_sid = self._cred("account_sid", "TWILIO_ACCOUNT_SID")
        auth_token = self._cred("auth_token", "TWILIO_AUTH_TOKEN")
        from_num = self._cred("from_number", "TWILIO_WHATSAPP_FROM")
        if not account_sid or not auth_token or not from_num:
            raise RuntimeError("Twilio WhatsApp credentials not configured")
        result = await _send_whatsapp(account_sid, auth_token, from_num, recipient_id, text)
        return str(result)


FEATURE = {
    "id": "whatsapp",
    "display_name": "WhatsApp (Twilio)",
    "category": "channel",
    "status": "beta",
    "summary": "Send and receive WhatsApp messages via Twilio.",
    "requires": ["Twilio account SID + auth token + a WhatsApp sender"],
}

plugin_cls = TwilioWhatsAppPlugin

"""
Twilio SMS communication plugin.

Receives inbound SMS via Twilio webhook, sends outbound SMS via Twilio REST API.
Credentials: account_sid, auth_token, from_number (stored in registry.json).
Env-var fallbacks: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER.

Plugin discovery: set `plugin_cls = TwilioSmsPlugin` at module level.
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


async def _send_sms(account_sid: str, auth_token: str, from_num: str, to_num: str, body: str) -> dict:
    """Send an SMS via Twilio REST API."""
    url = f"{TWILIO_API}/Accounts/{account_sid}/Messages.json"
    payload = {"From": from_num, "To": to_num, "Body": body}
    try:
        async with httpx.AsyncClient(timeout=15, auth=(account_sid, auth_token)) as client:
            resp = await client.post(url, data=payload)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("Twilio SMS send failed: %s", e)
        return {"error": str(e)}


def _verify_twilio_signature(auth_token: str, signature: str, url: str, params: dict) -> bool:
    """Verify Twilio's X-Twilio-Signature HMAC-SHA1."""
    try:
        sorted_params = "".join(f"{k}{v}" for k, v in sorted(params.items()))
        expected = hmac.HMAC(
            auth_token.encode("utf-8"),
            (url + sorted_params).encode("utf-8"),
            hashlib.sha1,
        ).digest()
        import base64
        return hmac.compare_digest(
            base64.b64encode(expected).decode(),
            signature,
        )
    except Exception:
        return False


class TwilioSmsPlugin(CommunicationPlugin):
    """Twilio SMS plugin — receives inbound SMS, sends outbound SMS."""

    def __init__(self, registry: dict):
        self._registry = registry

    def _cfg(self) -> dict:
        return self._registry.get("plugins", {}).get("twilio_sms", {})

    def _cred(self, key: str, env_var: str) -> str:
        return self._cfg().get(key) or os.environ.get(env_var, "")

    @property
    def name(self) -> str:
        return "twilio_sms"

    @property
    def enabled(self) -> bool:
        if not self._cfg().get("enabled", False):
            return False
        return bool(self._cred("account_sid", "TWILIO_ACCOUNT_SID") and
                    self._cred("auth_token", "TWILIO_AUTH_TOKEN"))

    @property
    def user_id_prefix(self) -> str:
        return "sms:"

    @property
    def webhook_path(self) -> str:
        return "twilio_sms"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "send_sms",
                "description": "Send an SMS message via Twilio to a phone number. Args: to (E.164 phone number e.g. +15551234567), text (str).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Recipient phone number in E.164 format (e.g. +15551234567)"},
                        "text": {"type": "string", "description": "Message body"},
                    },
                    "required": ["to", "text"],
                },
            }
        ]

    async def verify_request(self, request: Request) -> bool:
        """Verify Twilio signature. Falls back to True in dev (no auth_token)."""
        auth_token = self._cred("auth_token", "TWILIO_AUTH_TOKEN")
        sig = request.headers.get("X-Twilio-Signature", "")
        if not sig or not auth_token:
            # No signature / no token → accept (dev mode)
            body_bytes = await request.body()
            params = dict(urllib.parse.parse_qsl(body_bytes.decode("utf-8")))
            request.state._twilio_sms_params = params
            return True
        body_bytes = await request.body()
        params = dict(urllib.parse.parse_qsl(body_bytes.decode("utf-8")))
        request.state._twilio_sms_params = params
        url = str(request.url)
        return _verify_twilio_signature(auth_token, sig, url, params)

    def extract_external_id(self, request: Request) -> str:
        params = getattr(request.state, "_twilio_sms_params", {})
        return params.get("From", "")

    def extract_text(self, request: Request) -> str:
        params = getattr(request.state, "_twilio_sms_params", {})
        return params.get("Body", "")

    async def handle_webhook(self, request: Request) -> Response:
        # Webhook verification + body parsing happens in verify_request.
        # The actual routing is done by the webhooks router.
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml",
            status_code=200,
        )

    async def send_message(self, recipient_id: str, text: str) -> str:
        account_sid = self._cred("account_sid", "TWILIO_ACCOUNT_SID")
        auth_token = self._cred("auth_token", "TWILIO_AUTH_TOKEN")
        from_num = self._cred("from_number", "TWILIO_FROM_NUMBER")
        if not account_sid or not auth_token or not from_num:
            raise RuntimeError("Twilio SMS credentials not configured")
        result = await _send_sms(account_sid, auth_token, from_num, recipient_id, text)
        return str(result)


FEATURE = {
    "id": "sms",
    "display_name": "SMS (Twilio)",
    "category": "channel",
    "status": "beta",
    "summary": "Send and receive SMS via Twilio.",
    "requires": ["Twilio account SID + auth token + a phone number"],
}

plugin_cls = TwilioSmsPlugin

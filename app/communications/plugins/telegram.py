"""
Telegram communication plugin.

Uses python-telegram-bot for both sending messages and webhook reception.
Bot token from TELEGRAM_BOT_TOKEN env var.

Plugin discovery: set `plugin_cls = TelegramPlugin` at module level.
"""

import json
import logging
import os
from typing import Optional

import httpx
from fastapi import Request, Response

from app.communications.base import CommunicationPlugin

logger = logging.getLogger(__name__)

BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"

# ── Telegram API helpers (lightweight, no full SDK needed for basic ops) ──

TELEGRAM_API = "https://api.telegram.org/bot"


async def _send_message(bot_token: str, chat_id: int | str, text: str) -> dict:
    """Send a text message via Telegram Bot API."""
    url = f"{TELEGRAM_API}{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("Telegram send_message failed: %s", e)
        return {"ok": False, "error": str(e)}


async def set_webhook(bot_token: str, url: str) -> bool:
    """Register the webhook URL with Telegram."""
    api_url = f"{TELEGRAM_API}{bot_token}/setWebhook"
    payload = {"url": url, "allowed_updates": ["message"]}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(api_url, json=payload)
            data = resp.json()
            if data.get("ok"):
                logger.info("Telegram webhook set to %s", url)
                return True
            else:
                logger.error("Telegram setWebhook failed: %s", data)
                return False
    except Exception as e:
        logger.error("Telegram setWebhook error: %s", e)
        return False


async def delete_webhook(bot_token: str) -> bool:
    """Remove the webhook."""
    api_url = f"{TELEGRAM_API}{bot_token}/deleteWebhook"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(api_url)
            return resp.json().get("ok", False)
    except Exception as e:
        logger.error("Telegram deleteWebhook error: %s", e)
        return False


# ── Plugin class ────────────────────────────────────────────────────────────


class TelegramPlugin(CommunicationPlugin):
    """Telegram bot plugin. Uses Bot API directly via HTTP (no full SDK)."""

    def __init__(self, registry: dict):
        self._registry = registry
        self._bot_token: Optional[str] = None

    @property
    def name(self) -> str:
        return "telegram"

    @property
    def enabled(self) -> bool:
        plugin_config = self._registry.get("plugins", {}).get("telegram", {})
        if not plugin_config.get("enabled", False):
            return False
        # Check registry token first, then env var (so UI-set token takes priority)
        token = plugin_config.get("bot_token", "")
        if not token:
            token = os.environ.get(BOT_TOKEN_ENV, "")
        if not token:
            logger.warning("Telegram plugin enabled but no token set")
            return False
        self._bot_token = token
        return True

    @property
    def user_id_prefix(self) -> str:
        return "telegram:"

    @property
    def webhook_path(self) -> str:
        return "telegram"

    def get_tools(self) -> list[dict]:
        return [
            {
                "name": "send_telegram_message",
                "description": "Send a Telegram message to a chat. Use ONLY for registered contacts. Args: chat_id (int or str), text (str).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "chat_id": {
                            "type": ["integer", "string"],
                            "description": "Telegram chat ID (numeric, can be negative for groups)",
                        },
                        "text": {
                            "type": "string",
                            "description": "Message text (supports HTML formatting)",
                        },
                    },
                    "required": ["chat_id", "text"],
                },
            }
        ]

    def extract_external_id(self, request: Request) -> str:
        """Extract Telegram chat_id from the webhook payload."""
        # Telegram sends update as JSON body
        body = getattr(request.state, "_telegram_body", {})
        if not body:
            return ""
        message = body.get("message", {})
        chat = message.get("chat", {})
        return str(chat.get("id", ""))

    def extract_text(self, request: Request) -> str:
        """Extract message text from Telegram webhook payload."""
        body = getattr(request.state, "_telegram_body", {})
        if not body:
            return ""
        message = body.get("message", {})
        return message.get("text", "")

    async def verify_request(self, request: Request) -> bool:
        """
        Read and cache the JSON body. Telegram webhooks don't have a signature
        (unlike Twilio). For production, use a secret path or IP whitelist.
        """
        try:
            body = await request.json()
            request.state._telegram_body = body
            return True
        except Exception:
            return False

    async def handle_webhook(self, request: Request) -> Response:
        """
        Handle an incoming Telegram update.
        This is the main entry point — called from webhooks.py.
        It extracts the message, checks auth, and routes to agent loop.

        The actual agent routing happens in webhooks.py.
        This method just validates the request and returns parsed data
        for the webhook router to process.
        """
        body = getattr(request.state, "_telegram_body", {})
        if not body:
            return Response(content='{"error":"no body"}', status_code=400)

        message = body.get("message", {})
        if not message:
            # Non-message update (e.g. callback_query) — ignore
            return Response(content='{"ok":true}', status_code=200)

        return Response(content='{"ok":true}', status_code=200)

    async def send_message(self, recipient_id: str, text: str) -> str:
        """Send a message to a Telegram chat."""
        token = await self._resolve_token()
        if not token:
            raise RuntimeError("Telegram bot token not configured")
        result = await _send_message(token, recipient_id, text)
        return json.dumps(result)

    async def _resolve_token(self) -> str:
        """Resolve the bot token from registry or env var."""
        if self._bot_token:
            return self._bot_token
        plugin_config = self._registry.get("plugins", {}).get("telegram", {})
        token = plugin_config.get("bot_token", "") or os.environ.get(BOT_TOKEN_ENV, "")
        if token:
            self._bot_token = token
        return token

    async def set_webhook_url(self, base_url: str) -> bool:
        """Register the webhook with Telegram. Call after server starts."""
        token = await self._resolve_token()
        if not token:
            return False
        webhook_url = f"{base_url.rstrip('/')}/api/v1/webhooks/telegram"
        return await set_webhook(token, webhook_url)

    async def delete_webhook_url(self) -> bool:
        """Remove the webhook registration."""
        token = await self._resolve_token()
        if not token:
            return False
        return await delete_webhook(token)


# ── Plugin discovery hook ───────────────────────────────────────────────────

plugin_cls = TelegramPlugin

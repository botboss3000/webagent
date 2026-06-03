"""Minimal OpenAI-compatible chat client over httpx.

Server-independent by design: this talks straight to the LLM provider (OpenRouter
/ OpenAI / any compatible ``/chat/completions`` endpoint), so the server manager works
when the webAgent server is down. No ``openai`` SDK dependency — keeps the .exe
small and bionic-friendly for a future Termux build.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from .config import ProviderConfig


class LLMError(Exception):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Completion:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)   # {prompt_tokens, completion_tokens, total_tokens}


class LLMClient:
    def __init__(self, provider: ProviderConfig, timeout: float = 180.0) -> None:
        self.provider = provider
        self._client = httpx.AsyncClient(
            base_url=provider.base_url,
            timeout=httpx.Timeout(20.0, read=timeout),
            headers={
                "Authorization": f"Bearer {provider.api_key}",
                "Content-Type": "application/json",
                # Optional OpenRouter attribution headers (harmless elsewhere).
                "HTTP-Referer": "https://github.com/webagent",
                "X-Title": "webagent",
            },
        )

    @staticmethod
    def _network_error(e: Exception, host: str) -> str:
        """Turn a raw httpx/socket failure into an actionable message. The common one
        on phones is a DNS lookup failure ("No address associated with hostname")."""
        detail = str(e) or e.__class__.__name__
        low = detail.lower()
        is_dns = (
            "no address associated with hostname" in low
            or "name or service not known" in low
            or "nodename nor servname" in low
            or "getaddrinfo failed" in low
            or "temporary failure in name resolution" in low
        )
        if is_dns:
            return (
                f"Can't resolve '{host}' (DNS lookup failed). Check the Base URL's host is "
                f"spelled right and that this device has internet. On Android/Termux this "
                f"usually means no working DNS: connect to Wi-Fi/data, disable any VPN, and "
                f"restart Termux; you can test name resolution with 'nslookup {host}' after "
                f"'pkg install dnsutils'."
            )
        if isinstance(e, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout)):
            return f"Timed out talking to '{host}'. Check your connection and try again."
        if isinstance(e, httpx.ConnectError):
            return f"Couldn't connect to '{host}': {detail}. Check the Base URL and your network."
        return f"Request to '{host}' failed: {detail}"

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> Completion:
        if not self.provider.configured:
            raise LLMError("No API key configured. Open the App panel and set a provider + key.")
        parts = urlsplit(self.provider.base_url or "")
        host = parts.hostname or ""
        if parts.scheme not in ("http", "https") or not host:
            raise LLMError(
                f"Invalid Base URL: {self.provider.base_url!r}. It must look like "
                "'https://host/v1'. Open the App panel, pick a Provider (or fix the Base URL)."
            )
        body: dict[str, Any] = {
            "model": self.provider.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        try:
            resp = await self._client.post("/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise LLMError(self._network_error(e, host)) from e
        if resp.status_code >= 400:
            raise LLMError(f"HTTP {resp.status_code} from {host}: {resp.text[:400]}")
        try:
            data = resp.json()
            msg = data["choices"][0]["message"]
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            raise LLMError(f"bad response shape: {e}") from e

        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args))
        return Completion(content=msg.get("content") or "", tool_calls=calls, raw=msg,
                          usage=data.get("usage") or {})

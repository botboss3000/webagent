"""Minimal OpenAI-compatible chat client over httpx.

Server-independent by design: this talks straight to the LLM provider (OpenRouter
/ OpenAI / any compatible ``/chat/completions`` endpoint), so the server manager works
when the webAgent server is down. No ``openai`` SDK dependency — keeps the .exe
small and bionic-friendly for a future Termux build.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from .config import ProviderConfig


class LLMError(Exception):
    pass


class _TransientLLMError(LLMError):
    """A retryable failure (rate limit, upstream provider hiccup, timeout, or a
    200-OK response that carried an error / empty / garbled body instead of choices).
    These are the cause of the intermittent 'bad response shape: choices' the user saw:
    OpenAI-compatible providers — OpenRouter especially — return such bodies under load."""
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

    @staticmethod
    def _error_text(resp: httpx.Response) -> str:
        """Pull a human message out of an error response body when it's structured JSON
        ({"error": {"message": ...}} or {"error": "..."}); fall back to the raw text."""
        try:
            data = resp.json()
        except Exception:
            return resp.text[:400]
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])
            if isinstance(err, str) and err:
                return err
        return resp.text[:400]

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    # Statuses worth retrying: rate limit + the standard transient server/gateway codes.
    _RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        max_retries: int = 3,
    ) -> Completion:
        """Run one chat completion, retrying transient failures with exponential backoff.

        The intermittent 'bad response shape: choices' was a transient upstream condition
        (rate limit / provider hiccup) leaking through as a hard error. Such failures are
        now retried a few times; only a persistent failure surfaces to the user."""
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

        last: _TransientLLMError | None = None
        for attempt in range(max_retries + 1):
            try:
                return await self._attempt(body, host)
            except _TransientLLMError as e:
                last = e
                if attempt >= max_retries:
                    break
                # Exponential backoff: 0.5s, 1s, 2s … keeps a flaky provider from
                # turning a single hiccup into a failed turn.
                await asyncio.sleep(0.5 * (2 ** attempt))
        assert last is not None
        raise LLMError(f"{last} (after {max_retries + 1} attempts)")

    async def _attempt(self, body: dict[str, Any], host: str) -> Completion:
        try:
            resp = await self._client.post("/chat/completions", json=body)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError,
                httpx.RemoteProtocolError) as e:
            # Network-level blips are transient — retry.
            raise _TransientLLMError(self._network_error(e, host)) from e
        except httpx.HTTPError as e:
            raise LLMError(self._network_error(e, host)) from e
        if resp.status_code >= 400:
            text = self._error_text(resp)
            msg = f"HTTP {resp.status_code} from {host}: {text}"
            if resp.status_code in self._RETRY_STATUSES:
                raise _TransientLLMError(msg)
            raise LLMError(msg)
        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            # A 200 with a non-JSON body is almost always a proxy/gateway hiccup — retry.
            raise _TransientLLMError(
                f"{host} returned non-JSON (HTTP {resp.status_code}): {resp.text[:400]!r}"
            ) from e
        # OpenAI-compatible providers (OpenRouter especially) often return HTTP 200 with an
        # error payload instead of 'choices' — rate limits, upstream provider failures,
        # transient capacity. Surface/retry that instead of a cryptic KeyError('choices').
        if isinstance(data, dict) and "choices" not in data and data.get("error"):
            err = data["error"]
            detail = err.get("message") if isinstance(err, dict) else str(err)
            raise _TransientLLMError(f"{host} returned an error: {detail or err}")
        try:
            choices = data["choices"]
        except (KeyError, TypeError) as e:
            raise _TransientLLMError(
                f"bad response shape ({e}); raw: {str(data)[:400]}"
            ) from e
        if not choices:
            raise _TransientLLMError(
                f"{host} returned no choices (empty completion). Raw: {str(data)[:400]}"
            )
        try:
            msg = choices[0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"bad response shape ({e}); raw: {str(data)[:400]}") from e

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

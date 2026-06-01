"""Minimal OpenAI-compatible chat client over httpx.

Server-independent by design: this talks straight to the LLM provider (OpenRouter
/ OpenAI / any compatible ``/chat/completions`` endpoint), so the operator works
when the webAgent server is down. No ``openai`` SDK dependency — keeps the .exe
small and bionic-friendly for a future Termux build.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

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
                "X-Title": "webagent-tui",
            },
        )

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
            raise LLMError("No API key configured. Set LLM_API_KEY (or the project's .env).")
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
            raise LLMError(f"request failed: {e}") from e
        if resp.status_code >= 400:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:500]}")
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
        return Completion(content=msg.get("content") or "", tool_calls=calls, raw=msg)

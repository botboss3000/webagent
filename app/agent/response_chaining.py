"""Provider-neutral stateful response chaining for OpenResponses endpoints.

The local WebAgent transcript remains authoritative.  This module is only a
transport optimisation: supported providers may retain the previous response
and accept the next delta through ``previous_response_id``.  If a chain cannot
be resumed, callers can always rebuild a fresh request from the local messages.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, Iterable, List, Mapping, Optional
from urllib.parse import urlsplit

import httpx

from app.agent.provider_cache import provider_family


_SUPPORTED_FAMILIES = frozenset({"openai", "openrouter", "xai", "azure_openai"})


def _enabled_by_default() -> bool:
    raw = os.environ.get("WEBAGENT_STATEFUL_RESPONSES", "1")
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def response_provider_family(provider: str, base_url: str, model: str) -> str:
    identity = f"{provider} {base_url} {model}".lower()
    if "api.x.ai" in identity or provider.lower() in {"xai", "x.ai"}:
        return "xai"
    if "openai.azure.com" in identity or provider.lower() in {"azure", "azure_openai"}:
        return "azure_openai"
    return provider_family(provider, base_url, model)


@dataclass(frozen=True)
class ResponseChainCapability:
    enabled: bool
    family: str
    transport: str
    reason: str


def response_chain_capability(
    *, provider: str, base_url: str, model: str, mode: Any = "auto",
) -> ResponseChainCapability:
    """Resolve whether this provider can use the OpenResponses stateful path."""
    family = response_provider_family(provider, base_url, model)
    requested = str(mode if mode is not None else "auto").strip().lower()
    if requested in {"0", "false", "off", "disabled", "chat_completions"}:
        return ResponseChainCapability(False, family, "chat_completions", "disabled")
    if not _enabled_by_default():
        return ResponseChainCapability(False, family, "chat_completions", "global_disabled")
    if family not in _SUPPORTED_FAMILIES:
        return ResponseChainCapability(False, family, "chat_completions", "unsupported_provider")
    return ResponseChainCapability(True, family, "responses", "supported")


def response_chain_identity(*, provider: str, base_url: str, model: str) -> str:
    canonical = "\0".join((
        response_provider_family(provider, base_url, model),
        str(base_url).rstrip("/").lower(),
        str(model),
    ))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def response_chain_history_hash(messages: Iterable[Mapping[str, Any]]) -> str:
    """Fingerprint portable, non-system transcript state for safe continuation."""
    canonical = [dict(message) for message in messages if message.get("role") != "system"]
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _instructions(messages: Iterable[Mapping[str, Any]]) -> str:
    # Responses does not inherit instructions when previous_response_id is used,
    # so every current system layer is deliberately resent on every request.
    return "\n\n".join(
        str(message.get("content") or "").strip()
        for message in messages
        if message.get("role") == "system" and str(message.get("content") or "").strip()
    )


def _responses_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content if content is not None else ""
    converted: List[dict] = []
    for part in content:
        if not isinstance(part, Mapping):
            converted.append({"type": "input_text", "text": str(part)})
            continue
        kind = str(part.get("type") or "")
        if kind in {"text", "input_text"}:
            converted.append({"type": "input_text", "text": str(part.get("text") or "")})
        elif kind in {"image_url", "input_image"}:
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, Mapping) else image
            item = {"type": "input_image", "image_url": str(url or "")}
            detail = part.get("detail") or (image.get("detail") if isinstance(image, Mapping) else None)
            if detail:
                item["detail"] = detail
            converted.append(item)
        else:
            converted.append(dict(part))
    return converted


def responses_input(
    messages: Iterable[Mapping[str, Any]], *, delta_start: int = 0,
) -> List[dict]:
    """Translate Chat Completions messages into portable Responses input items."""
    source = list(messages)[max(0, int(delta_start or 0)):]
    result: List[dict] = []
    for message in source:
        role = str(message.get("role") or "")
        if role == "system":
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            if call_id:
                result.append({
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": str(message.get("content") or ""),
                })
            continue
        if role not in {"user", "assistant", "developer"}:
            continue
        content = message.get("content")
        if content not in (None, "", []):
            result.append({"role": role, "content": _responses_content(content)})
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                fn = call.get("function") if isinstance(call, Mapping) else None
                if not isinstance(fn, Mapping):
                    continue
                call_id = str(call.get("id") or "")
                if call_id:
                    result.append({
                        "type": "function_call",
                        "call_id": call_id,
                        "name": str(fn.get("name") or ""),
                        "arguments": str(fn.get("arguments") or "{}"),
                    })
    return result


def responses_tools(tools: Optional[Iterable[Mapping[str, Any]]]) -> Optional[List[dict]]:
    if not tools:
        return None
    result: List[dict] = []
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, Mapping) else None
        if tool.get("type") == "function" and isinstance(fn, Mapping):
            result.append({
                "type": "function",
                "name": str(fn.get("name") or ""),
                "description": str(fn.get("description") or ""),
                "parameters": dict(fn.get("parameters") or {}),
            })
        elif isinstance(tool, Mapping):
            result.append(dict(tool))
    return result or None


def build_responses_request(
    *,
    model: str,
    messages: List[Mapping[str, Any]],
    tools: Optional[List[Mapping[str, Any]]],
    max_output_tokens: int,
    previous_response_id: Optional[str] = None,
    delta_start: int = 0,
    reasoning_effort: Optional[str] = None,
    prompt_cache_key: Optional[str] = None,
    provider_family_name: str = "",
    session_key: str = "",
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "model": model,
        "input": responses_input(messages, delta_start=delta_start),
        "instructions": _instructions(messages),
        "max_output_tokens": max_output_tokens,
        "stream": True,
        # OpenRouter's OpenResponses schema currently exposes store=false only;
        # its own continuation layer still accepts previous_response_id.
        "store": provider_family_name != "openrouter",
    }
    converted_tools = responses_tools(tools)
    if converted_tools:
        body.update({"tools": converted_tools, "tool_choice": "auto"})
    if previous_response_id:
        body["previous_response_id"] = previous_response_id
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort}
    if prompt_cache_key:
        body["prompt_cache_key"] = prompt_cache_key
    if provider_family_name == "openrouter" and session_key:
        body["session_id"] = session_key[:256]
    return body


def _responses_url(base_url: str) -> str:
    return str(base_url).rstrip("/") + "/responses"


def _headers(*, api_key: str, base_url: str, family: str) -> Dict[str, str]:
    headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
    if family == "azure_openai" or "openai.azure.com" in urlsplit(base_url).netloc.lower():
        headers["api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


@dataclass
class _EventState:
    tool_indices: Dict[str, int] = field(default_factory=dict)
    tool_names: Dict[str, str] = field(default_factory=dict)


def _usage_chunk(response: Mapping[str, Any]) -> Any:
    usage = response.get("usage") or {}
    input_details = usage.get("input_tokens_details") or {}
    output_details = usage.get("output_tokens_details") or {}
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=usage.get("input_tokens", usage.get("prompt_tokens")),
            completion_tokens=usage.get("output_tokens", usage.get("completion_tokens")),
            cost=usage.get("cost"),
            prompt_tokens_details=input_details,
            completion_tokens_details=output_details,
            model_extra=dict(usage),
        ),
        choices=[],
    )


def response_event_chunks(event: Mapping[str, Any], state: _EventState) -> List[Any]:
    """Normalize one Responses SSE event into the loop's existing stream shape."""
    kind = str(event.get("type") or "")
    if kind == "response.output_text.delta":
        delta = SimpleNamespace(content=str(event.get("delta") or ""), tool_calls=None)
        return [SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=delta)])]
    if kind == "response.output_item.added":
        item = event.get("item") or {}
        if item.get("type") != "function_call":
            return []
        item_id = str(item.get("id") or item.get("call_id") or event.get("output_index") or "")
        index = int(event.get("output_index") or len(state.tool_indices))
        state.tool_indices[item_id] = index
        state.tool_names[item_id] = str(item.get("name") or "")
        fn = SimpleNamespace(name=state.tool_names[item_id], arguments=str(item.get("arguments") or ""))
        tc = SimpleNamespace(index=index, id=str(item.get("call_id") or item_id), function=fn)
        return [SimpleNamespace(usage=None, choices=[SimpleNamespace(
            delta=SimpleNamespace(content=None, tool_calls=[tc]),
        )])]
    if kind == "response.function_call_arguments.delta":
        item_id = str(event.get("item_id") or "")
        index = state.tool_indices.get(item_id, int(event.get("output_index") or 0))
        fn = SimpleNamespace(name=None, arguments=str(event.get("delta") or ""))
        tc = SimpleNamespace(index=index, id=None, function=fn)
        return [SimpleNamespace(usage=None, choices=[SimpleNamespace(
            delta=SimpleNamespace(content=None, tool_calls=[tc]),
        )])]
    if kind in {"response.completed", "response.incomplete"}:
        return [_usage_chunk(event.get("response") or {})]
    if kind == "error":
        error = event.get("error") or event
        raise RuntimeError(str(error.get("message") or error))
    return []


class ResponsesStream:
    """Small HTTP/SSE stream adapter exposing ChatCompletion-like chunks."""

    def __init__(self, client: httpx.AsyncClient, context: Any, response: httpx.Response):
        self._client = client
        self._context = context
        self._response = response
        self._state = _EventState()
        self.response_id: Optional[str] = None
        self._chunks: List[Any] = []
        self._lines: Optional[AsyncIterator[str]] = None

    def __aiter__(self):
        self._lines = self._response.aiter_lines()
        return self

    async def __anext__(self):
        if self._chunks:
            return self._chunks.pop(0)
        if self._lines is None:
            self._lines = self._response.aiter_lines()
        async for line in self._lines:
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            event = json.loads(payload)
            response = event.get("response") or {}
            rid = response.get("id") or event.get("response_id")
            if rid:
                self.response_id = str(rid)
            self._chunks.extend(response_event_chunks(event, self._state))
            if self._chunks:
                return self._chunks.pop(0)
        raise StopAsyncIteration

    async def close(self) -> None:
        try:
            await self._context.__aexit__(None, None, None)
        finally:
            await self._client.aclose()


async def open_responses_stream(
    *, base_url: str, api_key: str, family: str, body: Mapping[str, Any], timeout: float = 60.0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> ResponsesStream:
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, read=None), transport=transport,
    )
    context = client.stream(
        "POST", _responses_url(base_url),
        headers=_headers(api_key=api_key, base_url=base_url, family=family),
        json=dict(body),
    )
    try:
        response = await context.__aenter__()
        if response.status_code >= 400:
            detail = (await response.aread()).decode("utf-8", errors="replace")
            raise RuntimeError(f"Responses API HTTP {response.status_code}: {detail[:2000]}")
        return ResponsesStream(client, context, response)
    except BaseException:
        try:
            await context.__aexit__(None, None, None)
        finally:
            await client.aclose()
        raise


def is_missing_response_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in (
        "previous_response_not_found", "previous response not found",
        "invalid previous_response_id", "unknown response",
    ))


def is_unsupported_responses_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in (
        "http 404", "http 405", "not found", "unknown endpoint",
        "unsupported endpoint", "responses api is not supported",
    ))

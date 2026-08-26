"""Tests for the server-side cross-browser dictation fallback."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from app.api import transcription


class _Upload:
    filename = "dictation.webm"
    content_type = "audio/webm;codecs=opus"

    async def read(self, _limit: int) -> bytes:
        return b"recorded-audio"


class _Client:
    def __init__(self, response: httpx.Response, calls: list):
        self.response = response
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_openrouter_transcription_payload() -> None:
    calls = []

    async def resolve(_user_id: str):
        return {
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "test-key",
        }

    with (
        patch("app.auth.identity.assert_caller_is", AsyncMock(return_value="user-1")),
        patch("app.entitlements.service.resolve_capabilities", AsyncMock(return_value={
            "features": {"voice_llm": True},
        })),
        patch.object(transcription, "_resolve_user_config", resolve),
        patch.object(
            transcription,
            "get_voice_dictation_config",
            lambda: {"llm_enabled": True, "mode": "browser_then_llm"},
        ),
        patch.object(
            transcription.httpx,
            "AsyncClient",
            lambda **_kwargs: _Client(httpx.Response(200, json={"text": "test 1 2 3"}), calls),
        ),
    ):
        result = asyncio.run(
            transcription.transcribe_audio(
                request=SimpleNamespace(),
                file=_Upload(),
                user_id="user-1",
                language="en-US",
            )
        )

    assert result == {"text": "test 1 2 3"}
    url, request = calls[0]
    assert url == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert request["json"]["model"] == "openai/whisper-large-v3"
    assert request["json"]["input_audio"] == {
        "data": base64.b64encode(b"recorded-audio").decode("ascii"),
        "format": "webm",
    }
    assert request["json"]["language"] == "en"


def test_transcription_rejects_when_admin_disables_llm_voice() -> None:
    with (
        patch("app.auth.identity.assert_caller_is", AsyncMock(return_value="user-1")),
        patch("app.entitlements.service.resolve_capabilities", AsyncMock(return_value={
            "features": {"voice_llm": True},
        })),
        patch.object(
            transcription,
            "get_voice_dictation_config",
            lambda: {"llm_enabled": False, "mode": "browser_then_llm"},
        ),
    ):
        try:
            asyncio.run(
                transcription.transcribe_audio(
                    request=SimpleNamespace(),
                    file=_Upload(),
                    user_id="user-1",
                    language="en",
                )
            )
        except HTTPException as exc:
            assert exc.status_code == 403
        else:
            raise AssertionError("disabled LLM voice dictation was accepted")

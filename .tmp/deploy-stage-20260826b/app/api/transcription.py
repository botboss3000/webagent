"""Cross-browser speech-to-text fallback for the chat composer."""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.admin.settings import _resolve_user_config, get_voice_dictation_config


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/transcribe", tags=["transcription"])

_MAX_AUDIO_BYTES = 10 * 1024 * 1024
_AUDIO_FORMATS = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
}


def _provider_error(response: httpx.Response) -> str:
    try:
        body = response.json()
        detail: Any = body.get("error") or body.get("detail") or body
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("detail") or detail
        text = str(detail)
    except Exception:
        text = response.text or response.reason_phrase
    return text[:240]


@router.post("")
async def transcribe_audio(
    request: Request,
    file: UploadFile = File(...),
    user_id: str = Form(...),
    language: str = Form(""),
):
    """Transcribe a short composer recording with the configured AI provider."""
    from app.auth.identity import assert_caller_is
    from app.db import get_db
    from app.entitlements.service import resolve_capabilities

    user_id = await assert_caller_is(request, user_id)
    capabilities = await resolve_capabilities(user_id, db=get_db())
    if not (capabilities.get("features") or {}).get("voice_llm"):
        raise HTTPException(
            status_code=403,
            detail={"code": "upgrade_required", "feature": "voice_llm"},
        )
    if not get_voice_dictation_config()["llm_enabled"]:
        raise HTTPException(status_code=403, detail="LLM voice dictation is disabled by the administrator")

    mime_type = (file.content_type or "").split(";", 1)[0].lower()
    audio_format = _AUDIO_FORMATS.get(mime_type)
    if not audio_format:
        raise HTTPException(status_code=400, detail=f"Unsupported audio type: {mime_type or 'unknown'}")

    contents = await file.read(_MAX_AUDIO_BYTES + 1)
    if not contents:
        raise HTTPException(status_code=400, detail="The recording was empty")
    if len(contents) > _MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Recording is too large (10MB maximum)")

    config = await _resolve_user_config(user_id)
    provider = str(config.get("provider") or "").lower()
    base_url = str(config.get("base_url") or "").rstrip("/")
    api_key = str(config.get("api_key") or "")
    if not base_url or not api_key:
        raise HTTPException(
            status_code=503,
            detail="Configure an AI provider before using cross-browser voice dictation",
        )

    is_openrouter = provider == "openrouter" or "openrouter.ai" in base_url
    endpoint = f"{base_url}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"}
    language_code = (language or "").strip().lower().split("-", 1)[0][:3]

    try:
        async with httpx.AsyncClient(timeout=75.0) as client:
            if is_openrouter:
                model = os.environ.get("VOICE_TRANSCRIPTION_MODEL") or "openai/whisper-large-v3"
                payload: dict[str, Any] = {
                    "model": model,
                    "input_audio": {
                        "data": base64.b64encode(contents).decode("ascii"),
                        "format": audio_format,
                    },
                }
                if language_code:
                    payload["language"] = language_code
                response = await client.post(endpoint, json=payload, headers=headers)
            else:
                model = os.environ.get("VOICE_TRANSCRIPTION_MODEL") or "gpt-4o-mini-transcribe"
                data = {"model": model}
                if language_code:
                    data["language"] = language_code
                response = await client.post(
                    endpoint,
                    data=data,
                    files={"file": (file.filename or f"dictation.{audio_format}", contents, mime_type)},
                    headers=headers,
                )
    except httpx.RequestError as exc:
        logger.warning("Voice transcription request failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not reach the transcription provider") from exc

    if response.status_code >= 400:
        provider_detail = _provider_error(response)
        logger.warning("Voice transcription provider returned %s: %s", response.status_code, provider_detail)
        raise HTTPException(
            status_code=502,
            detail=f"Transcription provider rejected the recording: {provider_detail}",
        )

    try:
        result = response.json()
        text = str(result.get("text") or "").strip()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Transcription provider returned an invalid response") from exc
    if not text:
        raise HTTPException(status_code=422, detail="No speech was detected in the recording")
    return {"text": text}

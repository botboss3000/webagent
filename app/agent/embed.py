"""
Embedding utility for webAgent memory system.

Uses the SAME provider config as the chat agent (LLM_BASE_URL / LLM_API_KEY env vars
set from the web UI Settings modal). No separate .env key required.

For the embedding model, uses EMBED_MODEL env var (default: text-embedding-3-small).
Future: per-task model selection in web UI settings.

Returns {EMBED_DIM}-dim float32 vectors (configurable via EMBED_DIM env var, default 1536).
"""

from __future__ import annotations

import logging
import os
from typing import List

logger = logging.getLogger(__name__)

_embed_client = None
_embed_model_name: str = ""

EMBED_DIM = int(os.environ.get("EMBED_DIM") or 1536)


def _get_embed_client():
    """Lazy-init the embed client, same provider config as chat.

    Reads LLM_BASE_URL / LLM_API_KEY (set by admin/settings.py on provider save),
    falls back to OPENROUTER_* env vars for dev convenience.
    """
    global _embed_client, _embed_model_name
    if _embed_client is None:
        from openai import AsyncOpenAI

        base_url = (
            os.environ.get("LLM_BASE_URL")
            or os.environ.get("OPENROUTER_BASE_URL")
            or "https://openrouter.ai/api/v1"
        )
        api_key = (
            os.environ.get("LLM_API_KEY")
            or os.environ.get("OPENROUTER_API_KEY")
            or ""
        )
        model = os.environ.get("EMBED_MODEL") or "text-embedding-3-small"

        _embed_client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=15.0)
        _embed_model_name = model
        logger.info("Embed client initialised: model=%s base_url=%s", model, base_url)
    return _embed_client, _embed_model_name


async def embed_text(text: str) -> List[float]:
    """Embed a single text string via the configured provider.

    Returns a 1536-dim list of float32 values.
    Raises on API error (caller should catch and fall back gracefully).
    """
    client, model = _get_embed_client()
    response = await client.embeddings.create(model=model, input=text[:8000])
    return response.data[0].embedding

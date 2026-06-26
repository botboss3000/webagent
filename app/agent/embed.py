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
import time
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
    # Record background usage without blocking this latency-sensitive call:
    # memory recall awaits embeddings before the LLM turn, so fire-and-forget.
    try:
        _u = getattr(response, "usage", None)
        if _u:
            import asyncio
            from plugins.billing.usage import record_background_usage
            _tok = getattr(_u, "prompt_tokens", None)
            if _tok is None:
                _tok = getattr(_u, "total_tokens", 0) or 0
            asyncio.create_task(record_background_usage(
                model=model, input_tokens=_tok or 0, label="embed"))
    except Exception:
        pass
    return response.data[0].embedding


async def warm_embed_client() -> bool:
    """Best-effort warm-up of the embedding client, called once at startup.

    The first embedding request of a process pays a cold-start penalty (lazy
    openai import + httpx client construction + DNS/TLS handshake + provider
    cold routing) — measured ~7.5s cold vs ~0.3s warm. Firing one throwaway
    embed at boot moves that cost off the user's first chat turn, so the
    pre-agent memory_search no longer stalls visibly on a cold connection.

    Safe to call when no provider key is configured yet — it logs and returns
    False rather than raising. The real call still falls back lazily on first use.
    """
    if not (os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY")):
        logger.info("Embed warmup skipped: no provider API key configured yet")
        return False
    t = time.perf_counter()
    try:
        await embed_text("warmup")
        logger.info("Embed client warmed in %.2fs", time.perf_counter() - t)
        return True
    except Exception as e:
        logger.warning("Embed warmup failed (will retry lazily on first use): %s", e)
        return False

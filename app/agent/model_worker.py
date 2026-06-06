"""One-shot "ask a model" worker — the simple custom-prompt sub-task primitive.

Runs ONE chat completion against a chosen model with a custom system prompt, a
user message, and optional inline images — **with NO tools attached** — and
returns the text answer. That last part matters: image-only models (e.g.
``google/gemini-2.5-flash-image``) reject any request that carries tools, so a
tools-free worker is the only way to delegate to them. This is the lightweight
alternative to spawning a full ephemeral agent: stateless, one round, fast.

It is deliberately model-agnostic. Callers resolve which model to use (a vision
model for image questions, an image-out model for generation, etc.) and pass its
config in; the worker just runs the turn.

╔══════════════════════════════════════════════════════════════════════════════╗
║  SISTER-SYNC: MODEL-WORKER-DELEGATE — keep callers in sync                    ║
║  This neutral-core helper is the shared "spawn a simple custom-prompt worker  ║
║  on model M" primitive. It is reused by:                                      ║
║    • plugins/abilities/image_vision.py   (process_image → vision model)       ║
║    • plugins/abilities/agent_orchestration.py (optional lightweight delegate) ║
║  It lives in core (not in a plugin) so an agent can delegate image work even  ║
║  when the Orchestration ability is OFF. If you change ask_model's signature   ║
║  or return shape, update every caller (grep SISTER-SYNC: MODEL-WORKER-DELEGATE║
║  ). The image-describe sub-step (app/agent/prompts.describe_image_attachment) ║
║  is the same shape and predates this — keep them conceptually aligned.        ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


async def ask_model(
    model_cfg: Dict[str, Any],
    system_prompt: str,
    user_text: str,
    attachments: Optional[List[Dict[str, Any]]] = None,
    *,
    max_tokens: int = 900,
    temperature: float = 0.2,
    timeout: float = 60.0,
) -> Optional[str]:
    """Run one tools-free completion and return the model's text reply.

    Args:
      model_cfg:  {model, base_url, api_key} for the target model (provider key).
      system_prompt: the worker's instructions ("you are X, do Y, report Z").
      user_text:  the question / task for this round.
      attachments: optional attachment dicts; any inlinable image among them is
                   passed to the model as an image part (others are ignored — a
                   tools-free worker can't open files).
    Returns the reply text, or None when the config is incomplete or the call
    fails. Never raises — callers treat None as "delegation unavailable".
    """
    model = (model_cfg or {}).get("model", "")
    base_url = (model_cfg or {}).get("base_url", "")
    api_key = (model_cfg or {}).get("api_key", "")
    if not (model and base_url and api_key):
        return None

    # Inline any image attachments (reusing the shared mime/size/storage guards).
    image_parts: List[Dict[str, Any]] = []
    if attachments:
        from app.agent.prompts import _attachment_to_image_data_url
        for att in attachments:
            try:
                url = await _attachment_to_image_data_url(att)
            except Exception:
                url = None
            if url:
                image_parts.append({"type": "image_url", "image_url": {"url": url}})

    if image_parts:
        user_content: Any = [{"type": "text", "text": user_text or ""}, *image_parts]
    else:
        user_content = user_text or ""

    try:
        from openai import AsyncOpenAI
    except ImportError:
        from app.openai_compat import AsyncOpenAI

    try:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if resp and resp.choices:
            return (resp.choices[0].message.content or "").strip() or None
    except Exception as e:
        logger.warning("ask_model failed (model=%s): %s", model, e)
    return None

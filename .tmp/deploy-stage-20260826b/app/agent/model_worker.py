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


def _completion_is_empty(resp) -> tuple:
    """Return (is_empty, finish_reason) for a chat completion. A reasoning model
    can return a 200 with NO visible text (it spent the whole token budget on
    hidden reasoning) — that must be treated as a failure, not a blank answer."""
    try:
        ch = resp.choices[0] if (resp and resp.choices) else None
        if ch is None:
            return True, None
        content = getattr(ch.message, "content", None) or ""
        return (not content.strip()), getattr(ch, "finish_reason", None)
    except Exception:
        return False, None  # don't second-guess an unexpected shape


async def safe_chat_completion(client, **kwargs):
    """``client.chat.completions.create`` with graceful fallback for models that
    reject common params OR return an empty reply.

    Robustness layers (a one-shot worker must not be silently defeated by a model
    quirk):
      • Some newer models (OpenAI GPT-5 family on OpenRouter) reject
        ``temperature``; others want ``max_completion_tokens`` instead of
        ``max_tokens`` — on such a 400 we drop/swap the param and retry.
      • Reasoning models spend completion tokens on hidden reasoning first; if
        that eats the whole budget the visible content comes back EMPTY. So we
        ask for LOW reasoning effort by default (budget goes to the answer), and
        if a reply still arrives empty because the budget was exhausted
        (finish_reason 'length'), we retry once with a much larger budget.
      • Any param the provider doesn't understand (e.g. the reasoning hint on a
        non-reasoning model) is dropped and the call retried, never raised blindly.
    """
    # Keep "thinking" cheap so a describe/answer worker spends its tokens on the
    # visible reply. Caller can override by passing reasoning/extra_body itself.
    if "extra_body" not in kwargs and "reasoning_effort" not in kwargs:
        kwargs["extra_body"] = {"reasoning": {"effort": "low"}}
    bumped = False
    last = None
    for _ in range(6):
        try:
            resp = await client.chat.completions.create(**kwargs)
        except Exception as e:
            last = e
            msg = str(e).lower()
            if "temperature" in kwargs and "temperature" in msg \
                    and ("not supported" in msg or "unsupported" in msg):
                kwargs.pop("temperature", None)
                continue
            if "max_tokens" in kwargs and "max_completion_tokens" in msg:
                kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
                continue
            if "extra_body" in kwargs:  # provider may not accept the reasoning hint
                kwargs.pop("extra_body", None)
                continue
            raise
        # 200 but empty because reasoning exhausted the budget → retry bigger once.
        empty, finish = _completion_is_empty(resp)
        if empty and finish == "length" and not bumped:
            bumped = True
            for key in ("max_tokens", "max_completion_tokens"):
                if key in kwargs:
                    kwargs[key] = max(int(kwargs[key]) * 3, 3000)
            continue
        return resp
    raise last if last else RuntimeError("safe_chat_completion: retries exhausted")


async def ask_model(
    model_cfg: Dict[str, Any],
    system_prompt: str,
    user_text: str,
    attachments: Optional[List[Dict[str, Any]]] = None,
    *,
    max_tokens: int = 900,
    temperature: float = 0.2,
    timeout: float = 60.0,
    error_sink: Optional[List[str]] = None,
) -> Optional[str]:
    """Run one tools-free completion and return the model's text reply.

    Args:
      model_cfg:  {model, base_url, api_key} for the target model (provider key).
      system_prompt: the worker's instructions ("you are X, do Y, report Z").
      user_text:  the question / task for this round.
      attachments: optional attachment dicts; any inlinable image among them is
                   passed to the model as an image part (others are ignored — a
                   tools-free worker can't open files).
      error_sink: optional list; on failure the real error string is appended so
                  callers can surface WHY the call failed (e.g. "no remaining
                  credits") instead of only seeing None.
    Returns the reply text, or None when the config is incomplete or the call
    fails. Never raises — callers treat None as "delegation unavailable".
    """
    model = (model_cfg or {}).get("model", "")
    base_url = (model_cfg or {}).get("base_url", "")
    api_key = (model_cfg or {}).get("api_key", "")
    if not (model and base_url and api_key):
        if error_sink is not None:
            error_sink.append("ask_model: incomplete model config (missing model/base_url/api_key)")
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
        resp = await safe_chat_completion(
            client,
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
        if error_sink is not None:
            error_sink.append(str(e))
    return None

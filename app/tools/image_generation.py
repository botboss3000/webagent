"""
Image generation tool — multi-provider, configured by the admin/user.

Which model generates images comes from the unified Models list: the saved
model ticked for image output (App Config → Models → Image-out), resolved by
``pick_image_generator`` in app/admin/settings.py. A standalone
``auth_elements`` row under service="image_gen" is still honored as a legacy
fallback for setups created before image generation was folded into Models.

Default dispatch hits an OpenAI-compatible `/images/generations` endpoint —
that covers OpenAI (DALL·E, gpt-image-1), Together AI, DeepInfra, Fireworks,
and other OpenAI-compatible image hosts. Three providers do NOT use that route
and are special-cased below: Stability AI, Google Gemini/Imagen, and OpenRouter.
OpenRouter has **no** `/images/generations` endpoint at all — image models there
are driven through `/chat/completions` with `modalities: ["image","text"]`, and
the generated image comes back as a data URL in `message.images[]`. Hitting the
OpenAI route on OpenRouter returns its website's 404 HTML page, so an OpenRouter
image model MUST take the `openrouter` shape.

Generated images are persisted in the user's data home under
`data/user_data/{user_id}/visuals/` and a relative `/user_data/...` URL is
returned so the chat UI can render the result inline.
"""

import base64
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from app import user_workspace as ws

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Provider presets known to support image generation. Mirrors the LLM
# PROVIDER_PRESETS shape so the UI can offer a familiar dropdown.
IMAGE_PROVIDER_PRESETS = {
    "openai": {
        "name": "OpenAI (DALL·E / gpt-image)",
        "base_url": "https://api.openai.com/v1",
        "api_shape": "openai",
        "suggested_models": ["dall-e-3", "dall-e-2", "gpt-image-1"],
    },
    "together": {
        "name": "Together AI",
        "base_url": "https://api.together.xyz/v1",
        "api_shape": "openai",
        "suggested_models": ["black-forest-labs/FLUX.1-schnell-Free", "black-forest-labs/FLUX.1-dev"],
    },
    "fireworks": {
        "name": "Fireworks AI",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "api_shape": "openai",
        "suggested_models": ["accounts/fireworks/models/flux-1-schnell-fp8", "accounts/fireworks/models/playground-v2-5-1024px-aesthetic"],
    },
    "deepinfra": {
        "name": "DeepInfra",
        "base_url": "https://api.deepinfra.com/v1/openai",
        "api_shape": "openai",
        "suggested_models": ["black-forest-labs/FLUX-1-schnell", "stabilityai/sdxl-turbo"],
    },
    "stability": {
        "name": "Stability AI",
        "base_url": "https://api.stability.ai",
        "api_shape": "stability",
        "suggested_models": ["sd3.5-large", "sd3.5-medium", "core", "ultra"],
    },
    "gemini": {
        "name": "Google Gemini / Imagen",
        "base_url": "https://generativelanguage.googleapis.com",
        "api_shape": "gemini",
        "suggested_models": ["imagen-3.0-generate-002", "gemini-2.0-flash-exp-image-generation"],
    },
}


# ── Config storage (parallel to settings.py LLM pattern) ────────────────────


def _user_visuals_dir(user_id: str) -> Path:
    """The user's 'visuals' room in their data home (data/user_data/<uid>/visuals/)."""
    return ws.user_dir(user_id, "visuals")


async def load_image_provider_for_user(user_id: str) -> Optional[dict]:
    """Return the image-generation config for this user.

    Source order:
      1. The unified Models list — the saved model the user ticked for image
         output (own config, then admin_default, resolved inside
         load_llm_capabilities_for_user), via ``pick_image_generator``.
      2. Legacy fallback — a standalone service="image_gen" auth element, if one
         still exists from before image generation was folded into Models.
    Returns {provider, base_url, api_key, model, api_shape} or None.
    """
    # 1) Unified Models list (preferred)
    try:
        from app.admin.settings import (
            load_llm_capabilities_for_user,
            pick_image_generator,
        )
        caps = await load_llm_capabilities_for_user(user_id)
        picked = pick_image_generator(caps)
        if picked:
            return picked
    except Exception as e:
        logger.debug("unified image-generator lookup failed: %s", e)

    # 2) Legacy standalone image_gen config (graceful migration)
    from app.db import get_db
    db = get_db()
    for uid in (user_id, "admin_default"):
        try:
            elem = await db.auth_element_get(uid, "image_gen", "default")
        except Exception as e:
            logger.debug("auth_element_get failed for image_gen %s: %s", uid, e)
            continue
        if not elem:
            continue
        cfg = elem.get("config") or {}
        if isinstance(cfg, str):
            try:
                cfg = json.loads(cfg)
            except Exception:
                cfg = {}
        cfg["api_key"] = elem.get("secret_ref", "")
        if cfg.get("api_key") and cfg.get("model"):
            return cfg
    return None


# ── The tool ────────────────────────────────────────────────────────────────


async def generate_image(
    user_id: str,
    prompt: str,
    size: str = "1024x1024",
    n: int = 1,
    quality: Optional[str] = None,
    style: Optional[str] = None,
) -> str:
    """Generate one or more images from a text prompt using the configured provider."""
    if not prompt or not prompt.strip():
        return json.dumps({"status": "error", "message": "prompt is required"})

    cfg = await load_image_provider_for_user(user_id)
    if not cfg:
        return json.dumps({
            "status": "error",
            "code": "image_gen_not_configured",
            "message": (
                "No image generation model is configured. Ask the admin to open "
                "App Config → Models, save a model that can create images, and tick "
                "its Image-out box."
            ),
        })

    provider = (cfg.get("provider") or "").lower().strip()
    base_url = (cfg.get("base_url") or "").rstrip("/")
    api_key = cfg.get("api_key") or ""
    model = cfg.get("model") or ""
    api_shape = (cfg.get("api_shape")
                 or IMAGE_PROVIDER_PRESETS.get(provider, {}).get("api_shape")
                 or "openai")

    if not api_key or not model:
        return json.dumps({
            "status": "error",
            "message": "Image generation config missing api_key or model.",
        })

    try:
        import httpx
    except Exception as e:
        return json.dumps({"status": "error", "message": f"httpx unavailable: {e}"})

    n = max(1, min(int(n or 1), 4))

    try:
        if api_shape == "openai":
            return await _openai_compatible(
                user_id=user_id, base_url=base_url, api_key=api_key, model=model,
                prompt=prompt, size=size, n=n, quality=quality, style=style,
                client_cls=httpx.AsyncClient,
            )
        if api_shape == "stability":
            return await _stability(
                user_id=user_id, base_url=base_url, api_key=api_key, model=model,
                prompt=prompt, client_cls=httpx.AsyncClient,
            )
        if api_shape == "gemini":
            return await _gemini(
                user_id=user_id, base_url=base_url, api_key=api_key, model=model,
                prompt=prompt, n=n, client_cls=httpx.AsyncClient,
            )
        if api_shape == "openrouter":
            return await _openrouter(
                user_id=user_id, base_url=base_url, api_key=api_key, model=model,
                prompt=prompt, client_cls=httpx.AsyncClient,
            )
        return json.dumps({"status": "error", "message": f"Unknown api_shape '{api_shape}'"})
    except Exception as e:
        logger.exception("generate_image failed")
        return json.dumps({"status": "error", "message": str(e)})


# ── Per-provider implementations ────────────────────────────────────────────


def _save_png_bytes(user_id: str, raw: bytes, ext: str = "png") -> str:
    """Persist raw image bytes under visuals/users/{user_id}/ and return the
    relative URL the chat UI can use to render it."""
    out_dir = _user_visuals_dir(user_id)
    fname = f"imggen-{int(time.time())}-{uuid.uuid4().hex[:8]}.{ext}"
    path = out_dir / fname
    with open(path, "wb") as f:
        f.write(raw)
    return ws.public_url(user_id, "visuals", fname)


def _save_from_url(user_id: str, url: str, http_client) -> Optional[str]:
    """Download a provider-hosted URL and persist a local copy. Used so images
    survive after provider URLs expire."""
    try:
        resp = http_client.get(url)
        if resp.status_code != 200:
            return None
        ct = resp.headers.get("content-type", "image/png")
        ext = "png" if "png" in ct else ("jpg" if "jpeg" in ct or "jpg" in ct else "png")
        return _save_png_bytes(user_id, resp.content, ext=ext)
    except Exception:
        return None


async def _openai_compatible(*, user_id, base_url, api_key, model, prompt, size, n, quality, style, client_cls) -> str:
    """POST {base_url}/images/generations with OpenAI's payload shape.

    Most providers return either a `data[].url` (transient) or `data[].b64_json`.
    We persist either form to disk and return the local URLs.
    """
    url = f"{base_url}/images/generations"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict = {"model": model, "prompt": prompt, "n": n, "size": size}
    if quality:
        payload["quality"] = quality
    if style:
        payload["style"] = style
    async with client_cls(timeout=120.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code != 200:
            return json.dumps({
                "status": "error",
                "message": f"Provider HTTP {resp.status_code}: {resp.text[:500]}",
            })
        data = resp.json() or {}
        items = data.get("data") or []
        saved = []
        for item in items:
            if item.get("b64_json"):
                raw = base64.b64decode(item["b64_json"])
                saved.append(_save_png_bytes(user_id, raw, ext="png"))
            elif item.get("url"):
                rel = _save_from_url(user_id, item["url"], client)
                saved.append(rel or item["url"])
        return json.dumps({
            "status": "ok",
            "provider": "openai_compatible",
            "model": model,
            "images": saved,
            "count": len(saved),
        })


async def _stability(*, user_id, base_url, api_key, model, prompt, client_cls) -> str:
    """Stability AI v2beta REST endpoint. Model names: 'core', 'ultra', 'sd3.5-large', etc.
    Endpoint path varies by model family."""
    model_lc = (model or "core").lower()
    if model_lc.startswith("sd3"):
        path = "/v2beta/stable-image/generate/sd3"
        form = {"prompt": (None, prompt), "model": (None, model_lc), "output_format": (None, "png")}
    elif model_lc == "ultra":
        path = "/v2beta/stable-image/generate/ultra"
        form = {"prompt": (None, prompt), "output_format": (None, "png")}
    else:
        path = "/v2beta/stable-image/generate/core"
        form = {"prompt": (None, prompt), "output_format": (None, "png")}
    url = f"{base_url}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "image/*",
    }
    async with client_cls(timeout=180.0) as client:
        resp = await client.post(url, headers=headers, files=form)
        if resp.status_code != 200:
            return json.dumps({
                "status": "error",
                "message": f"Stability HTTP {resp.status_code}: {resp.text[:500]}",
            })
        rel = _save_png_bytes(user_id, resp.content, ext="png")
        return json.dumps({
            "status": "ok",
            "provider": "stability",
            "model": model,
            "images": [rel],
            "count": 1,
        })


async def _gemini(*, user_id, base_url, api_key, model, prompt, n, client_cls) -> str:
    """Google Generative Language API — supports both Imagen (`predict`) and
    Gemini 2.0 Flash image (`generateContent`).

    The two endpoint paths return different payloads:
      - imagen-* → `predictions[].bytesBase64Encoded`
      - gemini-*-image* → `candidates[].content.parts[].inlineData.data` (b64)
    """
    model_lc = (model or "").lower()
    headers = {"Content-Type": "application/json"}
    if model_lc.startswith("imagen"):
        url = f"{base_url}/v1beta/models/{model}:predict?key={api_key}"
        payload = {
            "instances": [{"prompt": prompt}],
            "parameters": {"sampleCount": n},
        }
    else:
        url = f"{base_url}/v1beta/models/{model}:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
        }
    async with client_cls(timeout=180.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code != 200:
            return json.dumps({
                "status": "error",
                "message": f"Gemini HTTP {resp.status_code}: {resp.text[:500]}",
            })
        data = resp.json() or {}
        saved: list[str] = []
        for pred in (data.get("predictions") or []):
            b64 = pred.get("bytesBase64Encoded")
            if b64:
                saved.append(_save_png_bytes(user_id, base64.b64decode(b64), ext="png"))
        for cand in (data.get("candidates") or []):
            for part in (cand.get("content", {}).get("parts") or []):
                inline = part.get("inlineData") or part.get("inline_data") or {}
                b64 = inline.get("data")
                if b64:
                    saved.append(_save_png_bytes(user_id, base64.b64decode(b64), ext="png"))
        return json.dumps({
            "status": "ok",
            "provider": "gemini",
            "model": model,
            "images": saved,
            "count": len(saved),
        })


async def _openrouter(*, user_id, base_url, api_key, model, prompt, client_cls) -> str:
    """OpenRouter image generation — via /chat/completions, NOT /images/generations.

    OpenRouter routes image models through the chat API: send the prompt as a user
    message with ``modalities: ["image", "text"]``; the result image(s) come back as
    data URLs under ``choices[].message.images[].image_url.url``. We decode and
    persist them like every other provider.
    """
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image", "text"],
    }
    async with client_cls(timeout=180.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code != 200:
            return json.dumps({
                "status": "error",
                "message": f"OpenRouter HTTP {resp.status_code}: {resp.text[:500]}",
            })
        data = resp.json() or {}
        saved: list[str] = []
        for choice in (data.get("choices") or []):
            msg = (choice or {}).get("message") or {}
            for img in (msg.get("images") or []):
                # OpenRouter shape: {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}
                src = ((img or {}).get("image_url") or {}).get("url") or img.get("url") or ""
                if src.startswith("data:"):
                    try:
                        b64 = src.split(",", 1)[1]
                        saved.append(_save_png_bytes(user_id, base64.b64decode(b64), ext="png"))
                    except Exception:
                        continue
                elif src.startswith("http"):
                    rel = _save_from_url(user_id, src, client)
                    if rel:
                        saved.append(rel)
        if not saved:
            # No image came back — surface any text the model returned to explain why.
            txt = ""
            for choice in (data.get("choices") or []):
                txt = ((choice or {}).get("message") or {}).get("content") or ""
                if txt:
                    break
            return json.dumps({
                "status": "error",
                "message": f"OpenRouter returned no image. Model said: {txt[:300]}" if txt
                else "OpenRouter returned no image (the model may not support image output).",
            })
        return json.dumps({
            "status": "ok",
            "provider": "openrouter",
            "model": model,
            "images": saved,
            "count": len(saved),
        })


TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "description": "What to draw — be specific and visual."},
        "size": {
            "type": "string",
            "description": "WxH for OpenAI-shape providers (e.g. '1024x1024', '1024x1792'). Ignored by Stability/Gemini.",
            "default": "1024x1024",
        },
        "n": {
            "type": "integer",
            "description": "How many images to generate (1–4). Some providers only honor n=1.",
            "default": 1,
        },
        "quality": {"type": "string", "description": "Optional OpenAI quality flag ('standard' | 'hd')."},
        "style": {"type": "string", "description": "Optional OpenAI style flag ('vivid' | 'natural')."},
    },
    "required": ["prompt"],
}

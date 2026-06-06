"""Image Generation ability — SELF-CONTAINED drop-in.

A TIER-1 ability: the actual handler lives in core (``app/tools/image_generation``)
and uses whichever saved model is ticked for image output in App Config → Models
(Image-out), saving results under ``visuals/users/{user_id}/``. This file is the
drop-in face of that capability — it declares the FEATURE descriptor and, via
``build_tools``, lazily imports the core handler + its schema and hands them to
``app/tools/loader.py`` for registration. See app/abilities/__init__.py for the
discovery contract.
"""

from __future__ import annotations

from typing import Optional

FEATURE = {
    "id": "image_generation",
    "display_name": "Image Generation",
    "category": "ability",
    "status": "beta",
    "summary": "generate_image via the configured image model.",
    "tools": ["generate_image"],
    "group": "core",
    "icon": "image",
    "color": "#bb9af7",
    "description": "Lets the agent generate images from text prompts. Requires a provider + model config.",
    "simple": False,
}


# Populated inside build_tools() from the core module's TOOL_PARAMETERS so the
# schema can never drift from the handler. generate_image is non-destructive
# (the loader marks it destructive=False), so DESTRUCTIVE stays empty.
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: Optional[str] = None, enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the image_generation ability.

    Lazily imports the core handler + its parameter schema so merely scanning
    FEATURE stays cheap. Closes over ``user_id`` exactly as the old loader block
    did, and refreshes module-level TOOL_SCHEMAS from the core constant so the
    loader (which reads it AFTER this call) sees the live schema.
    """
    from app.tools.image_generation import (
        generate_image as _core_generate_image,
        TOOL_PARAMETERS as _IMG_PARAMS,
    )

    async def _generate_image_wrapper(prompt: str, size: str = "1024x1024",
                                      n: int = 1, quality: Optional[str] = None,
                                      style: Optional[str] = None):
        return await _core_generate_image(
            user_id=user_id, prompt=prompt, size=size, n=n,
            quality=quality, style=style,
        )

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({"generate_image": _IMG_PARAMS})

    return {"generate_image": _generate_image_wrapper}

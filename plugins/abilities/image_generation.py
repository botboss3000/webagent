"""Image Generation ability — drop-in. See app/abilities/__init__.py for the contract."""

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
